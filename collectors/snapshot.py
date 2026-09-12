"""Phase 0 item 2 -- the snapshot writer.

BUILD_BRIEF.md section 3: "On trigger, capture the full input schema (section 4) and
append to DuckDB."

Maps a :class:`~collectors.metrics.TokenMetrics` onto the section 4 snapshot. The
mapping is deliberately dumb: it copies what the source reported and leaves
everything else ``None``. It computes nothing, fills nothing in, and carries no
fallbacks.

Several field groups come out entirely null in this phase, and that is correct
rather than broken:

* ``social_x`` and ``social_tg`` -- Phase 0 item 3 collects these, and it is not
  built yet. There is no retroactive source for micro-cap social history
  (BUILD_BRIEF.md section 1), which is the entire reason the collector has to run
  forward for weeks before anything can be fitted.
* ``trends`` and ``lineage`` -- no collector yet.
* ``flows`` -- needs per-cohort wallet analysis, a separate and much heavier query
  than the trigger path.

``mindshare`` is filled when the caller supplies it, which the trigger watcher does
for every batch: the batch is the measurement universe. It is null on a snapshot
built from a single token in isolation, because a share with no universe behind it
would be a number with no meaning.

They are null, not zero. A zero would say "we measured no mentions"; null says "we
did not measure". Those are different rows to a model, and `data_completeness` on
each row records which one this is.

This module imports the store lazily, inside the two functions that need it, so
``build_snapshot`` -- the section 4 mapping itself -- can be imported without
DuckDB. ``api/screener.py`` runs on stdlib only and uses the same mapping the
collector uses, rather than a second one that would drift.

``socials_declared`` is the exception worth noting: it is filled from launch
metadata now, from block one, because it is the best-evidenced feature available
(1.919% graduation with all three declared versus 0.110% without) and it costs
nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

from collectors.config import load_config
from collectors.metrics import TokenMetrics
from collectors.mindshare import MindshareObservation, to_schema_group
from collectors.schema import (
    Authorities,
    Deployer,
    Flows,
    Holders,
    Launch,
    Lineage,
    Market,
    Snapshot,
    SocialsDeclared,
    SocialTG,
    SocialX,
    Trends,
    clean,
)

if TYPE_CHECKING:  # avoids a cycle: trigger_watcher imports build_snapshot
    from collectors.store import Store
    from collectors.trigger_watcher import TriggerDecision


def build_snapshot(
    metrics: TokenMetrics,
    decision: TriggerDecision,
    *,
    source: str | None = None,
    regime: str | None = None,
    mindshare: MindshareObservation | None = None,
) -> Snapshot:
    """Build the section 4 snapshot for a token that has just crossed the trigger.

    ``decision.trigger`` must be set; building a snapshot for a token that did not
    fire would put a row into the dataset at a lifecycle point of its own, which is
    exactly the comparison the cohort design exists to prevent.

    ``mindshare`` comes from the caller rather than from ``metrics`` because it is
    not a property of one token: it is a share of the universe the token was
    observed in, so only the code holding the whole batch can compute it. Absent, it
    is all-null -- unknown mindshare, not a mindshare of zero.
    """
    if not decision.fired or decision.trigger is None:
        raise ValueError(
            "refusing to snapshot a token that did not cross the trigger; the sample "
            "is lifecycle-matched by construction (BUILD_BRIEF.md section 5, step 1)"
        )

    return Snapshot(
        chain=metrics.chain,
        contract=metrics.contract,
        trigger=decision.trigger,
        source=source or metrics.source,
        ts=metrics.observed_at_ms,
        ticker=metrics.ticker,
        trigger_mcap_crossed=decision.mcap_crossed,
        trigger_holders_crossed=decision.holders_crossed,
        age_at_trigger_minutes=metrics.age_minutes(),
        regime=regime,
        listings=metrics.listings,
        market=Market(
            mcap_usd=clean(metrics.mcap_usd),
            liquidity_usd=clean(metrics.liquidity_usd),
            volume_24h_usd=clean(metrics.volume_24h_usd),
            price_usd=clean(metrics.price_usd),
            fdv_usd=clean(metrics.fdv_usd),
        ),
        holders=Holders(
            count=metrics.holder_count,
            growth_6h_pct=clean(metrics.holders_growth_6h_pct),
            top10_ex_lp_pct=clean(metrics.top10_ex_lp_pct),
        ),
        authorities=Authorities(
            mint_revoked=metrics.mint_revoked,
            freeze_active=metrics.freeze_active,
            lp_locked_until=metrics.lp_locked_until_ms,
        ),
        deployer=Deployer(
            address=metrics.deployer_address,
            prior_launches=metrics.deployer_prior_launches,
            prior_rugs=metrics.deployer_prior_rugs,
        ),
        launch=Launch(
            bundled_supply_pct=clean(metrics.bundled_supply_pct),
            sniper_wallets=metrics.sniper_wallets,
            initial_buy_sol=clean(metrics.initial_buy_sol),
        ),
        mindshare=to_schema_group(mindshare),
        # Phase 0 items 3-5 fill these. Null until then, never zero.
        flows=Flows(),
        social_x=SocialX(),
        social_tg=SocialTG(),
        socials_declared=SocialsDeclared(
            telegram=metrics.declared_telegram,
            x=metrics.declared_x,
            website=metrics.declared_website,
        ),
        trends=Trends(),
        lineage=Lineage(),
    )


def write_snapshot(store: Store, snapshot: Snapshot) -> str:
    """Append one snapshot. Raises ``AppendOnlyViolation`` if the token already has one."""
    return store.append_snapshot(snapshot)


def _export_json(store: Store, limit: int) -> list[dict]:
    """Re-emit stored rows in the nested section 4 shape, for eyeballing."""
    from collectors.schema import FEATURE_GROUPS

    out = []
    for flat in store.recent_snapshots(limit):
        nested: dict = {
            "snapshot_id": flat["snapshot_id"],
            "ts": flat["ts"],
            "trigger": flat["trigger_kind"],
            "ticker": flat["ticker"],
            "chain": flat["chain"],
            "contract": flat["contract"],
            "age_at_trigger_minutes": flat["age_at_trigger_minutes"],
        }
        for group in FEATURE_GROUPS:
            prefix = f"{group}_"
            nested[group] = {
                key[len(prefix) :]: value
                for key, value in flat.items()
                if key.startswith(prefix)
            }
        nested["listings"] = json.loads(flat["listings"]) if flat["listings"] else None
        nested["regime"] = flat["regime"]
        nested["labels"] = {"filled_at": None}
        nested["_meta"] = {
            "data_completeness": flat["data_completeness"],
            "fields_present": flat["fields_present"],
            "fields_expected": flat["fields_expected"],
            "source": flat["source"],
            "trigger_mcap_crossed": flat["trigger_mcap_crossed"],
            "trigger_holders_crossed": flat["trigger_holders_crossed"],
        }
        out.append(nested)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="snapshot",
        description="Inspect the append-only snapshot table. This command never writes.",
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--limit", type=int, default=5, help="rows to emit (default: 5)")
    parser.add_argument("--stats", action="store_true", help="counts instead of rows")
    args = parser.parse_args(argv)

    from collectors.store import Store

    config = load_config()
    db_path = args.db or str(config.db_path)
    try:
        store = Store(db_path, read_only=True)
    except Exception as exc:
        print(f"error: cannot open {db_path}: {exc}", file=sys.stderr)
        return 2
    with store:
        if args.stats:
            payload = {
                "db": db_path,
                "snapshots": store.snapshot_count(),
                "trigger_breakdown": store.trigger_breakdown(),
                "dates": store.snapshot_dates(),
            }
        else:
            payload = _export_json(store, args.limit)
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
