"""One collection cycle, start to finish. The thing a scheduler calls.

BUILD_BRIEF.md section 3 says to run Phase 0 for four to six weeks. Nothing was
running it: every command in the repo did one step and expected a human to run the
next. This is the whole loop as a single entrypoint, so a cron line or a GitHub
Actions schedule is enough to actually accumulate the dataset.

    journal -> database -> poll -> re-price -> label -> social -> score -> export -> journal

The first and last steps are what make it schedulable. State lives in Neon when
``SCREENER_DATABASE_URL`` is set (``collectors/neon.py``), otherwise in an
append-only JSONL journal (``collectors/journal.py``) that git can hold. Either way
it is rebuilt into DuckDB at the start of a run and appended to at the end. A run
that dies halfway loses that run's work and nothing else.

Every step is optional at the flag level and none of them is skipped by default,
because a half-run collector is worse than no collector: outcome labels that stop
being refreshed silently turn a live token into a permanently unresolved one.

What this does not do
---------------------
It does not trade, and there is no argument that makes it (hard rule 4). It does
not delete or rewrite anything: the journal is append-only, the database refuses a
second snapshot per token, and a re-priced token gets a new observation row rather
than an edited one. And it does not decide the regime -- that is a property of the
tape supplied by the operator, recorded, never inferred per token.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from collectors import chains, journal, neon
from collectors.config import load_config
from collectors.dexscreener import DexScreenerClient, DexScreenerFeed, DexScreenerPriceSource
from collectors.outcomes import OutcomeTracker
from collectors.safety import SafetySource
from collectors.social_base import OFFSET_TOLERANCE_MINUTES
from collectors.social_tg import TelegramCollector, TelegramPreviewClient
from collectors.social_x import XClient, XCollector, max_searches_from_env
from collectors.store import Store
from collectors.trigger_watcher import TriggerWatcher
from scoring.runner import ScoringRunner

log = logging.getLogger("collect")

DEFAULT_STATE_DIR = "state"
DEFAULT_WORKING_DB = "data/collector.duckdb"


def run_cycle(
    *,
    chain_names: Sequence[str],
    state_dir: str | Path = DEFAULT_STATE_DIR,
    db_path: str | Path = DEFAULT_WORKING_DB,
    regime: str | None = None,
    with_safety: bool = True,
    with_social: bool = True,
    max_tokens_per_poll: int = 120,
    score_limit: int = 200,
    export_to: str | Path | None = None,
    feed: Any = None,
    price_source: Any = None,
    safety_source: Any = None,
    telegram_client: Any = None,
    x_client: Any = None,
    x_max_searches: int | None = None,
    database_url: str | None = None,
    mirror_dir: str | Path = neon.DEFAULT_MIRROR_DIR,
    neon_journal: Any = None,
) -> dict[str, Any]:
    """Run one full cycle and return a summary. Every source is injectable for tests.

    With ``database_url`` (or an injected ``neon_journal``) the dataset lives in
    Neon: the run restores from a local mirror of it, and its new rows go to Neon.
    state/ is then read once -- to seed an empty Neon -- and otherwise only its
    manifest, the collector's heartbeat, is written. See collectors/neon.py.
    """
    started = time.time()
    resolved = chains.resolve_requested(list(chain_names))

    working = Path(db_path)
    if working.exists():
        # A leftover working copy from a crashed run. The journal is the dataset;
        # rebuilding from it is always correct, so the stale copy is not consulted.
        working.unlink()

    if (database_url or neon_journal is not None) and (
        Path(mirror_dir).resolve() == Path(state_dir).resolve()
    ):
        raise ValueError(
            "the Neon mirror and the git journal must be different directories: "
            "the mirror is a cache that may be emptied and rebuilt"
        )
    dataset = neon_journal or (neon.NeonJournal.connect(database_url) if database_url else None)

    store = Store(working)
    summary: dict[str, Any] = {"chains": resolved, "regime": regime}
    try:
        source: str | Path = state_dir
        dataset_id = ""
        if dataset is not None:
            dataset_id = dataset.ensure_schema()
            summary["store"] = "neon"
            summary["neon"] = {
                # A no-op on every run but the first: it copies state/ into Neon
                # only while Neon is empty.
                "bootstrapped": dataset.bootstrap(state_dir),
                "pulled": dataset.pull(mirror_dir, dataset_id),
            }
            source = mirror_dir
        else:
            summary["store"] = "journal"
        summary["restored"] = journal.restore(store, source)

        # for_chains reads SCREENER_SEED_TOKENS itself, so a seeded address is
        # polled on the schedule without a second switch -- the same shape as
        # SCREENER_CHAIN_IDS, and for the same reason: a chain whose tokens
        # nobody boosts is invisible to discovery, and binding its id alone
        # collects nothing while looking exactly like a quiet chain.
        feed = feed or DexScreenerFeed.for_chains(
            resolved, client=DexScreenerClient(), max_tokens_per_poll=max_tokens_per_poll
        )
        seeds = tuple(getattr(feed, "seed_contracts", ()) or ())
        if seeds:
            summary["seeded_tokens"] = len(seeds)
        watcher = TriggerWatcher(
            store, source=getattr(feed, "source_name", "dexscreener"), regime=regime
        )
        try:
            batch = list(feed.poll())
        except Exception:
            # A source outage delays a snapshot; it must not lose the run, because
            # the rest of the cycle still has labels to refresh and rows to score.
            log.exception("poll failed")
            batch = []
        fired = watcher.process(batch)
        summary["poll"] = {"observed": len(batch), "fired": len(fired)}

        tracker = OutcomeTracker(store)
        source = price_source or DexScreenerPriceSource(DexScreenerClient())
        try:
            summary["reprice"] = tracker.reprice_due(source)
        except Exception:
            log.exception("re-pricing failed")
            summary["reprice"] = {"error": True}
        summary["labels_written"] = len(tracker.refresh_labels())

        # The forward social series, and the reason this runs on the same schedule
        # as everything else rather than on its own: it is the only part of the
        # dataset that cannot be reconstructed later. On-chain history is
        # backfillable from Bitquery, Dune or Helius; a Telegram member count at
        # 14:00 last Tuesday is archived nowhere (BUILD_BRIEF.md section 1). An
        # hour this does not run is an hour that stays empty forever, which is why
        # a failure here is logged and swallowed rather than allowed to end the
        # cycle -- and why a failed poll is written as a row with `error` set
        # instead of skipped. The gap is data too.
        #
        # No credentials: t.me serves a public preview carrying the member count.
        # That is also its limit -- message rate and unique speakers need a real
        # client, and prompts/score.md pillar B cares about the speaker ratio far
        # more than about raw membership. This collects the number that is free.
        if with_social:
            collector = TelegramCollector(
                store, client=telegram_client or TelegramPreviewClient()
            )
            try:
                written = collector.run(limit=score_limit)
            except Exception:
                log.exception("telegram collection failed")
                written = []
            late = sum(
                1
                for o in written
                if o.age_minutes is not None
                and o.age_minutes - o.offset_minutes > OFFSET_TOLERANCE_MINUTES
            )
            summary["social_tg"] = {
                "observations": len(written),
                "with_counts": sum(1 for o in written if o.error is None),
                "no_handle": sum(
                    1 for o in written if o.error and "no telegram handle" in o.error
                ),
                # Filed under an offset they missed. Expected on the first cycle
                # after this is switched on, because every existing snapshot is
                # already past t+0; a steady stream of them later means the
                # schedule is slipping.
                "late": late,
            }

        # X, on the same schedule and for the same reason -- and off unless a
        # credential is present, because it is the one metered source here.
        #
        # Why it is worth a credential at all, in one number from this repo's own
        # data: 88% of the tokens collected so far declare an X account. The
        # published 17.4x graduation lift on declared socials is measured over the
        # launch population, where most tokens declare nothing; by the time a token
        # is above $250k the boolean is nearly constant and carries almost no
        # information (calibration/backtest.py: 88% tie mass, AUC 0.48 over 239
        # resolved tokens -- the figure barely moved when the sample tripled).
        # What is
        # left to learn is the *series* -- mentions per hour, unique authors, reply
        # ratio -- which is Pillar A, the largest prior weight in the vector at
        # 0.28, and which resolves on exactly zero rows today.
        #
        # See docs/x-investigation.md for the cost side and the decision.
        if with_social and (x_client or os.environ.get("X_BEARER_TOKEN")):
            client = x_client or XClient(bearer_token=os.environ["X_BEARER_TOKEN"])
            x_collector = XCollector(store, client)
            try:
                x_written = x_collector.run(
                    limit=score_limit, max_searches=x_max_searches
                )
            except Exception:
                log.exception("x collection failed")
                x_written = []
            summary["social_x"] = {
                "observations": len(x_written),
                "with_counts": sum(1 for o in x_written if o.error is None),
                "with_errors": sum(1 for o in x_written if o.error is not None),
                "skipped_for_budget": x_collector.skipped_for_budget,
                "budget": x_max_searches,
            }
        elif with_social:
            # Stated in the summary rather than silently absent. A social series
            # that is not being collected and a social series of zeroes look the
            # same in a row count afterwards.
            summary["social_x"] = {"skipped": "X_BEARER_TOKEN is not set"}

        runner = ScoringRunner(
            store,
            safety_source=safety_source
            or (SafetySource() if with_safety else None),
        )
        scored = runner.run(limit=score_limit, regime=regime)
        summary["scored"] = {
            "rows": len(scored.rows),
            "safety_known": len(runner.last_safety_reports),
            # Only the tokens actually looked up this cycle. The rest reused a
            # verdict an earlier run established, which is why a steady-state cycle
            # costs a handful of requests rather than a sweep of the whole table.
            "safety_fetched": len(runner.last_safety_fetched),
            **scored.exclusion_summary(),
        }

        if export_to:
            from export_web import build_payload

            payload = build_payload(store)
            out = Path(export_to)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
            summary["exported"] = {"path": str(out), "tokens": len(payload["tokens"])}

        if dataset is not None:
            written = dataset.record(store, mirror_dir, dataset_id)
            neon.write_manifest(state_dir, dataset.counts(), written, dataset_id)
            summary["journalled"] = written
        else:
            summary["journalled"] = journal.sync(store, state_dir)
        summary["totals"] = {
            "snapshots": store.snapshot_count(),
            "scores": store.score_count(),
            "outcome_observations": store.outcome_observation_count(),
            "safety_observations": store.safety_observation_count(),
            "labelled_snapshots": store.labelled_snapshot_count(),
            "chain_breakdown": store.chain_breakdown(),
        }
    finally:
        store.close()
        if dataset is not None and neon_journal is None:
            dataset.close()

    summary["seconds"] = round(time.time() - started, 1)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="collect",
        description=(
            "One Phase 0 collection cycle: restore, poll, re-price, label, score, "
            "export, journal. Designed to be run on a schedule."
        ),
    )
    parser.add_argument(
        "--chains",
        default=",".join(chains.default_chain_names()),
        help=f"comma-separated (supported: {', '.join(chains.supported_names())})",
    )
    parser.add_argument("--state", default=DEFAULT_STATE_DIR, help="journal directory")
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            f"keep the dataset in Neon/Postgres (default: {neon.ENV_VAR}); without "
            "it, state/ is the journal"
        ),
    )
    parser.add_argument(
        "--mirror",
        default=str(neon.DEFAULT_MIRROR_DIR),
        help="local cache of the Neon dataset, kept between runs (Neon mode only)",
    )
    parser.add_argument("--db", default=DEFAULT_WORKING_DB, help="working DuckDB path")
    parser.add_argument("--regime", choices=["hot", "neutral", "cold"])
    parser.add_argument(
        "--no-safety", action="store_true", help="skip the GoPlus/RugCheck lookups"
    )
    parser.add_argument(
        "--no-social",
        action="store_true",
        help=(
            "skip the Telegram preview poll. Nothing else replaces it: member "
            "counts at a past timestamp are archived nowhere, so a skipped cycle "
            "is a permanent hole in the series."
        ),
    )
    parser.add_argument("--max-tokens-per-poll", type=int, default=120)
    parser.add_argument(
        "--x-max-searches",
        type=int,
        # Resolved after load_config(), not here: an argparse default is evaluated
        # before .env is loaded, which honoured X_BEARER_TOKEN from .env and dropped
        # the budget set beside it -- an uncapped metered source.
        default=None,
        help=(
            "cap the X searches one cycle may make (default: "
            "X_MAX_SEARCHES_PER_CYCLE, else unlimited). Every other source here is "
            "keyless and free; this one is metered per post read."
        ),
    )
    parser.add_argument("--score-limit", type=int, default=200)
    parser.add_argument(
        "--export",
        nargs="?",
        const="web/screener-data.json",
        help="also write the web export (default path: web/screener-data.json)",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="print the journal row counts and stop",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    load_config()  # loads .env if present; nothing here needs a credential

    if args.summary_only:
        print(json.dumps(journal.summarise(args.state), indent=2))
        return 0

    try:
        x_max_searches = (
            args.x_max_searches
            if args.x_max_searches is not None
            else max_searches_from_env()
        )
        summary = run_cycle(
            chain_names=args.chains.split(","),
            state_dir=args.state,
            db_path=args.db,
            regime=args.regime,
            with_safety=not args.no_safety,
            with_social=not args.no_social,
            max_tokens_per_poll=args.max_tokens_per_poll,
            score_limit=args.score_limit,
            export_to=args.export,
            x_max_searches=x_max_searches,
            # Resolved after load_config() for the same reason as the X budget: a
            # URL kept in .env must be honoured. Blank means the git journal.
            database_url=args.database_url or os.environ.get(neon.ENV_VAR) or None,
            mirror_dir=args.mirror,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
