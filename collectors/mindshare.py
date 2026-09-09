"""Mindshare -- a token's share of the attention observed around it.

What this measures, precisely
-----------------------------
Mindshare here is **share of observed on-chain and paid attention within one
measurement universe at one moment**. It is not a social-mention metric and it is
not Kaito-style narrative mindshare. Saying so up front matters, because "mindshare"
is a word with four meanings in this market and picking the wrong one silently is
how a feature ends up measuring something nobody intended.

Three components, each a share of the universe total:

* **Trade attention** -- the token's 24h transaction count over the universe's.
  How many times people acted, independent of how much they spent.
* **Dollar attention** -- 24h volume over the universe's. How much they spent.
* **Paid attention** -- DexScreener boost spend over the universe's. Literally the
  money spent buying visibility, which is the purest available proxy for
  *manufactured* mindshare and is worth having as its own column precisely so it
  can be told apart from the other two later.

The composite is the mean of whichever components resolved, renormalised over
their weights, expressed in percent.

Why a share and not a level
---------------------------
A level (400 transactions) means nothing without the tape around it: 400 on a dead
Tuesday and 400 during a mania are different observations. A share is
regime-normalised by construction, which is the same reason CLAUDE.md keeps
LunarCrush for regime detection rather than per-token scoring. The universe is the
comparison set, so it is recorded on every row.

The rules this obeys
--------------------
**No imputation** (hard rule 3). A component the source did not report is skipped,
not zeroed. A *measured* zero -- the API said ``totalAmount: 0`` -- is a real zero
and contributes a real zero share. Those two cases are different rows and this
module keeps them different. A token with no resolvable component has
``share_pct=None``; it does not have a mindshare of zero.

**Raw counts are stored, the share is derived** (BUILD_BRIEF.md section 3 item 3).
The per-token components *and the universe totals they were divided by* both go on
the row. That is what makes the share recomputable: when this formula is revised --
and it will be, it is an uncalibrated prior like every other weight in the repo --
every past row can be recomputed under the new one instead of being stranded.

**The universe is a sample, and a biased one.** Tokens reach it by being boosted or
profiled on DexScreener, so the denominator is not "all trading". It is named on
the row (``universe_size``) and stated on the web page. A bias you record is a
covariate; a bias you do not is a confound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Bumped whenever the component set or the weights below change. Written into the
# export and every scored row, so a share can always be traced to the formula that
# produced it -- the same discipline prompts/score.md applies to prompt_version.
METHOD_VERSION = "mindshare-v1"

# Uncalibrated priors, like every other weight in this repo. Equal thirds is the
# honest starting point: there is no evidence yet that dollar attention predicts
# better than trade attention, and inventing a split would be pretending there is.
COMPONENT_WEIGHTS: dict[str, float] = {
    "txns_24h": 1.0,
    "volume_24h_usd": 1.0,
    "boost_total": 1.0,
}

# The components, and where each is read from on a token record.
COMPONENTS: tuple[str, ...] = tuple(COMPONENT_WEIGHTS)


@dataclass(frozen=True, slots=True)
class MindshareObservation:
    """One token's mindshare, plus everything needed to recompute it later."""

    chain: str
    contract: str
    share_pct: float | None = None
    rank: int | None = None
    percentile: float | None = None
    universe_size: int | None = None

    # Raw per-token components.
    txns_24h: int | None = None
    txns_6h: int | None = None
    boost_amount: float | None = None
    boost_total: float | None = None
    boosts_active: float | None = None
    pair_count: int | None = None

    # Raw universe totals -- the denominators. Stored so the share above is a
    # derivation, not a fact that can never be checked.
    universe_txns_24h: int | None = None
    universe_volume_24h_usd: float | None = None
    universe_boost_total: float | None = None

    # Per-component shares, for inspection. Not stored as columns; the raw numbers
    # above are what the row carries.
    components: dict[str, float | None] | None = None

    @property
    def measured(self) -> bool:
        return self.share_pct is not None


