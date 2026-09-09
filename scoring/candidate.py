"""Building the prompts/score.md step 4 input packet.

Split out of ``scoring/runner.py`` for the same reason as
``collectors/trigger_rule.py``: the runner imports DuckDB through the store, and
``api/screener.py`` runs on stdlib only. Both have to turn a snapshot row into the
identical candidate packet, because both then feed it to the same filters and the
same pillar maths -- a second copy of this mapping would let the live ranking and
the stored ranking disagree about the same token.

The runner re-exports both functions, so every existing import site is unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from collectors.schema import FEATURE_GROUPS, now_ms
from filters.hard_filters import FilterInput


def candidate_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build the prompts/score.md step 4 input packet from a stored snapshot row."""
    grouped: dict[str, Any] = {}
    for group in FEATURE_GROUPS:
        prefix = f"{group}_"
        grouped[group] = {
            key[len(prefix) :]: value
            for key, value in row.items()
            if key.startswith(prefix)
        }
    for key in ("net_flow_by_cohort",):
        raw = grouped.get("flows", {}).get(key)
        if isinstance(raw, str):
            grouped["flows"][key] = json.loads(raw)

    listings = row.get("listings")
    age_minutes = row.get("age_at_trigger_minutes")
    return {
        "snapshot_id": row["snapshot_id"],
        "ticker": row.get("ticker"),
        "chain": row.get("chain"),
        "contract": row.get("contract"),
        "age_hours": None if age_minutes is None else age_minutes / 60.0,
        "market_cap_usd": grouped["market"].get("mcap_usd"),
        "fdv_usd": grouped["market"].get("fdv_usd"),
        "liquidity_usd": grouped["market"].get("liquidity_usd"),
        "volume_24h_usd": grouped["market"].get("volume_24h_usd"),
        "holders": grouped["holders"],
        "authorities": grouped["authorities"],
        "deployer": grouped["deployer"],
        "launch": grouped["launch"],
        "flows": grouped["flows"],
        "mindshare": grouped["mindshare"],
        "social_x": grouped["social_x"],
        "social_tg": grouped["social_tg"],
        "socials_declared": grouped["socials_declared"],
        "trends": grouped["trends"],
        "lineage": grouped["lineage"],
        "listings": json.loads(listings) if isinstance(listings, str) else listings,
        "data_completeness": row.get("data_completeness"),
    }


def filter_input_from_candidate(candidate: dict[str, Any], **safety: Any) -> FilterInput:
    authorities = candidate.get("authorities") or {}
    holders = candidate.get("holders") or {}
    deployer = candidate.get("deployer") or {}
    base: dict[str, Any] = {
        "chain": candidate.get("chain") or "solana",
        "ticker": candidate.get("ticker"),
        "contract": candidate.get("contract"),
        "evaluated_at_ms": candidate.get("evaluated_at_ms") or now_ms(),
        "mint_revoked": authorities.get("mint_revoked"),
        "freeze_active": authorities.get("freeze_active"),
        "lp_locked_until_ms": authorities.get("lp_locked_until"),
        "top10_ex_lp_pct": holders.get("top10_ex_lp_pct"),
        "liquidity_usd": candidate.get("liquidity_usd"),
        "mcap_usd": candidate.get("market_cap_usd"),
        "fdv_usd": candidate.get("fdv_usd"),
        "deployer_prior_rugs": deployer.get("prior_rugs"),
    }
    base.update({k: v for k, v in safety.items() if k in FilterInput.__dataclass_fields__})
    return FilterInput(**base)
