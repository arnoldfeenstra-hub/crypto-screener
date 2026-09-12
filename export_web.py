"""Export the dataset to the static JSON the web viewer reads.

Same shape as the zittingsrooster project: a DuckDB file in, one JSON out,
``web/`` deployed as a static site with no build step.

What gets exported is deliberately not just the ranking. CLAUDE.md says never to
describe Phase 0 output as predictive, so the page has to be able to show *why* the
scores mean as little as they do: the Phase 0 progress bar, the exit criteria, the
published base rates, the calibration verdict, and how many rows were excluded for
being unmeasured rather than bad. A page that showed only a leaderboard would be
the failure BUILD_BRIEF.md section 3 names -- laundering a coin flip as a decision.

No secrets go in the file. It carries market data, scores and counts; nothing here
reads an API key, and contract addresses are public chain data.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from calibration.fit import (
    MIN_DEAD_PER_SURVIVOR,
    MIN_TRIGGERED_TOKENS,
    PUBLISHED_CONCORDANCE_BENCHMARK,
)
from calibration.report import render as render_calibration
from collectors import chains as chain_registry
from collectors.config import load_config
from collectors.mindshare import COMPONENT_WEIGHTS as MINDSHARE_COMPONENT_WEIGHTS
from collectors.mindshare import METHOD_VERSION as MINDSHARE_METHOD_VERSION
from collectors.store import Store
from scoring.pillars import MINDSHARE_PRIOR_WEIGHT, WEIGHTS, composite
from scoring.runner import WEIGHTS_VERSION, prompt_version

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = REPO_ROOT / "web" / "screener-data.json"

# CLAUDE.md, "Base rates". Shown on the page so the ranking is always read next to
# the number it has to beat.
PUBLISHED_BASE_RATES = {
    "graduation_rate_range_pct": [0.198, 2.7],
    "measurements": [
        {"figure_pct": 0.198, "source": "May-Jun cohort study"},
        {"figure_pct": 0.26, "source": "mid-June"},
        {"figure_pct": 2.7, "source": "Aug cohort tracking"},
        {"figure_pct": 1.4, "source": "all-time Dune"},
    ],
    "telegram_declared_pct": 1.485,
    "telegram_absent_pct": 0.166,
    "telegram_lift": 8.94,
    "all_three_socials_pct": 1.919,
    "no_socials_pct": 0.110,
    "all_three_lift": 17.4,
    "concordance_benchmark": PUBLISHED_CONCORDANCE_BENCHMARK,
    "note": (
        "Graduation is roughly a $69k market cap, and most graduates still round to "
        "zero. The screener's job is to beat a ~2% base rate, not to find winners."
    ),
}


def _json_field(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def build_payload(store: Store, *, limit: int = 500) -> dict[str, Any]:
    snapshots = store.recent_snapshots(limit)
    scores = {row["snapshot_id"]: row for row in store.latest_scores(limit)}

    survivors, dead = store.survivor_counts("7d")
    complete_social = store.snapshots_with_complete_social()
    triggered = store.snapshot_count()

    tokens: list[dict[str, Any]] = []
    for snap in snapshots:
        score_row = scores.get(snap["snapshot_id"], {})
        labels = store.latest_labels(snap["snapshot_id"]) or {}
        pillar_scores = _json_field(score_row.get("pillar_scores"), {})
        safety = store.latest_safety(snap["snapshot_id"])
        tokens.append(
            {
                "snapshot_id": snap["snapshot_id"],
                "ticker": snap["ticker"],
                "chain": snap["chain"],
                "contract": snap["contract"],
                "ts": snap["ts"],
                "snapshot_date": str(snap["snapshot_date"]),
                "trigger": snap["trigger_kind"],
                "trigger_mcap_crossed": snap["trigger_mcap_crossed"],
                "trigger_holders_crossed": snap["trigger_holders_crossed"],
                "age_at_trigger_minutes": snap["age_at_trigger_minutes"],
                "regime": snap["regime"],
                "source": snap["source"],
                "chain_label": chain_registry.label(snap["chain"]),
                "mcap_usd": snap["market_mcap_usd"],
                "fdv_usd": snap["market_fdv_usd"],
                "liquidity_usd": snap["market_liquidity_usd"],
                "volume_24h_usd": snap["market_volume_24h_usd"],
                "holders": snap["holders_count"],
                "top10_ex_lp_pct": snap["holders_top10_ex_lp_pct"],
                "mint_revoked": snap["authorities_mint_revoked"],
                "freeze_active": snap["authorities_freeze_active"],
                "socials_declared": {
                    "telegram": snap["socials_declared_telegram"],
                    "x": snap["socials_declared_x"],
                    "website": snap["socials_declared_website"],
                },
                # Mindshare (collectors/mindshare.py). The raw components and the
                # universe totals ship alongside the derived share so the page can
                # show what the share was computed from, and so a reader can see
                # that a 12% share of a universe of 40 is not a 12% share of the
                # market.
                "mindshare": {
                    "share_pct": snap["mindshare_share_pct"],
                    "rank": snap["mindshare_rank"],
                    "percentile": snap["mindshare_percentile"],
                    "universe_size": snap["mindshare_universe_size"],
                    "txns_24h": snap["mindshare_txns_24h"],
                    "txns_6h": snap["mindshare_txns_6h"],
                    "boost_amount": snap["mindshare_boost_amount"],
                    "boost_total": snap["mindshare_boost_total"],
                    "boosts_active": snap["mindshare_boosts_active"],
                    "pair_count": snap["mindshare_pair_count"],
                    "universe_txns_24h": snap["mindshare_universe_txns_24h"],
                    "universe_volume_24h_usd": snap["mindshare_universe_volume_24h_usd"],
                    "universe_boost_total": snap["mindshare_universe_boost_total"],
                },
                # The safety lookup behind the filter verdicts, if one was made.
                # Null here means the checks were never run, which is why the row
                # reads "excluded as unmeasured" rather than "rejected".
                "safety": (
                    {
                        "source": safety["source"],
                        "collected_at_ms": safety["collected_at_ms"],
                        "honeypot": safety["honeypot"],
                        "buy_tax_pct": safety["buy_tax_pct"],
                        "sell_tax_pct": safety["sell_tax_pct"],
                        "mint_revoked": safety["mint_revoked"],
                        "freeze_active": safety["freeze_active"],
                        "lp_burned": safety["lp_burned"],
                        "lp_locked_pct": safety["lp_locked_pct"],
                        "top10_ex_lp_pct": safety["top10_ex_lp_pct"],
                        "holder_count": safety["holder_count"],
                        "deployer_address": safety["deployer_address"],
                        "deployer_prior_rugs": safety["deployer_prior_rugs"],
                        "rugged": safety["rugged"],
                        "risk_labels": _json_field(safety["risk_labels"], []),
                    }
                    if safety
                    else None
                ),
                "data_completeness": snap["data_completeness"],
                "fields_present": snap["fields_present"],
                "fields_expected": snap["fields_expected"],
                # Scoring, if this token has been scored.
                "score": score_row.get("score"),
                "rank": score_row.get("rank"),
                "excluded": score_row.get("excluded"),
                "rejected_by": _json_field(score_row.get("rejected_by"), []),
                "indeterminate_on": _json_field(score_row.get("indeterminate_on"), []),
                "pillar_scores": pillar_scores,
                # The weighted mean over the pillars that resolved, kept separate
                # from `score` and reported for excluded rows too. `score` stays
                # null when a hard filter excluded the row -- that discipline does
                # not bend. This is the arithmetic underneath it, which is a fact
                # about the row whatever the filters said, and without it a page of
                # real Phase 0 data is a column of nulls that tells a reader
                # nothing about what was actually measured.
                "pillar_composite": composite(pillar_scores)[0],
                "thesis": score_row.get("thesis"),
                "bear_case": score_row.get("bear_case"),
                "falsifier": score_row.get("falsifier"),
                # Outcomes, if resolved.
                "max_multiple_24h": labels.get("max_multiple_24h"),
                "max_multiple_7d": labels.get("max_multiple_7d"),
                "max_drawdown_before_peak_24h": labels.get("max_drawdown_before_peak_24h"),
                "survived_24h": labels.get("survived_24h"),
                "survived_7d": labels.get("survived_7d"),
            }
        )

    calibration = render_calibration(store)

    excluded_evidence = sum(
        1 for t in tokens if t["excluded"] and t["rejected_by"]
    )
    excluded_unmeasured = sum(
        1 for t in tokens if t["excluded"] and not t["rejected_by"]
    )

    # Whether any row came off a real chain. Derived from the data rather than set
    # by hand, so the "these are fixtures" notice disappears by itself the first
    # time a live row lands, and cannot be left switched off by mistake.
    sources = {t["source"] for t in tokens if t["source"]}
    synthetic_sources = {s for s in sources if s.startswith(("replay", "fixture", "test"))}
    all_synthetic = bool(sources) and synthetic_sources == sources

    chain_counts = store.chain_breakdown()

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "phase": "0",
        "mode": "collected",
        "paper_mode": True,
        "prompt_version": prompt_version(),
        "weights_version": WEIGHTS_VERSION,
        "weights": WEIGHTS,
        "weights_are_calibrated": False,
        "data_sources": sorted(sources),
        "chains": [
            {
                "name": name,
                "label": chain_registry.label(name) or name,
                "count": count,
            }
            for name, count in chain_counts.items()
        ],
        "mindshare": {
            "method_version": MINDSHARE_METHOD_VERSION,
            "component_weights": MINDSHARE_COMPONENT_WEIGHTS,
            "prior_weight_in_composite": MINDSHARE_PRIOR_WEIGHT,
            "definition": (
                "Share of the attention observed across one measurement universe: "
                "24h transactions, 24h volume and DexScreener boost spend, each as a "
                "share of the universe total, averaged over the components that "
                "resolved. It is on-chain and paid attention, not social mentions."
            ),
            "caveat": (
                "The universe is whatever the collector polled -- tokens reach it by "
                "being boosted or profiled on DexScreener -- so it is a biased sample "
                "and shares from different universes are not comparable. Weighted 0.00 "
                "in the composite: it is collected and scored, but no prior was "
                "invented for it, and Phase 2 has not fitted one."
            ),
        },
        "all_rows_synthetic": all_synthetic,
        "synthetic_notice": (
            "Every row on this page is synthetic -- replayed from a recorded test "
            "fixture, not collected from any chain. The tickers and contract "
            "addresses do not refer to real tokens."
            if all_synthetic
            else None
        ),
        "headline_warning": (
            "Phase 0 -- collection only. The scoring weights are uncalibrated priors: "
            "guesses. No edge has been measured, so nothing on this page is a "
            "prediction or a recommendation."
        ),
        "progress": {
            "triggered_tokens": triggered,
            "complete_social_series": complete_social,
            "survivors_7d": survivors,
            "dead_7d": dead,
            "dead_per_survivor": (dead / survivors) if survivors else None,
            "min_triggered_tokens": MIN_TRIGGERED_TOKENS,
            "min_dead_per_survivor": MIN_DEAD_PER_SURVIVOR,
            "exit_criteria_met": calibration["phase_0_exit_criteria"]["met"],
            "snapshots_scored": len(scores),
            "outcome_observations": store.outcome_observation_count(),
            "social_observations": store.social_observation_count(),
            "labelled_snapshots": store.labelled_snapshot_count(),
            "excluded_on_evidence": excluded_evidence,
            "excluded_as_unmeasured": excluded_unmeasured,
            "safety_checked": store.snapshots_with_safety(),
            "safety_observations": store.safety_observation_count(),
        },
        "calibration": {
            "verdict": calibration["verdict"],
            "explanation": calibration["explanation"],
            "base_rate_by_mcap_band": calibration["base_rate_by_mcap_band"],
            "fit": calibration.get("fit"),
        },
        "published_base_rates": PUBLISHED_BASE_RATES,
        "trigger": {
            "mcap_usd": 250_000,
            "holder_count": 500,
            "rule": (
                "Every token is snapshotted once, the first time it crosses $250k "
                "market cap or 500 holders. The same rule applies to every token: "
                "the sample is lifecycle-matched by construction."
            ),
        },
        "tokens": tokens,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="export-web",
        description="Export the dataset to web/screener-data.json for the static viewer.",
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args(argv)

    config = load_config()
    db_path = args.db or str(config.db_path)
    if not Path(db_path).exists():
        print(json.dumps({"error": f"no database at {db_path}"}, indent=2))
        return 2

    with Store(db_path, read_only=True) as store:
        payload = build_payload(store, limit=args.limit)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "written": str(out),
                "tokens": len(payload["tokens"]),
                "bytes": out.stat().st_size,
                "verdict": payload["calibration"]["verdict"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
