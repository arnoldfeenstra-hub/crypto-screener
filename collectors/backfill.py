"""Phase 0 item 5 -- on-chain backfill.

BUILD_BRIEF.md section 3: "Separately, pull the on-chain-only history from Bitquery
for the last 90 days. This gives you a large labeled set with no social features --
useful on its own for fitting the structural pillars while social accumulates."

The one thing that must not be got wrong
----------------------------------------
A backfilled row is snapshotted at the **historical** moment of first crossing,
reconstructed from the archive -- not at the moment the backfill happens to run.
Snapshotting a 60-day-old token at today's price would place it in the sample at a
completely different point in its life than every live row, which is exactly the
comparison the cohort design exists to prevent (hard rule 2, and prompts/score.md
step 5: "comparing a winner at its peak against a loser at launch teaches the model
nothing").

:func:`reconstruct_trigger` is the pure core of that, and it is what the tests
exercise. It walks a time-ordered history and returns the *first* point that
crosses -- applying exactly the same rule as the live watcher, imported from it
rather than reimplemented, so the two can never drift apart.

Backfilled rows carry ``source='bitquery_backfill'`` so they can be excluded from
any analysis where their structurally-absent social columns would bias the result.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from collectors.config import load_config
from collectors.metrics import TokenMetrics
from collectors.snapshot import build_snapshot
from collectors.store import AppendOnlyViolation, Store, TriggerEvent
from collectors.trigger_watcher import TriggerDecision, evaluate

log = logging.getLogger("backfill")

PHASE = "0.5"
BACKFILL_SOURCE = "bitquery_backfill"
DEFAULT_LOOKBACK_DAYS = 90


@dataclass(frozen=True, slots=True)
class Crossing:
    """Where in a token's history it first crossed the trigger."""

    metrics: TokenMetrics
    decision: TriggerDecision
    index: int


def reconstruct_trigger(history: Iterable[TokenMetrics]) -> Crossing | None:
    """Find the first point in a token's history that crosses the trigger.

    The history is sorted by observation time first, so an archive that comes back
    in an arbitrary order cannot produce a "first" crossing that is not actually
    first. Returns ``None`` for a token that never crossed -- which is most of
    them, and which is correct: they never entered the live dataset either.

    Uses :func:`collectors.trigger_watcher.evaluate` directly. The backfill must
    apply the identical rule to the live path or the two halves of the sample are
    not comparable, and a reimplementation here would be free to drift.
    """
    ordered = sorted(history, key=lambda m: m.observed_at_ms)
    for index, point in enumerate(ordered):
        decision = evaluate(point.mcap_usd, point.holder_count)
        if decision.fired:
            return Crossing(metrics=point, decision=decision, index=index)
    return None


class BackfillRunner:
    """Reconstructs historical crossings and appends them as snapshots."""

    def __init__(self, store: Store, *, source: str = BACKFILL_SOURCE) -> None:
        self.store = store
        self.source = source
        self.stats = {"considered": 0, "crossed": 0, "written": 0, "already_present": 0}

    def ingest(self, histories: Iterable[Iterable[TokenMetrics]]) -> list[str]:
        """Ingest one history per token. Returns the snapshot ids written."""
        written: list[str] = []
        for history in histories:
            points = list(history)
            if not points:
                continue
            self.stats["considered"] += 1
            crossing = reconstruct_trigger(points)
            if crossing is None:
                continue
            self.stats["crossed"] += 1

            metrics = replace(crossing.metrics, source=self.source)
            snapshot = build_snapshot(metrics, crossing.decision, source=self.source)
            event = TriggerEvent(
                chain=metrics.chain,
                contract=metrics.contract,
                trigger_kind=snapshot.trigger,
                trigger_mcap_crossed=crossing.decision.mcap_crossed,
                trigger_holders_crossed=crossing.decision.holders_crossed,
                source=self.source,
                mcap_usd_at_trigger=metrics.mcap_usd,
                holder_count_at_trigger=metrics.holder_count,
                snapshot_id=snapshot.snapshot_id,
                # The historical crossing time, not now.
                ts=snapshot.ts,
            )
            try:
                self.store.append_trigger(snapshot, event)
            except AppendOnlyViolation:
                # The live watcher already captured this token. Its row stands: it
                # was written from a live observation, which is at least as good as
                # a reconstruction and carries the same lifecycle point.
                self.stats["already_present"] += 1
                continue
            self.stats["written"] += 1
            written.append(snapshot.snapshot_id)
        return written


def histories_from_bitquery(
    client: Any, contracts: Sequence[str], *, lookback_days: int = DEFAULT_LOOKBACK_DAYS
) -> list[list[TokenMetrics]]:
    """Pull per-token history from Bitquery.

    Left thin deliberately: the archive query shape is the part that cannot be
    verified without a live key (see collectors/bitquery.py), so the reconstruction
    logic above is kept independent of it and separately tested.
    """
    from collectors.bitquery import TOKEN_METRICS_QUERY, parse_token_metrics

    out: list[list[TokenMetrics]] = []
    for contract in contracts:
        payload = client.execute(
            TOKEN_METRICS_QUERY,
            {"mints": [contract], "since24h": _iso_since_days(lookback_days)},
        )
        parsed = parse_token_metrics(payload).get(contract)
        if not parsed:
            continue
        out.append(
            [
                TokenMetrics(
                    chain="solana",
                    contract=contract,
                    observed_at_ms=parsed.get("observed_at_ms") or 0,
                    source=BACKFILL_SOURCE,
                    ticker=parsed.get("ticker"),
                    mcap_usd=parsed.get("mcap_usd"),
                    price_usd=parsed.get("price_usd"),
                    volume_24h_usd=parsed.get("volume_24h_usd"),
                    mint_revoked=parsed.get("mint_revoked"),
                    freeze_active=parsed.get("freeze_active"),
                )
            ]
        )
    return out


def _iso_since_days(days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill",
        description=(
            "Phase 0 item 5. Reconstruct historical trigger crossings from the "
            "on-chain archive and append them as snapshots."
        ),
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument(
        "--from-json",
        help="ingest histories from a JSON file instead of the network "
        "(a list of token histories, each a list of TokenMetrics-shaped dicts)",
    )
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    config = load_config()

    if not args.from_json:
        print(
            json.dumps(
                {
                    "error": "no source given",
                    "hint": (
                        "pass --from-json with recorded histories, or supply "
                        "BITQUERY_TOKEN and extend main() to call "
                        "histories_from_bitquery; the archive query shape is "
                        "unverified against a live endpoint"
                    ),
                },
                indent=2,
            )
        )
        return 2

    from pathlib import Path

    from collectors.bitquery import _metrics_from_dict

    raw = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    histories = [[_metrics_from_dict(p, "solana") for p in hist] for hist in raw]

    with Store(args.db or str(config.db_path)) as store:
        runner = BackfillRunner(store)
        runner.ingest(histories)
        payload = {**runner.stats, "snapshots_total": store.snapshot_count()}
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