def _finite(value: Any) -> float | None:
    """A usable non-negative number, or ``None``.

    Negative attention is not a thing; a negative count is a broken response, and
    letting one through would drag another token's share above 100%.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _read(token: Any, name: str) -> Any:
    """Field access that works for a TokenMetrics-shaped object or a plain dict."""
    if isinstance(token, dict):
        return token.get(name)
    return getattr(token, name, None)


def _component_values(token: Any) -> dict[str, float | None]:
    """Read the three raw components off a TokenMetrics-shaped object or a dict."""
    return {name: _finite(_read(token, name)) for name in COMPONENTS}


def universe_totals(tokens: list[Any]) -> dict[str, float | None]:
    """Sum each component across the universe.

    A component nobody reported has a total of ``None``, not 0 -- there is no
    denominator, so no share can be computed from it, and a zero denominator would
    invite a division that produces infinity.
    """
    totals: dict[str, float | None] = {}
    for name in COMPONENTS:
        present = [
            value
            for value in (_component_values(t)[name] for t in tokens)
            if value is not None
        ]
        totals[name] = sum(present) if present else None
    return totals


def compute(
    tokens: list[Any],
    *,
    key: Any = None,
) -> dict[tuple[str, str], MindshareObservation]:
    """Mindshare for every token in one universe, keyed by ``(chain, contract)``.

    The universe is exactly the list passed in. Two calls with different lists
    produce different, incomparable shares -- which is why ``universe_size`` and the
    universe totals travel on every observation.
    """
    if key is None:
        def key(token: Any) -> tuple[str, str]:
            if isinstance(token, dict):
                return (str(token.get("chain")), str(token.get("contract")))
            return (str(token.chain), str(token.contract))

    totals = universe_totals(tokens)
    universe_size = len(tokens)

    raw: list[tuple[tuple[str, str], Any, dict[str, float | None], float | None]] = []
    for token in tokens:
        values = _component_values(token)
        shares: dict[str, float | None] = {}
        for name in COMPONENTS:
            total = totals[name]
            value = values[name]
            # No denominator, or nothing measured for this token: no share. Note
            # that a *measured* zero survives this and contributes a real 0.0.
            if value is None or total is None or total <= 0:
                shares[name] = None
            else:
                shares[name] = value / total

        weighted = [
            (COMPONENT_WEIGHTS[name], share)
            for name, share in shares.items()
            if share is not None
        ]
        if weighted:
            weight_sum = sum(w for w, _ in weighted)
            composite = sum(w * s for w, s in weighted) / weight_sum * 100.0
        else:
            composite = None
        raw.append((key(token), token, shares, composite))

    # Rank and percentile over the tokens whose share resolved. Unmeasured tokens
    # are not ranked last; they are not ranked at all.
    measured = sorted(
        (item for item in raw if item[3] is not None),
        key=lambda item: -(item[3] or 0.0),
    )
    ranks = {item[0]: position for position, item in enumerate(measured, start=1)}
    measured_count = len(measured)

    out: dict[tuple[str, str], MindshareObservation] = {}
    for token_key, token, shares, composite in raw:
        rank = ranks.get(token_key)
        percentile = None
        if rank is not None and measured_count > 0:
            # Share of the measured universe this token is at or above, 0-100.
            percentile = (measured_count - rank + 1) / measured_count * 100.0
        values = _component_values(token)
        out[token_key] = MindshareObservation(
            chain=token_key[0],
            contract=token_key[1],
            share_pct=composite,
            rank=rank,
            percentile=percentile,
            universe_size=universe_size,
            txns_24h=_as_int(values["txns_24h"]),
            txns_6h=_as_int(_finite(_read(token, "txns_6h"))),
            boost_amount=_finite(_read(token, "boost_amount")),
            boost_total=values["boost_total"],
            boosts_active=_finite(_read(token, "boosts_active")),
            pair_count=_as_int(_finite(_read(token, "pair_count"))),
            universe_txns_24h=_as_int(totals["txns_24h"]),
            universe_volume_24h_usd=totals["volume_24h_usd"],
            universe_boost_total=totals["boost_total"],
            components=shares,
        )
    return out


def _as_int(value: float | None) -> int | None:
    return None if value is None else int(value)


def to_schema_group(observation: MindshareObservation | None) -> Any:
    """Build the schema's ``Mindshare`` group from an observation.

    ``None`` in gives an all-null group: a token whose universe was never measured
    has unknown mindshare, and the completeness modifier is what accounts for that.
    """
    from collectors.schema import Mindshare

    if observation is None:
        return Mindshare()
    return Mindshare(
        share_pct=observation.share_pct,
        rank=observation.rank,
        percentile=observation.percentile,
        universe_size=observation.universe_size,
        txns_24h=observation.txns_24h,
        txns_6h=observation.txns_6h,
        boost_amount=observation.boost_amount,
        boost_total=observation.boost_total,
        boosts_active=observation.boosts_active,
        pair_count=observation.pair_count,
        universe_txns_24h=observation.universe_txns_24h,
        universe_volume_24h_usd=observation.universe_volume_24h_usd,
        universe_boost_total=observation.universe_boost_total,
    )


def recompute_share(row: dict[str, Any], weights: dict[str, float] | None = None) -> float | None:
    """Recompute a stored row's share under different component weights.

    This is the payoff for storing the denominators: when the formula above is
    revised, historical rows are re-derivable instead of stranded. Takes a flat
    snapshot row (``mindshare_*`` and ``market_*`` columns) and returns the share
    under ``weights``.
    """
    weights = weights or COMPONENT_WEIGHTS
    pairs = {
        "txns_24h": (row.get("mindshare_txns_24h"), row.get("mindshare_universe_txns_24h")),
        "volume_24h_usd": (
            row.get("market_volume_24h_usd"),
            row.get("mindshare_universe_volume_24h_usd"),
        ),
        "boost_total": (
            row.get("mindshare_boost_total"),
            row.get("mindshare_universe_boost_total"),
        ),
    }
    weighted: list[tuple[float, float]] = []
    for name, (value, total) in pairs.items():
        weight = weights.get(name)
        if not weight:
            continue
        value, total = _finite(value), _finite(total)
        if value is None or total is None or total <= 0:
            continue
        weighted.append((weight, value / total))
    if not weighted:
        return None
    weight_sum = sum(w for w, _ in weighted)
    return sum(w * s for w, s in weighted) / weight_sum * 100.0
