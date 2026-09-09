"""Phase 0 item 1 -- the trigger watcher.

BUILD_BRIEF.md section 3:

    Subscribe to new pools on target chains. Fire a snapshot the moment a token
    first crosses the trigger: *either* $250k mcap *or* 500 holders, whichever
    first. Record which trigger fired. Same rule for every token, no exceptions.

"Same rule for every token, no exceptions" is the load-bearing sentence, because
the whole calibration design rests on a lifecycle-matched sample: every token
captured at the same point in its life. A per-token exception, a manual override, a
"this one looks interesting so grab it early" -- any of those silently turns the
dataset into the thing CLAUDE.md warns about, a comparison of winners at peak
against losers at launch.

So the rule is a pure function of exactly two numbers:

    evaluate(mcap_usd, holder_count) -> TriggerDecision

It cannot see the ticker, the chain, the deployer, the social profile, or the
clock, because it is not given them. There is no allowlist, no skiplist, and no
threshold parameter anywhere in this module. Changing a threshold means editing a
module constant and bumping the schema, which is a visible commit, not a runtime
flag someone can pass on a Tuesday.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from collectors.config import load_config
from collectors.metrics import Observation, TokenMetrics
from collectors.snapshot import build_snapshot
from collectors.store import AppendOnlyViolation, Store, TriggerEvent

log = logging.getLogger("trigger_watcher")

# The trigger. One threshold pair, applied to every token on every chain.
TRIGGER_MCAP_USD: Final[float] = 250_000.0
TRIGGER_HOLDER_COUNT: Final[int] = 500

# The two members of the section 4 `trigger` enum.
TRIGGER_MCAP: Final[str] = "mcap_250k"
TRIGGER_HOLDERS: Final[str] = "holders_500"


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """Whether a token crossed, and on which condition."""

    fired: bool
    trigger: str | None
    mcap_crossed: bool
    holders_crossed: bool

    @property
    def both_crossed(self) -> bool:
        return self.mcap_crossed and self.holders_crossed


def _crossed(value: float | int | None, threshold: float) -> bool:
    """A threshold test that treats missing data as missing, never as zero.

    ``None`` does not cross. NaN and infinity do not cross either: they are what a
    broken upstream response looks like, not measurements. This is hard rule 3 at
    the one place where imputing a zero would quietly change which tokens enter
    the dataset.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    number = float(value)
    if not math.isfinite(number):
        return False
    return number >= threshold


def evaluate(mcap_usd: float | None, holder_count: int | None) -> TriggerDecision:
    """The trigger rule. A pure function of two numbers, identical for every token.

    Fires on ``mcap_usd >= 250_000`` or ``holder_count >= 500``, whichever the
    watcher sees first.

    When a single observation shows both conditions already met -- which happens
    when a token crosses between two polls, or when a backfill hands us a token
    long past both thresholds -- "whichever first" is unanswerable from the data,
    so ``trigger`` is set to ``mcap_250k`` by a fixed tie-break and both crossing
    flags are recorded on the row. The tie-break is arbitrary but constant; what
    matters is that it is not per-token, and that ``trigger_holders_crossed``
    preserves what actually happened.
    """
    mcap_crossed = _crossed(mcap_usd, TRIGGER_MCAP_USD)
    holders_crossed = _crossed(holder_count, TRIGGER_HOLDER_COUNT)
    if mcap_crossed:
        trigger = TRIGGER_MCAP
    elif holders_crossed:
        trigger = TRIGGER_HOLDERS
    else:
        trigger = None
    return TriggerDecision(
        fired=trigger is not None,
        trigger=trigger,
        mcap_crossed=mcap_crossed,
        holders_crossed=holders_crossed,
    )


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

    def offer(self, metrics: TokenMetrics):
        """Consider one observation. Returns the written Snapshot, or ``None``."""
        self.stats.observed += 1
        key = (metrics.chain, metrics.contract)

        if key in self._fired or self.store.has_triggered(*key):
            self._fired.add(key)
            self.stats.already_triggered += 1
            return None

        decision = evaluate_metrics(metrics)
        if not decision.fired:
            return None

        snapshot = build_snapshot(metrics, decision, source=self.source, regime=self.regime)
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
        return [snap for m in batch if (snap := self.offer(m)) is not None]

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
    if args.replay:
        from collectors.bitquery import ReplayFeed

        return ReplayFeed.from_path(Path(args.replay), chain=args.chain)
    from collectors.bitquery import BitqueryClient, BitqueryFeed

    client = BitqueryClient(
        token=config.require_bitquery(), endpoint=config.bitquery_endpoint
    )
    return BitqueryFeed(client, chain=args.chain, lookback_minutes=args.lookback_minutes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trigger-watcher",
        description=(
            "Phase 0 item 1. Watch new pools and snapshot each token once, the first "
            "time it crosses $250k mcap or 500 holders."
        ),
    )
    parser.add_argument("--chain", default="solana", help="chain to watch (default: solana)")
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
    except RuntimeError as exc:
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
            "chain": args.chain,
            "snapshots_total": 0 if args.dry_run else store.snapshot_count(),
            "trigger_breakdown": {} if args.dry_run else store.trigger_breakdown(),
            **stats.as_dict(),
        }
        if args.export_parquet and not args.dry_run:
            summary["parquet_export"] = str(store.export_parquet(config.parquet_dir))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
