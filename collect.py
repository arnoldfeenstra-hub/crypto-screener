"""One collection cycle, start to finish. The thing a scheduler calls.

BUILD_BRIEF.md section 3 says to run Phase 0 for four to six weeks. Nothing was
running it: every command in the repo did one step and expected a human to run the
next. This is the whole loop as a single entrypoint, so a cron line or a GitHub
Actions schedule is enough to actually accumulate the dataset.

    journal -> database -> poll -> re-price -> label -> score -> export -> journal

The first and last steps are what make it schedulable. State lives in an
append-only JSONL journal (``collectors/journal.py``) that git can hold, is
rebuilt into DuckDB at the start of a run, and is appended to at the end. A run
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
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from collectors import chains, journal
from collectors.config import load_config
from collectors.dexscreener import DexScreenerClient, DexScreenerFeed, DexScreenerPriceSource
from collectors.outcomes import OutcomeTracker
from collectors.safety import SafetySource
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
    max_tokens_per_poll: int = 120,
    score_limit: int = 200,
    export_to: str | Path | None = None,
    feed: Any = None,
    price_source: Any = None,
    safety_source: Any = None,
) -> dict[str, Any]:
    """Run one full cycle and return a summary. Every source is injectable for tests."""
    started = time.time()
    resolved = chains.resolve_requested(list(chain_names))

    working = Path(db_path)
    if working.exists():
        # A leftover working copy from a crashed run. The journal is the dataset;
        # rebuilding from it is always correct, so the stale copy is not consulted.
        working.unlink()

    store = Store(working)
    summary: dict[str, Any] = {"chains": resolved, "regime": regime}
    try:
        summary["restored"] = journal.restore(store, state_dir)

        feed = feed or DexScreenerFeed.for_chains(
            resolved, client=DexScreenerClient(), max_tokens_per_poll=max_tokens_per_poll
        )
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
        default=",".join(chains.DEFAULT_CHAINS),
        help=f"comma-separated (supported: {', '.join(chains.supported_names())})",
    )
    parser.add_argument("--state", default=DEFAULT_STATE_DIR, help="journal directory")
    parser.add_argument("--db", default=DEFAULT_WORKING_DB, help="working DuckDB path")
    parser.add_argument("--regime", choices=["hot", "neutral", "cold"])
    parser.add_argument(
        "--no-safety", action="store_true", help="skip the GoPlus/RugCheck lookups"
    )
    parser.add_argument("--max-tokens-per-poll", type=int, default=120)
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
        summary = run_cycle(
            chain_names=args.chains.split(","),
            state_dir=args.state,
            db_path=args.db,
            regime=args.regime,
            with_safety=not args.no_safety,
            max_tokens_per_poll=args.max_tokens_per_poll,
            score_limit=args.score_limit,
            export_to=args.export,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
