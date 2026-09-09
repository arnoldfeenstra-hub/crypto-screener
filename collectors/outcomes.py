"""Phase 0 item 4 -- outcome tracker.

BUILD_BRIEF.md section 3: "Re-price every snapshotted token on a schedule and fill
the label columns from section 2."

Two layers, split so the part that matters is testable without a network:

* :func:`compute_labels` -- pure. Takes a baseline and a price path, returns the
  section 2 labels. Every rule about what is and is not knowable lives here.
* :class:`OutcomeTracker` -- appends re-price observations and derived labels to the
  store, and decides which tokens are due.

Design constraints this module is built around
----------------------------------------------
**Labels are inserts, not updates** (hard rule 1). A label row never overwrites the
snapshot and never overwrites an earlier label row; recomputing at a later horizon
appends a new row, and reads take the most recent. The price path is kept too, in
``outcome_observations``, so any label can be re-derived from the evidence that
produced it after a formula change.

**All five horizons, decided later** (section 2). The brief is explicit that the
horizon is not chosen up front. Nothing here privileges one.

**A horizon that has not elapsed is null, not zero.** A ``max_multiple_7d`` computed
from two hours of data would be the most damaging kind of wrong number: plausible,
pessimistic, and silently mixed in with real ones. So a label is only produced once
the window has closed *and* an observation exists inside it.

**Drawdown is measured from the snapshot, not from the peak.** The brief's reason
for the column is that "a token that 5x'd after first going -60% is untradeable in
practice" -- that is drawdown relative to where the row entered the dataset, which
is the only reference a backtest starting at the snapshot can use.

**Dead tokens are kept.** Survival is a label, not a filter (hard rule 1). A token
that went to zero produces a complete, valuable row.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from collectors.config import load_config
from collectors.metrics import TokenMetrics
from collectors.schema import now_ms
from collectors.store import Store

log = logging.getLogger("outcomes")

PHASE = "0.4"

HORIZONS_MINUTES: dict[str, int] = {
    "1h": 60,
    "6h": 360,
    "24h": 1440,
    "72h": 4320,
    "7d": 10080,
}

# "Survived" means still above 20% of the mcap recorded at snapshot time
# (BUILD_BRIEF.md section 2).
SURVIVAL_FLOOR_RATIO = 0.20

SURVIVAL_HORIZONS = ("24h", "7d")
DRAWDOWN_HORIZONS = ("24h", "72h")

# How often to re-price, by age. Attention decays fast, so the early path needs
# resolution the late path does not.
REPRICE_SCHEDULE_MINUTES: tuple[tuple[int, int], ...] = (
    (60, 5),        # first hour: every 5 minutes
    (360, 15),      # to 6h: every 15
    (1440, 60),     # to 24h: hourly
    (10080, 360),   # to 7d: every 6h
)


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """One re-price of one snapshotted token."""

    snapshot_id: str
    ts: int
    mcap_usd: float | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    holder_count: int | None = None
    source: str = "unknown"
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def minutes_since(self, snapshot_ts: int) -> int:
        return int((self.ts - snapshot_ts) // 60_000)

    def to_row(self, snapshot_ts: int) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "snapshot_id": self.snapshot_id,
            "ts": self.ts,
            "minutes_since_snapshot": self.minutes_since(snapshot_ts),
            "mcap_usd": self.mcap_usd,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "holder_count": self.holder_count,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class Labels:
    """The section 2 forward labels. Every field is ``None`` until it is knowable."""

    snapshot_id: str
    filled_at_ms: int
    source: str = "outcomes"
    label_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    max_multiple_1h: float | None = None
    max_multiple_6h: float | None = None
    max_multiple_24h: float | None = None
    max_multiple_72h: float | None = None
    max_multiple_7d: float | None = None
    max_drawdown_before_peak_24h: float | None = None
    max_drawdown_before_peak_72h: float | None = None
    time_to_peak_minutes: int | None = None
    survived_24h: bool | None = None
    survived_7d: bool | None = None

    @property
    def complete(self) -> bool:
        """True once the widest horizon has resolved. Phase 0's exit counts these."""
        return self.max_multiple_7d is not None and self.survived_7d is not None

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        return row

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _usable(observations: Iterable[PriceObservation]) -> list[PriceObservation]:
    """Observations that carry a market cap, in time order.

    An observation with no mcap is kept in the store -- it is evidence that a
    re-price was attempted and came back empty -- but it cannot contribute to a
    multiple, and treating it as zero would manufacture a total loss.
    """
    return sorted(
        (o for o in observations if o.mcap_usd is not None), key=lambda o: o.ts
    )


