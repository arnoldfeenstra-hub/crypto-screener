"""Deterministic pillar scoring -- prompts/score.md step 2.

This is the half of scoring that must be reproducible. Phase 2 fits coefficients
against *these* numbers, so they cannot come from a model whose output varies
between calls; the LLM's job (scoring/runner.py) is the qualitative half -- thesis,
bear case, falsifier -- not the arithmetic.

Everything here is a pure function of one candidate dict. ``WEIGHTS`` is the first
fitted vector (prompts/score.md version 7): ``calibration/fit.py`` output on a
sample too small to establish an edge, adopted before Phase 0's gate was met, with
its calibration record in ``scoring/weights/``. ``PRIOR_WEIGHTS`` keeps the guesses
it replaced. ``WEIGHTS_VERSION`` and ``prompt_version`` are recorded on every
scored row so a score can always be traced to the weights that produced it.

What a Phase 0 row actually scores
----------------------------------
Pillar A (attention) reads X fields that nothing fills at a trigger unless the X
collector runs, so it comes back ``None`` -- not zero. B (community) resolves only
where a Telegram count was collected. C (lineage) resolves from the socials a
token declares; D (on-chain structure), E (asymmetry), F (mindshare) and G
(momentum) from what DexScreener answers. The composite is renormalised over
the pillars that resolved, so it reads as "of what can be seen", and is then
multiplied by data completeness, which drags it down to reflect how little that is.
Both numbers are reported separately so the distinction stays visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Mindshare's prior weight, and why it was zero.
#
# .claude/rules/stats.md forbids putting anything but fitted coefficients into the
# weight vector. Mindshare was a new feature with no outcome data behind it, so any
# nonzero prior would have been a guess -- and unlike the five original weights,
# which at least came from the brief, one invented in the same commit that
# invented the feature.
#
# So it was collected, scored and handed to calibration as a feature, contributing
# nothing until a fit gave it a number. The fit behind WEIGHTS did: 0.08 (below).
# This constant stays the prior, in PRIOR_WEIGHTS.
MINDSHARE_PRIOR_WEIGHT = 0.0

# Momentum's prior weight, and why it is also zero.
#
# Same rule, same answer. calibration/backtest.py measured the one rate-of-change
# feature the old schema could express -- a 6h-over-24h trade-count ratio -- and
# found it worth nothing: its apparent AUC of 0.70 collapsed to 0.50 inside a
# single age stratum, because for a token younger than six hours txns_6h equals
# txns_24h and the ratio pins at exactly 4.0. It was age wearing a disguise.
#
# The fields this pillar reads are new and were chosen so the same question can be
# asked without that artefact: a buy/sell split is a ratio inside one window and
# cannot saturate on age at all. Whether they carry signal is unmeasured, and a
# prior invented in the commit that invents the feature is not a prior. So the
# pillar is computed, stored, displayed, sorted on and handed to calibration, and
# it moves no score until a fit can see it. The fit behind WEIGHTS could not:
# none of the snapshots it was fitted on carried these fields, so it stays at
# zero there too. tests/test_momentum_pillar.py asserts the composite is
# numerically identical with the pillar present and absent.
MOMENTUM_PRIOR_WEIGHT = 0.0

# prompts/score.md step 2: the weight vector in force, and the first one fitted.
#
# From scoring/weights/fitted-v1.json, written by
# `python -m calibration.report --force --record scoring/weights/fitted-v1.json`:
# a logistic fit on one row per token as scored at its trigger -- 190 tokens
# triggered 2026-09-12 to 09-17 -- against the backtest's pre-set outcome, a 1.5x
# inside six hours. Negative coefficients clamped to zero, renormalised, rounded.
#
# On the 82 tokens that triggered afterwards (17 surged, all in a neutral tape) it
# ranks with AUC 0.67 [0.52, 0.83] where PRIOR_WEIGHTS ranked 0.64 [0.48, 0.79].
# That is a small, overlapping difference on one regime, adopted before Phase 0's
# gate was met; docs/calibration-2026-09-24.md sets out what was checked, what
# could not be, and why the gate was overridden.
#
# The zeros do not all mean the same thing, and the difference matters:
#   * lineage_meta_fit and community_depth were fitted and came out *negative* --
#     on this sample a higher score on either went with fewer surges. Clamped,
#     because prompts/score.md presents weights as importances.
#   * attention_velocity and momentum_flow were never observed at a trigger (no X
#     collector ran; the momentum fields postdate these snapshots), so the fit
#     could not see them. Zero is "not yet fitted", the rule mindshare and
#     momentum were held to when they were added.
WEIGHTS: dict[str, float] = {
    "attention_velocity": 0.0,
    "community_depth": 0.0,
    "lineage_meta_fit": 0.0,
    "onchain_structure": 0.55,
    "asymmetry_timing": 0.37,
    "mindshare": 0.08,
    "momentum_flow": 0.0,
}

# The uncalibrated priors WEIGHTS replaced (priors-v3), from the brief. Kept so a
# fit can always be judged against them, and so reverting is one assignment and a
# version bump.
PRIOR_WEIGHTS: dict[str, float] = {
    "attention_velocity": 0.28,
    "community_depth": 0.20,
    "lineage_meta_fit": 0.15,
    "onchain_structure": 0.22,
    "asymmetry_timing": 0.15,
    "mindshare": MINDSHARE_PRIOR_WEIGHT,
    "momentum_flow": MOMENTUM_PRIOR_WEIGHT,
}

# Bumped whenever WEIGHTS changes -- in value or in shape. Stored on every scored
# row so a score can be traced to the numbers that produced it. It lives here,
# beside the vector it names, so the two cannot be edited apart, and it names the
# calibration record in scoring/weights/ that the vector was copied from. The
# priors-v1..v3 versions had no record: they were guesses. v2 added the
# `mindshare` key at 0.0 and v3 `momentum_flow` at 0.0 without moving a score.
WEIGHTS_VERSION = "fitted-v1"

REGIME_MULTIPLIERS = {"hot": 1.0, "neutral": 1.0, "cold": 1.0}
# In a cold tape, compress toward the midpoint rather than scaling: score.md says
# most signals lose predictive power when the buyer pool has left, which flattens
# the ranking rather than shifting it down.
COLD_COMPRESSION = 0.5
CONTRADICTION_PENALTY = -20.0

# Below this, unique authors over total mentions implies coordinated posting.
AUTHOR_DIVERSITY_FLOOR = 0.25
COORDINATED_PILLAR_CAP = 40.0
# Unique daily speakers over members. Below this is a dead room with a big number.
SPEAKER_RATIO_FLOOR = 0.02

# Boost share over trade share. Above this, more of the token's visibility was
# bought than traded -- the mindshare is manufactured, and the pillar says so.
PAID_TILT_FLAG = 2.0

# Buys over buys+sells inside one window. 0.5 is balanced; these are the anchors
# the pillar scales between, not thresholds anything is rejected on.
BUY_PRESSURE_FLOOR = 0.40
BUY_PRESSURE_CEILING = 0.65
# A window ratio at or above this is pinned by construction rather than measured:
# a token younger than the longer window has identical counts in both, so the
# ratio equals the window ratio exactly. The pillar drops the component and says
# so rather than scoring the artefact.
WINDOW_SATURATION_EPSILON = 1e-9


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _scale(value: float | None, low: float, high: float) -> float | None:
    """Map a value onto 0-100 between two anchors. ``None`` in, ``None`` out."""
    if value is None:
        return None
    if high == low:
        return None
    return _clamp((value - low) / (high - low) * 100.0)


def _ratio(value: Any, total: Any) -> float | None:
    """``value / total``, or ``None`` when either is missing or the total is empty."""
    if value is None or total is None:
        return None
    try:
        value, total = float(value), float(total)
    except (TypeError, ValueError):
        return None
    return value / total if total > 0 else None


def _mean(parts: list[float | None]) -> float | None:
    """Average the components that exist. Returns ``None`` if none do.

    A missing component is skipped rather than counted as zero: pillar D scoring 30
    because two of its six inputs were never collected would be a measurement about
    the collector, not the token.
    """
    present = [p for p in parts if p is not None]
    return sum(present) / len(present) if present else None


@dataclass(frozen=True, slots=True)
class PillarScore:
    name: str
    score: float | None
    components: dict[str, float | None] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.score is not None


def _get(candidate: dict[str, Any], *path: str) -> Any:
    current: Any = candidate
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def attention_velocity(candidate: dict[str, Any]) -> PillarScore:
    """Pillar A -- acceleration, not volume."""
    x = candidate.get("social_x") or {}
    m6, m24 = x.get("mentions_6h"), x.get("mentions_24h")
    notes: list[str] = []

    slope = None
    if m6 is not None and m24 is not None and m24 > 0:
        # Rate over the last 6h against the trailing 24h rate. Above 1 is
        # accelerating; a decelerating curve at a high level is distribution.
        recent_rate = m6 / 6
        baseline_rate = m24 / 24
        ratio = recent_rate / baseline_rate if baseline_rate > 0 else None
        slope = _scale(ratio, 0.5, 3.0)
        if ratio is not None and ratio < 1.0:
            notes.append("mention curve decelerating at level -- distribution signature")

    diversity = None
    authors = x.get("unique_authors_24h")
    coordinated = False
    if authors is not None and m24 is not None and m24 > 0:
        ratio = authors / m24
        diversity = _scale(ratio, 0.1, 0.6)
        if ratio < AUTHOR_DIVERSITY_FLOOR:
            coordinated = True
            notes.append(f"author diversity {ratio:.2f} implies coordinated posting")

    reach = _scale(x.get("follower_weighted_reach"), 0.0, 5_000_000.0)
    tier1 = _scale(x.get("tier1_organic_engagements"), 0.0, 5.0)
    replies = _scale(x.get("reply_to_post_ratio"), 0.0, 1.5)
    if x.get("reply_to_post_ratio") == 0 and m24:
        notes.append("one-way posting with no replies -- manufactured presence")

    score = _mean([slope, diversity, reach, tier1, replies])
    if score is not None and coordinated:
        score = min(score, COORDINATED_PILLAR_CAP)
    return PillarScore(
        "attention_velocity",
        score,
        {
            "mention_slope": slope,
            "author_diversity": diversity,
            "reach_quality": reach,
            "tier1_crossover": tier1,
            "reply_ratio": replies,
        },
        tuple(notes),
    )


def community_depth(candidate: dict[str, Any]) -> PillarScore:
    """Pillar B -- speaker ratio matters more than membership."""
    tg = candidate.get("social_tg") or {}
    members, speakers = tg.get("members"), tg.get("unique_speakers_24h")
    notes: list[str] = []

    speaker_ratio = None
    if members is not None and speakers is not None and members > 0:
        ratio = speakers / members
        speaker_ratio = _scale(ratio, 0.0, 0.15)
        if ratio < SPEAKER_RATIO_FLOOR:
            notes.append(f"speaker ratio {ratio:.3f} -- dead room with a big number")

    growth = _scale(tg.get("member_growth_6h_pct"), 0.0, 200.0)
    chatter = _scale(tg.get("msgs_per_hour"), 0.0, 300.0)
    size = _scale(members, 0.0, 20_000.0)

    return PillarScore(
        "community_depth",
        _mean([speaker_ratio, growth, chatter, size]),
        {
            "speaker_ratio": speaker_ratio,
            "member_growth": growth,
            "messages_per_hour": chatter,
            "member_count": size,
        },
        tuple(notes),
    )


def lineage_meta_fit(candidate: dict[str, Any]) -> PillarScore:
    """Pillar C -- membership in a live meta, and position within it."""
    lineage = candidate.get("lineage") or {}
    trends = candidate.get("trends") or {}
    declared = candidate.get("socials_declared") or {}

    position_scores = {"first": 100.0, "second": 65.0, "derivative": 20.0}
    position = position_scores.get(lineage.get("position_in_meta"))
    in_meta = None if lineage.get("meta_tag") is None else 70.0

    # Cross-platform crossover: the strongest expansion signal in this pillar.
    crossover = _mean(
        [
            _scale(trends.get("google_trends_delta"), 0.0, 100.0),
            _scale(trends.get("tiktok_video_count_delta"), 0.0, 500.0),
        ]
    )

    # socials_declared is the best-evidenced feature in the whole schema: all three
    # present is a 17.4x graduation lift, Telegram alone 8.94x. Weighted here rather
    # than in a social pillar because it is launch metadata, not a social series.
    triple = [declared.get("telegram"), declared.get("x"), declared.get("website")]
    socials = None
    if any(v is not None for v in triple):
        present = sum(1 for v in triple if v is True)
        socials = {0: 5.0, 1: 45.0, 2: 65.0, 3: 90.0}[present]

    return PillarScore(
        "lineage_meta_fit",
        _mean([position, in_meta, crossover, socials]),
        {
            "position_in_meta": position,
            "in_live_meta": in_meta,
            "cross_platform_crossover": crossover,
            "socials_declared": socials,
        },
    )


def onchain_structure(candidate: dict[str, Any]) -> PillarScore:
    """Pillar D -- the pillar that mostly resolves on Phase 0 data."""
    holders = candidate.get("holders") or {}
    launch = candidate.get("launch") or {}
    flows = candidate.get("flows") or {}
    mcap = candidate.get("market_cap_usd")
    liquidity = candidate.get("liquidity_usd")
    volume = candidate.get("volume_24h_usd")
    notes: list[str] = []

    growth = _scale(holders.get("growth_6h_pct"), 0.0, 150.0)

    top10 = holders.get("top10_ex_lp_pct")
    concentration = None if top10 is None else _clamp(100.0 - top10 * 2.0)

    bundled = launch.get("bundled_supply_pct")
    bundling = None if bundled is None else _clamp(100.0 - bundled * 3.0)
    if bundled is not None and bundled > 25:
        notes.append(f"{bundled:.0f}% bundled at launch -- the float is an illusion")

    # Smart money counts only with a measured hit rate behind it (score.md: "Require
    # the hit rate, not just the label").
    hit_rate = flows.get("smart_money_hit_rate")
    entries = flows.get("smart_money_entries")
    smart = None
    if hit_rate is not None and entries is not None:
        smart = _clamp(hit_rate * 100.0 * min(1.0, entries / 5.0))

    turnover = None
    if volume is not None and mcap:
        ratio = volume / mcap
        # Constructive in the middle; very high turnover with a flat price is churn.
        turnover = _clamp(100.0 - abs(ratio - 1.5) * 40.0)

    depth = None
    if liquidity is not None and mcap:
        depth = _scale(liquidity / mcap * 100.0, 0.0, 15.0)

    return PillarScore(
        "onchain_structure",
        _mean([growth, concentration, bundling, smart, turnover, depth]),
        {
            "holder_growth": growth,
            "concentration": concentration,
            "bundle_sniper": bundling,
            "smart_money": smart,
            "turnover": turnover,
            "liquidity_depth": depth,
        },
        tuple(notes),
    )


def asymmetry_timing(candidate: dict[str, Any]) -> PillarScore:
    """Pillar E -- band, age and position on the listing ladder."""
    mcap = candidate.get("market_cap_usd")
    age_hours = candidate.get("age_hours")
    listings = candidate.get("listings") or []

    band = None
    if mcap is not None and mcap > 0:
        # Asymmetry concentrates low, and so does total loss. Score the band; the
        # risk pillars adjudicate.
        if mcap < 500_000:
            band = 90.0
        elif mcap < 2_000_000:
            band = 70.0
        elif mcap < 10_000_000:
            band = 45.0
        else:
            band = 20.0

    age = None
    if age_hours is not None:
        # The survival curve is brutally front-loaded; this is a placeholder shape
        # until Phase 2 supplies the calibrated curve.
        age = _clamp(100.0 - age_hours * 1.5) if age_hours < 72 else 20.0

    ladder = {"dex": 20.0, "aggregator": 45.0, "cex_perp": 70.0, "cex_spot": 95.0}
    rungs = [ladder[item] for item in listings if item in ladder]
    listing = max(rungs) if rungs else None

    return PillarScore(
        "asymmetry_timing",
        _mean([band, age, listing]),
        {"mcap_band": band, "age": age, "listing_ladder": listing},
    )


def mindshare(candidate: dict[str, Any]) -> PillarScore:
    """Share of the attention observed around the token (collectors/mindshare.py).

    Its prior weight was zero -- see :data:`MINDSHARE_PRIOR_WEIGHT` -- and it was
    computed and stored anyway, because a fit cannot weigh a feature nobody
    collected. The first fit gave it 0.08 in :data:`WEIGHTS`. It reads 24h volume,
    as Pillar D's turnover does, so the two weights are collinear and should be
    read together rather than apart.

    The interesting component is the last one. Mindshare that is bought and
    mindshare that is traded look identical in a share number and are opposite
    signals, so the ratio between them is separated out rather than blended in.
    """
    m = candidate.get("mindshare") or {}
    notes: list[str] = []

    # Percentile is already a 0-100 position within the measured universe, which
    # is exactly the shape a pillar wants. Share_pct is not: it is dominated by a
    # handful of tokens, so it is scaled rather than used raw.
    percentile = m.get("percentile")
    rank_score = None if percentile is None else _clamp(float(percentile))
    share_level = _scale(m.get("share_pct"), 0.0, 5.0)
    breadth = _scale(m.get("pair_count"), 1.0, 6.0)

    boost_share = _ratio(m.get("boost_total"), m.get("universe_boost_total"))
    trade_share = _ratio(m.get("txns_24h"), m.get("universe_txns_24h"))
    organic = None
    if boost_share is not None and trade_share is not None and trade_share > 0:
        tilt = boost_share / trade_share
        # At or below parity the attention is at least as traded as it is bought.
        organic = 100.0 if tilt <= 1.0 else _clamp(100.0 - (tilt - 1.0) * 50.0)
        if tilt >= PAID_TILT_FLAG:
            notes.append(
                f"boost share is {tilt:.1f}x trade share -- this mindshare is bought"
            )
    elif boost_share is not None and boost_share > 0 and trade_share is None:
        notes.append("paid boosts present but trade counts unknown -- tilt unmeasurable")

    return PillarScore(
        "mindshare",
        _mean([rank_score, share_level, breadth, organic]),
        {
            "universe_percentile": rank_score,
            "share_level": share_level,
            "venue_breadth": breadth,
            "organic_tilt": organic,
        },
        tuple(notes),
    )


def _pressure(buys: Any, sells: Any) -> float | None:
    """Buys over total trades in one window. ``None`` unless both sides are known.

    A missing sell count is not zero sells. Treating it as zero would report
    perfect buy pressure for a pool that simply did not answer, which is the
    single most flattering way to be wrong about a token.
    """
    if buys is None or sells is None:
        return None
    try:
        b, s = float(buys), float(sells)
    except (TypeError, ValueError):
        return None
    total = b + s
    return b / total if total > 0 else None


def _window_ratio(
    short: Any, short_hours: float, long: Any, long_hours: float
) -> tuple[float | None, bool]:
    """``(short rate / long rate, saturated)`` for two nested windows.

    The second element is the part that matters. The short window is contained in
    the long one, so a token younger than the *short* window has identical figures
    in both and the ratio equals ``long_hours / short_hours`` exactly -- 4.0 for
    6h-in-24h, 6.0 for 1h-in-6h. That is arithmetic about the token's age, not a
    measurement of its momentum, and calibration/backtest.py showed it reading as
    a strong signal precisely because it is an age proxy. Flagged here so the
    caller can drop it rather than score it. A token between the two windows is not
    pinned but is still read through its age, which only the caller can see.
    """
    short_rate = _ratio(short, short_hours)
    long_rate = _ratio(long, long_hours)
    if short_rate is None or long_rate is None or long_rate <= 0:
        return None, False
    ratio = short_rate / long_rate
    ceiling = long_hours / short_hours
    return ratio, ratio >= ceiling - WINDOW_SATURATION_EPSILON


def momentum_flow(candidate: dict[str, Any]) -> PillarScore:
    """Pillar G -- the shape of the last hour, not the level of the last day.

    prompts/score.md step 2 opens Pillar A with "measure acceleration, not volume"
    and closes the non-negotiables with "rate of change beats level". Until the
    `momentum` schema group existed there was nothing on a snapshot row to measure
    it *with*: every market field was a 24h level, and the only ratio available
    between two windows saturated on age.

    Four components, each a ratio the source's own numbers support:

    * **Buy pressure**, over an hour and over a day. A ratio inside one window,
      so it cannot be pinned by the token being young -- which is exactly what
      disqualified the old trade-count acceleration.
    * **Pressure trend**: the hour's buy pressure against the day's. Rising is a
      bid arriving; falling at a high level is the distribution shape Pillar A
      describes and Pillar D's cohort flow would confirm if it were collected.
    * **Volume acceleration**, 1h against 6h, *dropped until the six-hour window
      has filled* -- before that the ratio reads the token's age.
    * **Price slope**: the hour's move against the six-hour average hourly move,
      or the hour's move alone while the six-hour window is still filling.

    Weighted 0.00 into the composite -- see :data:`MOMENTUM_PRIOR_WEIGHT`. The fit
    behind :data:`WEIGHTS` had no momentum data at any trigger, so it could not
    weigh it either way.
    """
    m = candidate.get("momentum") or {}
    notes: list[str] = []

    pressure_1h = _pressure(m.get("buys_1h"), m.get("sells_1h"))
    pressure_24h = _pressure(m.get("buys_24h"), m.get("sells_24h"))
    buy_1h = _scale(pressure_1h, BUY_PRESSURE_FLOOR, BUY_PRESSURE_CEILING)
    buy_24h = _scale(pressure_24h, BUY_PRESSURE_FLOOR, BUY_PRESSURE_CEILING)

    trend = None
    if pressure_1h is not None and pressure_24h is not None:
        delta = pressure_1h - pressure_24h
        trend = _scale(delta, -0.15, 0.15)
        if delta <= -0.10 and pressure_24h >= 0.55:
            notes.append(
                f"buy pressure fell from {pressure_24h:.2f} (24h) to {pressure_1h:.2f} "
                "(1h) -- selling into the day's bid"
            )

    # A token younger than the six-hour window has not filled it: its 6h figures
    # cover `age` hours of trading, not six, so a sixth of them understates the
    # hourly rate by age/6. A perfectly flat 2h-old token then reads 3.0x
    # "acceleration" -- scored 100 -- where the same token at 8h reads 1.0x. The
    # two windows being *identical*, which _window_ratio flags, is only the
    # under-an-hour end of that range; the age on the candidate covers all of it.
    age_hours = candidate.get("age_hours")
    long_window_open = age_hours is not None and age_hours < 6.0

    accel_ratio, saturated = _window_ratio(
        m.get("volume_1h_usd"), 1.0, m.get("volume_6h_usd"), 6.0
    )
    acceleration = None
    if saturated:
        notes.append(
            "1h and 6h volume are identical -- the token is younger than six hours, "
            "so the acceleration is pinned by arithmetic and is not scored"
        )
    elif long_window_open and accel_ratio is not None:
        notes.append(
            f"the token is {age_hours:.1f}h old, younger than the six-hour window, so "
            "1h volume against the 6h rate reads its age and is not scored"
        )
    elif accel_ratio is not None:
        acceleration = _scale(accel_ratio, 0.3, 3.0)
        if accel_ratio < 0.5:
            notes.append(
                f"hourly volume is {accel_ratio:.2f}x the 6h rate -- attention is draining"
            )

    slope = None
    change_1h = m.get("price_change_1h_pct")
    change_6h = m.get("price_change_6h_pct")
    if change_1h is not None and change_6h is not None and not long_window_open:
        # The six-hour figure is a cumulative move; a sixth of it is its average
        # hour. Above that average the last hour is the fastest part of the move.
        hourly_average = change_6h / 6.0
        slope = _scale(change_1h - hourly_average, -10.0, 10.0)
    elif change_1h is not None:
        # No six-hour average to compare against -- none reported, or the window
        # has not filled -- so the hour's move is scored on its own.
        slope = _scale(change_1h, -10.0, 10.0)
    if change_1h is not None and change_6h is not None and change_6h > 25 and change_1h < 0:
        notes.append(
            f"up {change_6h:.0f}% over 6h and down {change_1h:.1f}% in the last "
            "hour -- the move is rolling over"
        )

    return PillarScore(
        "momentum_flow",
        _mean([buy_1h, buy_24h, trend, acceleration, slope]),
        {
            "buy_pressure_1h": buy_1h,
            "buy_pressure_24h": buy_24h,
            "pressure_trend": trend,
            "volume_acceleration": acceleration,
            "price_slope": slope,
        },
        tuple(notes),
    )


PILLARS = (
    attention_velocity,
    community_depth,
    lineage_meta_fit,
    onchain_structure,
    asymmetry_timing,
    mindshare,
    momentum_flow,
)


def composite(
    pillar_scores: dict[str, float | None], weights: dict[str, float] | None = None
) -> tuple[float | None, float]:
    """``(renormalised weighted mean, weight that resolved)`` over the pillars given.

    Renormalising over the pillars that resolved makes the number read as "of what
    can be seen"; how little that is comes back through the completeness multiplier
    rather than being buried here. A resolved set whose weights sum to zero -- which
    happens when only zero-weight pillars resolved, say lineage and momentum and
    nothing else -- has no composite at all. That is not a score of zero.

    Exposed separately from :func:`score_candidate` so a caller holding only stored
    pillar scores can re-derive the same number instead of writing a second version
    of this arithmetic.
    """
    weights = weights or WEIGHTS
    resolved = {
        name: score
        for name, score in pillar_scores.items()
        if score is not None and name in weights
    }
    resolved_weight = sum(weights[name] for name in resolved)
    if not resolved or resolved_weight <= 0:
        return None, resolved_weight
    return (
        sum(weights[name] * score for name, score in resolved.items()) / resolved_weight,
        resolved_weight,
    )


@dataclass(frozen=True, slots=True)
class PillarResult:
    pillars: tuple[PillarScore, ...]
    raw_score: float | None
    score: float | None
    data_completeness: float
    modifiers: tuple[str, ...]
    resolved_weight: float

    def pillar_scores(self) -> dict[str, float | None]:
        return {p.name: p.score for p in self.pillars}

    def notes(self) -> list[str]:
        return [note for p in self.pillars for note in p.notes]


def _contradiction(candidate: dict[str, Any], pillars: dict[str, float | None]) -> bool:
    """Attention peaking while early wallets distribute -- the local-top shape."""
    attention = pillars.get("attention_velocity")
    cohorts = _get(candidate, "flows", "net_flow_by_cohort") or {}
    if attention is None or not cohorts:
        return False
    large = cohorts.get("large") or cohorts.get("whale")
    small = cohorts.get("small") or cohorts.get("retail")
    if large is None or small is None:
        return False
    return attention >= 70 and large < 0 and small > 0


def score_candidate(
    candidate: dict[str, Any],
    *,
    regime: str | None = None,
    data_completeness: float | None = None,
    weights: dict[str, float] | None = None,
) -> PillarResult:
    """Weighted composite for one candidate, with the step-2 modifiers applied.

    ``weights`` defaults to :data:`WEIGHTS`. Passing another vector is for
    calibration, which has to score a candidate vector exactly the way the board
    would before it may replace the one in force.
    """
    pillars = tuple(fn(candidate) for fn in PILLARS)
    by_name = {p.name: p.score for p in pillars}
    raw, resolved_weight = composite(by_name, weights)

    modifiers: list[str] = []
    score = raw

    if score is not None:
        completeness = (
            data_completeness
            if data_completeness is not None
            else candidate.get("data_completeness")
        )
        completeness = 1.0 if completeness is None else float(completeness)
        if completeness < 1.0:
            score *= completeness
            modifiers.append(f"data_completeness x{completeness:.2f}")

        if regime == "cold":
            score = 50.0 + (score - 50.0) * COLD_COMPRESSION
            modifiers.append("cold regime: compressed toward midpoint")
        elif regime:
            modifiers.append(f"{regime} regime: no adjustment")

        if _contradiction(candidate, by_name):
            score = _clamp(score + CONTRADICTION_PENALTY)
            modifiers.append(f"contradiction penalty {CONTRADICTION_PENALTY:+.0f}")

        score = _clamp(score)

    return PillarResult(
        pillars=pillars,
        raw_score=raw,
        score=score,
        data_completeness=float(data_completeness or candidate.get("data_completeness") or 0.0),
        modifiers=tuple(modifiers),
        resolved_weight=resolved_weight,
    )
