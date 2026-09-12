"""Live screener endpoint -- ``GET /api/screener``.

Why this exists
---------------
``web/screener-data.json`` is an export of the collected dataset, and it is only as
fresh as the last collector run. Deployed on its own it shows whatever was in the
database when someone last ran the export -- which, before this endpoint existed,
was a recorded fixture. This function fetches DexScreener at request time, so the
deployed page shows real tokens whether or not a collector has ever run.

It reuses the repo's own logic rather than reimplementing it. Same trigger rule
(``collectors/trigger_rule.py``), same section 4 mapping
(``collectors/snapshot.py``), same candidate packet (``scoring/candidate.py``),
same eight filters, same pillar maths, same mindshare formula. That sharing is the
whole reason those modules were split away from the DuckDB-importing ones: a second
implementation for the serverless path would let the live ranking and the stored
ranking disagree about the same token, and nobody would know which was right.

Stdlib only, so there is no ``requirements.txt`` and nothing to install at deploy
time.

What it is and is not
---------------------
It is a live look at tokens that satisfy the trigger right now. It is **not** the
dataset. The dataset is what the collector writes, once per token, at first
crossing, and never edits; this is a view that will look different in a minute and
keeps no history. Tokens below the trigger are counted but not listed, because a
table mixing lifecycle points is the exact comparison CLAUDE.md's cohort design
exists to prevent.

Safety is part of it. ``collectors/safety.py`` runs for the tokens that cleared the
trigger, so the live table shows real filter verdicts rather than a column of
"unmeasured". A safety lookup that fails leaves those filters unknown, and unknown
excludes -- the view degrades toward showing fewer scores, never toward showing
unearned ones.

The scores here carry the same warning as everywhere else: uncalibrated priors,
Phase 0, no measured edge, nothing predictive. This endpoint reads. It holds no
credential, and there is no code path from here to an order (hard rule 4).
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

# The function is bundled with the repo laid out beside it; make the packages
# importable regardless of where the runtime roots the process.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from collectors import chains as chain_registry  # noqa: E402
from collectors import mindshare as mindshare_mod  # noqa: E402
from collectors.dexscreener import (  # noqa: E402
    SOURCE_NAME,
    DexScreenerClient,
    DexScreenerError,
    DexScreenerFeed,
)
from collectors.safety import SafetySource  # noqa: E402
from collectors.snapshot import build_snapshot  # noqa: E402
from collectors.trigger_rule import (  # noqa: E402
    TRIGGER_HOLDER_COUNT,
    TRIGGER_MCAP_USD,
    evaluate,
)
from filters.hard_filters import apply as apply_filters  # noqa: E402
from scoring.candidate import candidate_from_row, filter_input_from_candidate  # noqa: E402
from scoring.pillars import WEIGHTS, WEIGHTS_VERSION, score_candidate  # noqa: E402
from scoring.prompt_meta import prompt_version  # noqa: E402

DEFAULT_LIMIT = 100
MAX_LIMIT = 250
# Both caps exist for the same reason: keyless GoPlus is 30 requests a minute, and
# this function has a request budget measured in seconds. Token security batches --
# a few requests covers everything. The other two are one request each, so they are
# capped and spent on the rows a reader will actually reach. The collector, which
# has no such budget, runs them uncapped.
SAFETY_RUGCHECK_LIMIT = 6
SAFETY_DEPLOYER_LIMIT = 10
# Edge-cached for this long. DexScreener's keyless endpoints are a shared resource
# and this page could be opened by many people at once; one upstream poll per
# window is the polite shape, and 45s is well inside the freshness a human reading
# a market page can perceive.
CACHE_SECONDS = 45

HEADLINE_WARNING = (
    "Phase 0 -- collection only. The scoring weights are uncalibrated priors: "
    "guesses. No edge has been measured, so nothing on this page is a prediction "
    "or a recommendation."
)

LIVE_NOTICE = (
    "Live view: real DexScreener market data, fetched when this page loaded. It is "
    "not the collected dataset -- it keeps no history, and it will look different "
    "in a minute. Only tokens currently at or above the trigger are listed."
)


def build_live_payload(
    chain_names: list[str],
    *,
    limit: int = DEFAULT_LIMIT,
    feed: Any = None,
    regime: str | None = None,
    safety_source: Any = None,
    with_safety: bool = True,
) -> dict[str, Any]:
    """Poll, score and rank. Pure enough to test with a stub feed."""
    resolved = chain_registry.resolve_requested(chain_names)
    feed = feed or DexScreenerFeed.for_chains(resolved, client=DexScreenerClient())

    universe = list(feed.poll())
    # Mindshare is measured over everything polled, not over the tokens that fired.
    # A share whose denominator was already filtered would be a share of the
    # survivors -- the selection effect prompts/score.md step 5 warns about.
    shares = mindshare_mod.compute(universe) if universe else {}

    triggered = [m for m in universe if evaluate(m.mcap_usd, m.holder_count).fired]
    below_trigger = len(universe) - len(triggered)

    # Safety is looked up only for the tokens that actually cleared the trigger --
    # it is the expensive call, and a token below the trigger is not going to be
    # listed whatever it says. A failure leaves every filter unknown, which
    # excludes, so a degraded lookup makes this view more conservative, not less.
    reports: dict[tuple[str, str], Any] = {}
    safety_error: str | None = None
    if with_safety and triggered:
        source = safety_source or SafetySource(
            rugcheck_limit=SAFETY_RUGCHECK_LIMIT, deployer_limit=SAFETY_DEPLOYER_LIMIT
        )
        try:
            reports = source.fetch([(m.chain, m.contract) for m in triggered[:limit]])
        except Exception as exc:
            safety_error = f"{type(exc).__name__}: {exc}"

    tokens: list[dict[str, Any]] = []
    for metrics in triggered:
        decision = evaluate(metrics.mcap_usd, metrics.holder_count)

        snapshot = build_snapshot(
            metrics,
            decision,
            source=SOURCE_NAME,
            regime=regime,
            mindshare=shares.get((metrics.chain, metrics.contract)),
        )
        row = snapshot.to_row()
        candidate = candidate_from_row(row)

        report = reports.get((metrics.chain, metrics.contract))
        safety_fields = report.to_filter_fields() if report is not None else {}
        if safety_fields:
            candidate = {**candidate, "safety": safety_fields}
        verdict = apply_filters(filter_input_from_candidate(candidate, **safety_fields))
        pillars = score_candidate(candidate, regime=regime)
        excluded = verdict.excluded
        mindshare_group = candidate.get("mindshare") or {}

        tokens.append(
            {
                "snapshot_id": None,  # nothing was stored; this is a view
                "ticker": snapshot.ticker,
                "chain": snapshot.chain,
                "chain_label": chain_registry.label(snapshot.chain),
                "contract": snapshot.contract,
                "ts": snapshot.ts,
                "snapshot_date": snapshot.snapshot_date,
                "trigger": snapshot.trigger,
                "trigger_mcap_crossed": decision.mcap_crossed,
                "trigger_holders_crossed": decision.holders_crossed,
                "age_at_trigger_minutes": snapshot.age_at_trigger_minutes,
                "regime": regime,
                "source": SOURCE_NAME,
                "mcap_usd": snapshot.market.mcap_usd,
                "fdv_usd": snapshot.market.fdv_usd,
                "liquidity_usd": snapshot.market.liquidity_usd,
                "volume_24h_usd": snapshot.market.volume_24h_usd,
                "holders": snapshot.holders.count,
                "top10_ex_lp_pct": snapshot.holders.top10_ex_lp_pct,
                "mint_revoked": snapshot.authorities.mint_revoked,
                "freeze_active": snapshot.authorities.freeze_active,
                "socials_declared": {
                    "telegram": snapshot.socials_declared.telegram,
                    "x": snapshot.socials_declared.x,
                    "website": snapshot.socials_declared.website,
                },
                "mindshare": mindshare_group,
                "safety": (
                    {
                        "source": report.source,
                        "measured_fields": report.measured_fields,
                        "lp_locked_pct": report.lp_locked_pct,
                        "holder_count": report.holder_count,
                        "rugged": report.rugged,
                        "deployer_address": report.deployer_address,
                        "risk_labels": list(report.risk_labels),
                        **safety_fields,
                    }
                    if report is not None
                    else None
                ),
                "data_completeness": row["data_completeness"],
                "fields_present": row["fields_present"],
                "fields_expected": row["fields_expected"],
                "score": None if excluded else pillars.score,
                "rank": None,
                "excluded": excluded,
                "rejected_by": verdict.rejected_by,
                "indeterminate_on": verdict.indeterminate_on,
                "pillar_scores": pillars.pillar_scores(),
                # See export_web.py: separate from `score`, reported even when the
                # row is excluded, and never a substitute for it.
                "pillar_composite": pillars.score,
                "notes": pillars.notes(),
                # No narrative: that needs an API call per token, and this endpoint
                # holds no credential and runs on a request budget.
                "thesis": None,
                "bear_case": None,
                "falsifier": None,
                "max_multiple_24h": None,
                "max_multiple_7d": None,
                "max_drawdown_before_peak_24h": None,
                "survived_24h": None,
                "survived_7d": None,
            }
        )

    ranked = sorted(
        (t for t in tokens if not t["excluded"] and t["score"] is not None),
        key=lambda t: -t["score"],
    )
    for position, token in enumerate(ranked, start=1):
        token["rank"] = position

    tokens.sort(key=lambda t: (t["rank"] is None, t["rank"] or 0, -(t["mcap_usd"] or 0.0)))
    tokens = tokens[:limit]

    counts: dict[str, int] = {}
    for token in tokens:
        counts[token["chain"]] = counts.get(token["chain"], 0) + 1

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "phase": "0",
        "mode": "live",
        "paper_mode": True,
        "prompt_version": prompt_version(),
        "weights_version": WEIGHTS_VERSION,
        "weights": WEIGHTS,
        "weights_are_calibrated": False,
        "data_sources": [SOURCE_NAME],
        "all_rows_synthetic": False,
        "synthetic_notice": None,
        "headline_warning": HEADLINE_WARNING,
        "live_notice": LIVE_NOTICE,
        "live": {
            "chains_requested": resolved,
            "polled": len(universe),
            "triggered": len(tokens),
            "below_trigger": below_trigger,
            "universe_size": len(universe),
            "cache_seconds": CACHE_SECONDS,
            "safety_measured": len(reports),
            "safety_error": safety_error,
            "safety_sources": "GoPlus + RugCheck (keyless)" if with_safety else None,
            "discovery": (
                "DexScreener's boosted and profiled token lists. There is no keyless "
                "new-pool firehose, so a token enters this universe because someone "
                "paid to boost it or filled in its profile. That is a real sampling "
                "bias, it is the denominator of every mindshare figure here, and it "
                "is stated rather than hidden."
            ),
        },
        "chains": [
            {
                "name": name,
                "label": chain_registry.label(name) or name,
                "count": counts.get(name, 0),
            }
            for name in resolved
        ],
        "mindshare": {
            "method_version": mindshare_mod.METHOD_VERSION,
            "component_weights": mindshare_mod.COMPONENT_WEIGHTS,
            "prior_weight_in_composite": WEIGHTS.get("mindshare", 0.0),
            "definition": (
                "Share of the attention observed across one measurement universe: "
                "24h transactions, 24h volume and DexScreener boost spend, each as a "
                "share of the universe total, averaged over the components that "
                "resolved. It is on-chain and paid attention, not social mentions."
            ),
            "caveat": (
                "The universe is the tokens this poll saw, so shares from two polls "
                "are not comparable. Weighted 0.00 in the composite: collected and "
                "scored, but no prior was invented for it."
            ),
        },
        "trigger": {
            "mcap_usd": TRIGGER_MCAP_USD,
            "holder_count": TRIGGER_HOLDER_COUNT,
            "rule": (
                "Every token is snapshotted once, the first time it crosses $250k "
                "market cap or 500 holders. The same rule applies to every token: "
                "the sample is lifecycle-matched by construction. DexScreener reports "
                "no holder counts, so only the market-cap half can fire from this "
                "source -- a missing holder count never crosses 500."
            ),
        },
        "tokens": tokens,
    }


def _parse_query(raw: str) -> tuple[list[str], int, bool]:
    params = urllib.parse.parse_qs(raw or "")
    requested = params.get("chains", [",".join(chain_registry.DEFAULT_CHAINS)])[0]
    chain_names = [c.strip() for c in requested.split(",") if c.strip()]
    try:
        limit = int(params.get("limit", [str(DEFAULT_LIMIT)])[0])
    except ValueError:
        limit = DEFAULT_LIMIT
    # ?safety=0 skips the safety lookup. Useful when GoPlus is having a bad minute
    # and a market view is still wanted -- every filter then reads unknown, which
    # excludes, so nothing is silently passed.
    with_safety = params.get("safety", ["1"])[0].strip().lower() not in ("0", "false", "no")
    return chain_names, max(1, min(limit, MAX_LIMIT)), with_safety


class handler(BaseHTTPRequestHandler):
    """Vercel Python function entry point. GET only; nothing here writes."""

    def do_GET(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        chain_names, limit, with_safety = _parse_query(query)
        try:
            payload = build_live_payload(chain_names, limit=limit, with_safety=with_safety)
            status = 200
        except ValueError as exc:
            # An unsupported chain. The caller asked for something specific and got
            # nothing; saying so beats returning an empty list that reads as a quiet
            # market.
            payload = {"error": str(exc), "mode": "live", "tokens": []}
            status = 400
        except DexScreenerError as exc:
            payload = {
                "error": f"DexScreener is not answering: {exc}",
                "mode": "live",
                "tokens": [],
            }
            status = 503
        except Exception as exc:  # pragma: no cover - last-resort guard
            payload = {
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=3),
                "mode": "live",
                "tokens": [],
            }
            status = 500

        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if status == 200:
            self.send_header(
                "Cache-Control",
                f"public, s-maxage={CACHE_SECONDS}, stale-while-revalidate=300",
            )
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Quiet the default stderr access log; the platform already records it."""