def compute_labels(
    snapshot_id: str,
    snapshot_ts: int,
    baseline_mcap: float | None,
    observations: Iterable[PriceObservation],
    *,
    as_of_ms: int | None = None,
    source: str = "outcomes",
) -> Labels:
    """Derive the section 2 labels from a price path. Pure.

    ``as_of_ms`` is when the computation happens; a horizon that has not elapsed by
    then yields ``None`` rather than a number computed from a partial window.
    """
    as_of = as_of_ms if as_of_ms is not None else now_ms()
    empty = Labels(snapshot_id=snapshot_id, filled_at_ms=as_of, source=source)

    # Without a baseline there is nothing to be a multiple *of*. A zero baseline is
    # not a baseline either -- dividing by it would produce an infinite multiple for
    # any token that traded at all.
    if baseline_mcap is None or baseline_mcap <= 0:
        return empty

    path = _usable(observations)
    if not path:
        return empty

    values: dict[str, Any] = {}

    for name, minutes in HORIZONS_MINUTES.items():
        window_end = snapshot_ts + minutes * 60_000
        if as_of < window_end:
            continue  # the window is still open; nothing knowable yet
        window = [o for o in path if snapshot_ts <= o.ts <= window_end]
        if not window:
            continue  # elapsed, but never observed: unknown, not 1.0
        peak = max(window, key=lambda o: o.mcap_usd)  # type: ignore[arg-type,return-value]
        values[f"max_multiple_{name}"] = peak.mcap_usd / baseline_mcap  # type: ignore[operator]

        if name in DRAWDOWN_HORIZONS:
            before_peak = [o for o in window if o.ts <= peak.ts]
            trough = min(before_peak, key=lambda o: o.mcap_usd)  # type: ignore[arg-type,return-value]
            # Positive fraction: 0.6 means it traded 60% below the snapshot before
            # reaching its peak. Clamped at 0 so a token that only ever rose reads
            # as no drawdown rather than a negative one.
            values[f"max_drawdown_before_peak_{name}"] = max(
                0.0, 1.0 - trough.mcap_usd / baseline_mcap  # type: ignore[operator]
            )

        if name in SURVIVAL_HORIZONS:
            # The last observation inside the window is the state at the horizon.
            last = window[-1]
            values[f"survived_{name}"] = (
                last.mcap_usd >= baseline_mcap * SURVIVAL_FLOOR_RATIO  # type: ignore[operator]
            )

    # Time to peak is measured over the widest window that has actually closed, so
    # it never claims a peak from a period nobody has observed yet.
    closed = [
        m for name, m in HORIZONS_MINUTES.items() if as_of >= snapshot_ts + m * 60_000
    ]
    if closed:
        widest_end = snapshot_ts + max(closed) * 60_000
        window = [o for o in path if snapshot_ts <= o.ts <= widest_end]
        if window:
            peak = max(window, key=lambda o: o.mcap_usd)  # type: ignore[arg-type,return-value]
            values["time_to_peak_minutes"] = peak.minutes_since(snapshot_ts)

    return Labels(snapshot_id=snapshot_id, filled_at_ms=as_of, source=source, **values)


def due_for_repricing(minutes_since_snapshot: int, minutes_since_last: int | None) -> bool:
    """Whether a token is due a re-price, given its age and time since last look."""
    if minutes_since_snapshot > max(HORIZONS_MINUTES.values()):
        return False  # past 7d, every label has resolved
    if minutes_since_last is None:
        return True
    for age_limit, interval in REPRICE_SCHEDULE_MINUTES:
        if minutes_since_snapshot <= age_limit:
            return minutes_since_last >= interval
    return False


class OutcomeTracker:
    """Re-prices snapshotted tokens and appends their labels."""

    def __init__(self, store: Store, *, source: str = "outcomes") -> None:
        self.store = store
        self.source = source

    def record(self, observations: Iterable[PriceObservation]) -> int:
        return self.store.append_outcome_observations(observations)

    def record_from_metrics(self, snapshot_id: str, metrics: TokenMetrics) -> int:
        return self.record(
            [
                PriceObservation(
                    snapshot_id=snapshot_id,
                    ts=metrics.observed_at_ms,
                    mcap_usd=metrics.mcap_usd,
                    price_usd=metrics.price_usd,
                    liquidity_usd=metrics.liquidity_usd,
                    volume_24h_usd=metrics.volume_24h_usd,
                    holder_count=metrics.holder_count,
                    source=metrics.source,
                )
            ]
        )

    def refresh_labels(self, *, as_of_ms: int | None = None) -> list[Labels]:
        """Recompute and append labels for every snapshot with a price path."""
        as_of = as_of_ms if as_of_ms is not None else now_ms()
        written: list[Labels] = []
        for snap in self.store.snapshots_for_labelling():
            observations = self.store.outcome_observations(snap["snapshot_id"])
            labels = compute_labels(
                snap["snapshot_id"],
                snap["ts"],
                snap["market_mcap_usd"],
                observations,
                as_of_ms=as_of,
                source=self.source,
            )
            if all(
                getattr(labels, f) is None
                for f in labels.__dataclass_fields__
                if f.startswith(("max_", "survived_", "time_"))
            ):
                continue  # nothing knowable yet; do not write an empty row
            self.store.append_labels(labels)
            written.append(labels)
        return written

    def due(self, *, as_of_ms: int | None = None) -> list[dict[str, Any]]:
        as_of = as_of_ms if as_of_ms is not None else now_ms()
        out = []
        for snap in self.store.snapshots_for_labelling():
            age = int((as_of - snap["ts"]) // 60_000)
            last = self.store.last_observation_ts(snap["snapshot_id"])
            since_last = None if last is None else int((as_of - last) // 60_000)
            if due_for_repricing(age, since_last):
                out.append(snap)
        return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="outcomes",
        description="Phase 0 item 4. Re-price snapshotted tokens and fill forward labels.",
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument(
        "--refresh-labels",
        action="store_true",
        help="recompute labels from the stored price paths and append them",
    )
    parser.add_argument("--due", action="store_true", help="list tokens due a re-price")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    config = load_config()
    db_path = args.db or str(config.db_path)

    with Store(db_path) as store:
        tracker = OutcomeTracker(store)
        payload: dict[str, Any] = {"db": db_path}
        if args.due:
            due = tracker.due()
            payload["due_for_repricing"] = len(due)
            payload["contracts"] = [d["contract"] for d in due[:20]]
        if args.refresh_labels:
            written = tracker.refresh_labels()
            payload["labels_written"] = len(written)
            payload["complete_7d"] = sum(1 for label in written if label.complete)
        if not args.due and not args.refresh_labels:
            payload["labelled_snapshots"] = store.labelled_snapshot_count()
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
