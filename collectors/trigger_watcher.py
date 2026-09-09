"""Phase 0 item 1 -- the trigger watcher.

BUILD_BRIEF.md section 3:

    Subscribe to new pools on target chains. Fire a snapshot the moment a token
    first crosses the trigger: *either* $250k mcap *or* 500 holders, whichever
    first. Record which trigger fired. Same rule for every token, no exceptions.

The rule itself lives in ``collectors/trigger_rule.py`` and is imported, not
restated -- one function object, shared by the watcher, the backfill and the
serverless live endpoint, so the three cannot drift apart. This module is the loop
around it: polling a feed, deciding what is new, and writing what fires.

The watcher can hold several chains at once. That changes nothing about the rule,
which never sees the chain; it only means one process fills the cross-chain sample
instead of one process per chain.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from collectors import chains
from collectors import mindshare as mindshare_mod
from collectors.config import load_config
from collectors.metrics import Observation, TokenMetrics
from collectors.snapshot import build_snapshot
from collectors.store import AppendOnlyViolation, Store, TriggerEvent

# Re-exported so every existing import site keeps working and keeps getting the
# *same* function object. tests/test_social_and_backfill.py asserts that identity.
from collectors.trigger_rule import (
    TRIGGER_HOLDER_COUNT,
    TRIGGER_HOLDERS,
    TRIGGER_MCAP,
    TRIGGER_MCAP_USD,
    TriggerDecision,
    evaluate,
)

__all__ = [
    "TRIGGER_HOLDERS",
    "TRIGGER_HOLDER_COUNT",
    "TRIGGER_MCAP",
    "TRIGGER_MCAP_USD",
    "TriggerDecision",
    "TriggerWatcher",
    "WatchStats",
    "evaluate",
    "evaluate_metrics",
    "evaluate_observation",
    "main",
]

log = logging.getLogger("trigger_watcher")


def evaluate_observation(observation: Observation) -> TriggerDecision:
    """Apply :func:`evaluate` to an observation, passing only the two trigger inputs."""
    return evaluate(observation.mcap_usd, observation.holder_count)


def evaluate_metrics(metrics: TokenMetrics) -> TriggerDecision:
    """Apply :func:`evaluate` to a source record."""
    return evaluate_observation(metrics.observation())


class Feed(Protocol):
    """A source of token observations. Implemented by BitqueryFeed and ReplayFeed."""

    def poll(self) -> Sequence[TokenMetrics]: ...


@dataclass
class WatchStats:
    cycles: int = 0
    observed: int = 0
    fired: int = 0
    already_triggered: int = 0
    races_lost: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "cycles": self.cycles,
            "observed": self.observed,
            "fired": self.fired,
            "already_triggered": self.already_triggered,
            "races_lost": self.races_lost,
        }


class TriggerWatcher:
    """Watches a feed and fires exactly one snapshot per token, at first crossing.

    Restart-safe with no state file: "has this token already fired" is a lookup in
    the append-only ``trigger_events`` table, and the database's uniqueness
    constraint is the final word if two watchers race.
    """

    def __init__(
        self,
        store: Store,
        *,
        source: str,
        regime: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self.store = store
        self.source = source
        # Regime is a per-batch property of the tape, supplied by the operator or
        # later by a LunarCrush read. It is recorded, never inferred per token.
        self.regime = regime
        self.dry_run = dry_run
        self.stats = WatchStats()
        # Positive-only cache: an entry means "definitely fired". A miss falls
        # through to the database, so a token another process just wrote is never
        # mistaken for a new one.
        self._fired: set[tuple[str, str]] = set()

    def offer(self, metrics: TokenMetrics, mindshare=None):
        """Consider one observation. Returns the written Snapshot, or ``None``.

        ``mindshare`` is supplied by :meth:`process`, which holds the whole batch
        and can therefore compute a share. It is never derived here from one token,
        because a share of a universe of one is 100% and means nothing.
        """
        self.stats.observed += 1
        key = (metrics.chain, metrics.contract)

        if key in self._fired or self.store.has_triggered(*key):
            self._fired.add(key)
            self.stats.already_triggered += 1
            return None

        decision = evaluate_metrics(metrics)
        if not decision.fired:
            return None

        snapshot = build_snapshot(
            metrics,
            decision,
            source=self.source,
            regime=self.regime,
            mindshare=mindshare,
        )
        event = TriggerEvent(
            chain=metrics.chain,
            contract=metrics.contract,
            trigger_kind=snapshot.trigger,
            trigger_mcap_crossed=decision.mcap_crossed,
            trigger_holders_crossed=decision.holders_crossed,
            source=self.source,
            mcap_usd_at_trigger=metrics.mcap_usd,
            holder_count_at_trigger=metrics.holder_count,
            snapshot_id=snapshot.snapshot_id,
            ts=snapshot.ts,
        )

        if self.dry_run:
            log.info(
                "would fire %s %s trigger=%s mcap=%s holders=%s",
                metrics.chain,
                metrics.contract,
                snapshot.trigger,
                metrics.mcap_usd,
                metrics.holder_count,
            )
            self.stats.fired += 1
            self._fired.add(key)
            return snapshot

        try:
            self.store.append_trigger(snapshot, event)
        except AppendOnlyViolation:
            # Another watcher got there first. The existing row stands; nothing is
            # overwritten, and this one is simply not our write.
            self._fired.add(key)
            self.stats.races_lost += 1
            log.warning("lost trigger race for %s:%s", metrics.chain, metrics.contract)
            return None

        self._fired.add(key)
        self.stats.fired += 1
        log.info(
            "fired %s %s trigger=%s mcap=%s holders=%s completeness=%.2f",
            metrics.chain,
            metrics.contract,
            snapshot.trigger,
            metrics.mcap_usd,
            metrics.holder_count,
            snapshot.to_row()["data_completeness"],
        )
        return snapshot

    def process(self, batch: Iterable[TokenMetrics]) -> list:
        """Offer a whole poll to the trigger, with mindshare measured across it.

        The batch is the measurement universe. It is computed over *every* token
        polled, not only the ones that fire: a share whose denominator was the
        already-filtered set would be a share of the survivors, which is the
        selection effect prompts/score.md step 5 exists to warn about.
        """
        tokens = list(batch)
        shares = mindshare_mod.compute(tokens) if tokens else {}
        return [
            snap
            for m in tokens
            if (snap := self.offer(m, shares.get((m.chain, m.contract)))) is not None
        ]

    def run(
        self,
        feed: Feed,
        *,
        poll_seconds: int = 60,
        max_cycles: int | None = None,
    ) -> WatchStats:
        """Poll until ``max_cycles`` is reached, or forever if it is ``None``."""
        try:
            while max_cycles is None or self.stats.cycles < max_cycles:
                self.stats.cycles += 1
                try:
                    batch = feed.poll()
                except Exception:
                    # A source outage must not lose the run. The next cycle
                    # re-observes every token that has not yet fired, so a missed
                    # poll delays a snapshot, it does not skip one.
                    log.exception("poll failed on cycle %d", self.stats.cycles)
                    batch = []
                self.process(batch)
                if max_cycles is None or self.stats.cycles < max_cycles:
                    time.sleep(poll_seconds)
        except KeyboardInterrupt:
            log.info("interrupted; %s", self.stats.as_dict())
        return self.stats


def _build_feed(args: argparse.Namespace, config) -> Feed:
    """Pick the source. DexScreener is the default because it needs no credential.

    ``--replay`` is a recorded fixture and produces synthetic rows; the export
    marks them so nobody reads a replay as a collection run.
    """
    requested = [c.strip() for c in args.chains.split(",") if c.strip()]

    if args.replay:
        from collectors.bitquery import ReplayFeed

        return ReplayFeed.from_path(Path(args.replay), chain=requested[0])

    if args.source == "bitquery":
        from collectors.bitquery import BitqueryClient, BitqueryFeed

        client = BitqueryClient(
            token=config.require_bitquery(), endpoint=config.bitquery_endpoint
        )
        return BitqueryFeed(
            client, chain=requested[0], lookback_minutes=args.lookback_minutes
        )

    from collectors.dexscreener import DexScreenerFeed

    return DexScreenerFeed.for_chains(
        requested, max_tokens_per_poll=args.max_tokens_per_poll
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trigger-watcher",
        description=(
            "Phase 0 item 1. Watch new pools and snapshot each token once, the first "
            "time it crosses $250k mcap or 500 holders."
        ),
    )
    parser.add_argument(
        "--chains",
        default=",".join(chains.DEFAULT_CHAINS),
        help=(
            "comma-separated chains to watch (default: "
            f"{','.join(chains.DEFAULT_CHAINS)}). Supported by the DexScreener "
            f"source: {', '.join(chains.supported_names())}"
        ),
    )
    parser.add_argument(
        "--chain",
        dest="chains",
        help="single chain; alias for --chains, kept for older invocations",
    )
    parser.add_argument(
        "--source",
        choices=["dexscreener", "bitquery"],
        default="dexscreener",
        help="live source (default: dexscreener -- real data, no API key)",
    )
    parser.add_argument(
        "--max-tokens-per-poll",
        type=int,
        default=120,
        help="cap on tokens looked up per poll (DexScreener source)",
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--poll-seconds", type=int, help="override WATCHER_POLL_SECONDS")
    parser.add_argument(
        "--cycles", type=int, default=None, help="stop after N polls (default: run forever)"
    )
    parser.add_argument("--once", action="store_true", help="shorthand for --cycles 1")
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=180,
        help="how far back to ask the source for pools each poll (default: 180)",
    )
    parser.add_argument(
        "--replay",
        help="run from a recorded JSON fixture instead of the network; needs no API key",
    )
    parser.add_argument(
        "--regime",
        choices=["hot", "neutral", "cold"],
        help="tape regime for this batch, recorded on every row",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="evaluate and log, write nothing"
    )
    parser.add_argument("--export-parquet", action="store_true", help="export after the run")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    config = load_config()
    db_path = args.db or (":memory:" if args.dry_run else str(config.db_path))
    cycles = 1 if args.once else args.cycles
    # `or` would swallow --poll-seconds 0, which is the useful value for replays.
    poll_seconds = args.poll_seconds if args.poll_seconds is not None else config.poll_seconds

    try:
        feed = _build_feed(args, config)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with Store(db_path) as store:
        watcher = TriggerWatcher(
            store,
            source=getattr(feed, "source_name", "unknown"),
            regime=args.regime,
            dry_run=args.dry_run,
        )
        stats = watcher.run(feed, poll_seconds=poll_seconds, max_cycles=cycles)
        summary = {
            "db": db_path,
            "chains": list(
                getattr(feed, "chain_names", None)
                or [c.strip() for c in args.chains.split(",") if c.strip()]
            ),
            "source": getattr(feed, "source_name", "unknown"),
            "snapshots_total": 0 if args.dry_run else store.snapshot_count(),
            "trigger_breakdown": {} if args.dry_run else store.trigger_breakdown(),
            "chain_breakdown": {} if args.dry_run else store.chain_breakdown(),
            **stats.as_dict(),
        }
        if args.export_parquet and not args.dry_run:
            summary["parquet_export"] = str(store.export_parquet(config.parquet_dir))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
