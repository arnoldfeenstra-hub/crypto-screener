"""Does any collected feature separate the winners from the graveyard?

This is not :mod:`calibration.fit`. That module fits a weight vector and is gated
on Phase 0's exit criteria, because a fitted vector goes into
``prompts/score.md`` and changes what the screener says. This module fits nothing
and changes no weight. It asks one question per feature -- *did rows scoring high
on this go on to do better?* -- and answers it with the sample that exists, which
is small, so every answer carries the interval that says how small.

Why a separate module, and why the checks below
-----------------------------------------------
The first honest run of this found a feature with an apparent AUC of 0.70 across
three horizons: the trade-count acceleration ``(txns_6h/6) / (txns_24h/24)``. It
looked like the best signal in the dataset and it was worth nothing. Two of the
checks here are why that is now known rather than shipped.

**Tie mass.** 39 of 76 rows sat at *exactly* 4.0 -- and 65% of 239 rows do today,
so the artefact did not wash out with more data. A token younger than six hours has
``txns_6h == txns_24h`` -- the same trades, counted twice -- so the ratio is pinned
at ``24/6`` by arithmetic. Most of the sample was not ranked by the feature at all,
and AUC counts ties as half-wins, which hides that.

**Stratified AUC.** Inside a single age band the same feature scored 0.500 --
nothing. Pooled, it scored 0.70 because young tokens both surge more often *and*
pin at the ceiling. It was age wearing a disguise. A feature whose pooled AUC
survives its strata is measuring something; one whose AUC collapses inside every
stratum is measuring the control.

Both are reported for every feature, always, because the shape of that mistake is
not specific to that feature: a Phase 0 sample is small, its rows are correlated,
and any ratio between two nested time windows saturates the same way.

What this module will not do
----------------------------
It will not write a weight. ``.claude/rules/stats.md`` admits only fitted
coefficients that cleared the Phase 0 gate into ``prompts/score.md``, and nothing
here clears it. Every number is reported with its out-of-sample split, its base
rate and its interval, and the verdict says in words that the sample is too small
to conclude from. Read it as a list of leads to collect against, not as a result.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from calibration.fit import (
    MIN_DEAD_PER_SURVIVOR,
    MIN_TRIGGERED_TOKENS,
    auc,
    top_decile_lift,
    wilson_interval,
)

# A feature is not read as a ranking when this much of the sample shares one
# value. At 0.30 the modal value is nearly a third of the rows, and the pairs
# involving it are scored as coin flips rather than as separations.
TIE_MASS_WARNING = 0.30

# Below this many rows in a stratum, a within-stratum AUC is noise and is
# reported as unmeasurable rather than as a number.
MIN_STRATUM_ROWS = 12

# Age bands, in minutes, used as the control when stratifying. Chosen to bracket
# the window boundaries the ratios saturate on (1h, 6h) rather than round numbers.
AGE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("<1h", 0.0, 60.0),
    ("1-6h", 60.0, 360.0),
    ("6-48h", 360.0, 2880.0),
    (">48h", 2880.0, math.inf),
)

# The feature the strata are cut on. Stratifying on age and then asking whether
# age survives stratification is circular -- the check removes exactly the
# variation it is testing -- so this one feature is exempted and labelled as the
# control instead of being reported as confounded with itself.
CONTROL_FEATURE = "age_minutes"

# The default outcome. A 1.5x from the snapshot price inside six hours is a
# short-horizon surge, which is the question the screener is actually asked. It
# is emphatically *not* the pump.fun graduation rate in CLAUDE.md: these tokens
# are sampled above $250k, so they have already cleared that bar, and the base
# rate here is tens of percent rather than ~2%. The two numbers answer different
# questions and must never be compared.
DEFAULT_LABEL = "max_multiple_6h"
DEFAULT_THRESHOLD = 1.5


@dataclass(frozen=True)
class BacktestRow:
    """One snapshot, its forward outcome, and the time it was taken."""

    snapshot_id: str
    ts: int
    chain: str | None
    ticker: str | None
    features: dict[str, float | None]
    outcome: float | None
    age_minutes: float | None = None
    regime: str | None = None


def _div(numerator: Any, denominator: Any) -> float | None:
    """``a / b``, or ``None`` if either is missing or the denominator is empty.

    Never returns 0.0 for a missing input. A token whose sell count was not
    reported has an unknown buy share, not a buy share of zero.
    """
    if numerator is None or denominator is None:
        return None
    try:
        a, b = float(numerator), float(denominator)
    except (TypeError, ValueError):
        return None
    if b == 0 or not math.isfinite(a) or not math.isfinite(b):
        return None
    value = a / b
    return value if math.isfinite(value) else None


def _pressure(buys: Any, sells: Any) -> float | None:
    if buys is None or sells is None:
        return None
    return _div(buys, (buys or 0) + (sells or 0))


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _flag(value: Any) -> float | None:
    """A boolean as 1.0/0.0, keeping unknown as ``None`` rather than as 0."""
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    return None


def extract_features(
    row: dict[str, Any], safety: dict[str, Any] | None = None
) -> dict[str, float | None]:
    """Everything a snapshot row can be ranked by, derived at read time.

    Every entry is a pure function of stored columns, so a feature added here
    applies retroactively to rows written before it existed -- which is the reason
    the schema stores raw counts and derives nothing at write time.
    """
    safety = safety or {}
    mcap = _num(row.get("market_mcap_usd"))
    volume = _num(row.get("market_volume_24h_usd"))
    liquidity = _num(row.get("market_liquidity_usd"))
    txns_24h = _num(row.get("mindshare_txns_24h"))
    txns_6h = _num(row.get("mindshare_txns_6h"))
    declared = [
        row.get("socials_declared_telegram"),
        row.get("socials_declared_x"),
        row.get("socials_declared_website"),
    ]

    features: dict[str, float | None] = {
        # --- levels -------------------------------------------------------
        "mcap_usd": mcap,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "age_minutes": _num(row.get("age_at_trigger_minutes")),
        "turnover_24h": _div(volume, mcap),
        "liquidity_over_mcap": _div(liquidity, mcap),
        "fdv_over_mcap": _div(_num(row.get("market_fdv_usd")), mcap),
        "value_per_trade": _div(volume, txns_24h),
        # --- the saturating ratio, kept so the checks can show why ---------
        # Deliberately still here. It is the feature that looked like the best
        # signal in the sample and is an age proxy, and leaving it in means the
        # tie-mass and stratified columns demonstrate that on every run rather
        # than in a commit message nobody reads.
        "txn_accel_6h_24h": _div(_div(txns_6h, 6.0), _div(txns_24h, 24.0)),
        # --- momentum group (schema 7) ------------------------------------
        "buy_pressure_1h": _pressure(row.get("momentum_buys_1h"), row.get("momentum_sells_1h")),
        "buy_pressure_24h": _pressure(row.get("momentum_buys_24h"), row.get("momentum_sells_24h")),
        "volume_accel_1h_6h": _div(
            _num(row.get("momentum_volume_1h_usd")),
            _div(_num(row.get("momentum_volume_6h_usd")), 6.0),
        ),
        "price_change_5m_pct": _num(row.get("momentum_price_change_5m_pct")),
        "price_change_1h_pct": _num(row.get("momentum_price_change_1h_pct")),
        "price_change_6h_pct": _num(row.get("momentum_price_change_6h_pct")),
        "price_change_24h_pct": _num(row.get("momentum_price_change_24h_pct")),
        # --- attention -----------------------------------------------------
        "mindshare_share_pct": _num(row.get("mindshare_share_pct")),
        "mindshare_percentile": _num(row.get("mindshare_percentile")),
        "pair_count": _num(row.get("mindshare_pair_count")),
        "boost_total": _num(row.get("mindshare_boost_total")),
        # --- launch metadata, the published-evidence features --------------
        "socials_declared_n": (
            float(sum(1 for v in declared if v is True))
            if any(v is not None for v in declared)
            else None
        ),
        "declared_x": _flag(row.get("socials_declared_x")),
        "declared_telegram": _flag(row.get("socials_declared_telegram")),
        "declared_website": _flag(row.get("socials_declared_website")),
        # --- safety, where it was measured ---------------------------------
        "holder_count": _num(safety.get("holder_count")),
        "top10_ex_lp_pct": _num(safety.get("top10_ex_lp_pct")),
        "lp_locked_pct": _num(safety.get("lp_locked_pct")),
        # --- what the screener currently says ------------------------------
        "data_completeness": _num(row.get("data_completeness")),
    }
    return features


def auc_interval(
    labels: Sequence[int], scores: Sequence[float], z: float = 1.96
) -> tuple[float, float] | None:
    """Hanley-McNeil confidence interval for AUC. ``None`` when one class is absent.

    The closed form rather than a bootstrap, because at this sample size the
    interval's job is to be visibly wide rather than to be precise about how wide.
    It assumes independent rows, which these are not quite -- tokens launched in
    the same hour share a regime -- so the true interval is wider still. Reported
    anyway: an interval that understates its width still refutes a point estimate
    read as a result.

    Perfect separation is special-cased. The closed form has zero variance there,
    which would print an AUC of 1.0 from four rows as ``[1.00, 1.00]``.
    """
    area = auc(labels, scores)
    if area is None:
        return None
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives < 1 or negatives < 1:
        return None
    q1 = area / (2.0 - area)
    q2 = 2.0 * area * area / (1.0 + area)
    variance = (
        area * (1.0 - area)
        + (positives - 1) * (q1 - area * area)
        + (negatives - 1) * (q2 - area * area)
    ) / (positives * negatives)
    if variance <= 0:
        # Perfect separation. The closed form collapses to zero variance and would
        # report an AUC of 1.0 as exact, which is the most confident thing this
        # module could possibly say and would be said on the smallest samples.
        # Every pair is concordant, so bound that proportion instead: a Wilson
        # interval on pairs/pairs is wide when there are few pairs and narrow when
        # there are many, which is the behaviour wanted.
        pairs = positives * negatives
        low, _ = wilson_interval(pairs, pairs, z)
        return (low, 1.0) if area >= 0.5 else (0.0, 1.0 - low)
    half = z * math.sqrt(variance)
    return (max(0.0, area - half), min(1.0, area + half))


def tie_mass(values: Sequence[float]) -> tuple[float, float | None]:
    """``(share of rows at the modal value, that value)``.

    High tie mass is the failure mode AUC hides. Ties are scored as half-wins, so
    a feature that assigns one value to a third of the sample can post a
    respectable AUC while ranking a third of the sample not at all.
    """
    if not values:
        return (0.0, None)
    counts: dict[float, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    modal_value, modal_count = max(counts.items(), key=lambda kv: kv[1])
    return (modal_count / len(values), modal_value)


def _band(age_minutes: float | None) -> str | None:
    if age_minutes is None:
        return None
    for name, low, high in AGE_BANDS:
        if low <= age_minutes < high:
            return name
    return None


@dataclass(frozen=True)
class FeatureResult:
    """One feature's separation, and every reason to distrust it."""

    name: str
    n: int
    positives: int
    area: float | None
    interval: tuple[float, float] | None
    out_of_sample_area: float | None
    decile_lift: float | None
    tie_share: float
    tie_value: float | None
    strata: dict[str, float | None] = field(default_factory=dict)
    # The out-of-sample AUC's own interval. .claude/rules/stats.md asks for an
    # interval on every reported figure, and this is the one that gets quoted.
    out_of_sample_interval: tuple[float, float] | None = None

    @property
    def separates(self) -> bool:
        """The interval excludes 0.5 -- the feature ordered the outcome at all."""
        if self.interval is None:
            return False
        low, high = self.interval
        return low > 0.5 or high < 0.5

    @property
    def holds_out_of_sample(self) -> bool:
        """The direction learned on the earlier rows still orders the later ones.

        ``out_of_sample_area`` is scored with the training half's direction, so
        above 0.5 means that direction held on rows it never saw. ``separates`` is
        computed over every row, held-out ones included, so without this check a
        feature whose later rows ran the other way was still called a lead that
        "separated out of sample".
        """
        return self.out_of_sample_area is not None and self.out_of_sample_area > 0.5

    @property
    def is_control(self) -> bool:
        """The feature the strata are cut on. It cannot be checked against itself."""
        return self.name == CONTROL_FEATURE

    @property
    def survives_strata(self) -> bool:
        """Every measurable age band points the same way the pooled figure does.

        This is the check that disqualified the trade-count acceleration. A pooled
        AUC of 0.70 built entirely out of the difference *between* age bands, with
        0.50 inside each of them, is a measurement of age.

        The control feature is exempt, and not as a favour: stratifying on age
        removes most of age's variation by construction, so asking age to survive
        its own strata asks it to predict what has been held constant. Its bands
        are still printed -- a monotone run across them is the readable part.
        """
        if self.is_control:
            return True
        measured = [v for v in self.strata.values() if v is not None]
        if not measured or self.area is None:
            return False
        direction = 1.0 if self.area >= 0.5 else -1.0
        return all((value - 0.5) * direction > 0.02 for value in measured)

    @property
    def degenerate(self) -> bool:
        return self.tie_share >= TIE_MASS_WARNING

    def verdict(self) -> str:
        if self.area is None:
            return "unmeasurable"
        if self.is_control:
            direction = "younger surges more" if self.area < 0.5 else "older surges more"
            return (
                f"control variable -- {direction}; the strata are cut on it, so it is "
                "not checked against itself"
            )
        if self.degenerate and not self.survives_strata:
            return "artefact: pinned value, and no separation inside an age band"
        if self.degenerate:
            return f"suspect: {self.tie_share:.0%} of rows share one value"
        if not self.separates:
            return "no separation (interval spans 0.5)"
        if not self.survives_strata:
            return "confounded with age: separation does not survive stratification"
        if self.out_of_sample_area is None:
            return "separates in sample only -- no out-of-sample split to confirm it"
        if not self.holds_out_of_sample:
            return (
                "separates in sample only -- the direction learned on the earlier "
                "rows did not hold on the later ones"
            )
        return "separates, out of sample, within age bands -- a lead, not a result"

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.name,
            "n": self.n,
            "positives": self.positives,
            "auc": self.area,
            "auc_ci": list(self.interval) if self.interval else None,
            "auc_out_of_sample": self.out_of_sample_area,
            "auc_out_of_sample_ci": (
                list(self.out_of_sample_interval) if self.out_of_sample_interval else None
            ),
            "holds_out_of_sample": self.holds_out_of_sample,
            "top_decile_lift": self.decile_lift,
            "tie_share": self.tie_share,
            "tie_value": self.tie_value,
            "auc_by_age_band": self.strata,
            "separates": self.separates,
            "survives_strata": self.survives_strata,
            "is_control": self.is_control,
            "verdict": self.verdict(),
        }


def evaluate_feature(
    name: str, rows: Sequence[BacktestRow], *, test_fraction: float = 0.3
) -> FeatureResult:
    """Score one feature against the outcome already attached to each row.

    Rows missing the feature are dropped from *this* feature's evaluation and
    from nothing else. That is the one place ``.claude/rules/stats.md``'s "do not
    drop rows with missing features" gives way, and it gives way narrowly: a
    univariate AUC over rows where the feature is null is not a smaller
    measurement, it is not a measurement. The count that survived is reported
    beside every figure so the reader can see which features were answered by
    half the sample.
    """
    usable = [r for r in rows if r.features.get(name) is not None and r.outcome is not None]
    usable = sorted(usable, key=lambda r: r.ts)
    labels = [int(r.outcome or 0) for r in usable]
    scores = [float(r.features[name]) for r in usable]  # type: ignore[arg-type]

    share, value = tie_mass(scores)
    positives = sum(labels)
    if not usable or positives in (0, len(labels)):
        return FeatureResult(name, len(usable), positives, None, None, None, None, share, value)

    area = auc(labels, scores)
    interval = auc_interval(labels, scores)
    # Taken at the tail the feature predicts from. top_decile_lift ranks highest
    # first, so for a lower-is-better feature the raw top decile is its *worst*
    # decile -- which read as a lift of 0.00 on the one lead the sample has.
    pooled_direction = 1.0 if area is None or area >= 0.5 else -1.0
    lift = top_decile_lift(labels, [pooled_direction * s for s in scores])

    # Forward in time, never at random (.claude/rules/stats.md). The direction is
    # taken from the training half only; the test half is scored with that
    # direction and never consulted to choose it.
    cut = int(len(usable) * (1.0 - test_fraction))
    train, test = usable[:cut], usable[cut:]
    out_of_sample = None
    out_of_sample_interval = None
    train_labels = [int(r.outcome or 0) for r in train]
    test_labels = [int(r.outcome or 0) for r in test]
    splits_usable = (
        bool(train)
        and bool(test)
        and 0 < sum(train_labels) < len(train_labels)
        and 0 < sum(test_labels) < len(test_labels)
    )
    if splits_usable:
        train_area = auc(train_labels, [float(r.features[name]) for r in train])  # type: ignore[arg-type]
        direction = 1.0 if (train_area is None or train_area >= 0.5) else -1.0
        oriented = [direction * float(r.features[name]) for r in test]  # type: ignore[arg-type]
        out_of_sample = auc(test_labels, oriented)
        out_of_sample_interval = auc_interval(test_labels, oriented)

    strata: dict[str, float | None] = {}
    for band_name, _, _ in AGE_BANDS:
        in_band = [r for r in usable if _band(r.age_minutes) == band_name]
        band_labels = [int(r.outcome or 0) for r in in_band]
        if len(in_band) < MIN_STRATUM_ROWS or sum(band_labels) in (0, len(band_labels)):
            strata[band_name] = None
            continue
        strata[band_name] = auc(band_labels, [float(r.features[name]) for r in in_band])  # type: ignore[arg-type]

    return FeatureResult(
        name=name,
        n=len(usable),
        positives=positives,
        area=area,
        interval=interval,
        out_of_sample_area=out_of_sample,
        decile_lift=lift,
        tie_share=share,
        tie_value=value,
        strata=strata,
        out_of_sample_interval=out_of_sample_interval,
    )


@dataclass(frozen=True)
class BacktestReport:
    label: str
    threshold: float
    rows: int
    positives: int
    results: tuple[FeatureResult, ...]
    gate_met: bool
    triggered_tokens: int
    dead_per_survivor: float | None
    complete_social: int = 0

    @property
    def base(self) -> float:
        return self.positives / self.rows if self.rows else 0.0

    def leads(self) -> tuple[FeatureResult, ...]:
        """Features that separated, out of sample, inside an age band.

        "Lead" is the strongest word the sample supports. Nothing here is fitted,
        nothing here is a weight, and at this row count a lead is a thing to keep
        collecting against.
        """
        return tuple(
            r
            for r in self.results
            if r.separates
            and r.holds_out_of_sample
            and r.survives_strata
            and not r.degenerate
            and not r.is_control
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "threshold": self.threshold,
            "rows": self.rows,
            "positives": self.positives,
            "base_rate": self.base,
            "base_rate_ci": list(wilson_interval(self.positives, self.rows)) if self.rows else None,
            "phase0_gate_met": self.gate_met,
            "triggered_tokens": self.triggered_tokens,
            "complete_social_series": self.complete_social,
            "min_triggered_tokens": MIN_TRIGGERED_TOKENS,
            "dead_per_survivor": self.dead_per_survivor,
            "min_dead_per_survivor": MIN_DEAD_PER_SURVIVOR,
            "is_calibration": False,
            "features": [r.to_dict() for r in self.results],
            "leads": [r.name for r in self.leads()],
            "verdict": self.verdict(),
        }

    def verdict(self) -> str:
        if not self.rows:
            return (
                "No rows with a resolved outcome. Nothing has been measured; run the "
                "collector and come back."
            )
        low, high = wilson_interval(self.positives, self.rows)
        leads = self.leads()
        head = (
            f"{self.rows} tokens, {self.positives} reached {self.label} "
            f">= {self.threshold:g}x -- a base rate of {self.base:.1%} "
            f"[{low:.1%}, {high:.1%}]. "
        )
        if not self.gate_met:
            ratio = (
                "no survivors yet"
                if self.dead_per_survivor is None
                else f"{self.dead_per_survivor:.1f}"
            )
            head += (
                f"Phase 0's gate is NOT met ({self.complete_social}/"
                f"{MIN_TRIGGERED_TOKENS} triggered tokens with a complete social "
                f"series, {ratio}/{MIN_DEAD_PER_SURVIVOR} dead per survivor), so "
                "nothing below is a calibration and no weight in prompts/score.md may "
                "be changed on it. "
            )
        if leads:
            head += (
                "Features that separated out of sample and survived age "
                f"stratification: {', '.join(r.name for r in leads)}. Treat them as "
                "leads to collect against, not as a measured edge."
            )
        else:
            head += (
                "No feature separated out of sample and survived age stratification. "
                "On this sample the honest statement is that nothing has been "
                "measured."
            )
        return head


def build_rows(
    snapshots: Sequence[dict[str, Any]],
    labels_for: Callable[[str], dict[str, Any] | None],
    *,
    safety_for: Callable[[str], dict[str, Any] | None] | None = None,
    label: str = DEFAULT_LABEL,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[BacktestRow]:
    """Join snapshots to their forward labels and derive every feature.

    A snapshot whose label has not been filled yet is dropped, not defaulted. A
    horizon that has not elapsed is null (the outcome tracker's own rule), and a
    null read as a zero would be a token recorded as having gone nowhere when in
    fact nobody has looked yet.
    """
    out: list[BacktestRow] = []
    for snap in snapshots:
        snapshot_id = snap["snapshot_id"]
        label_row = labels_for(snapshot_id) or {}
        raw = label_row.get(label)
        if raw is None:
            continue
        outcome = float(bool(raw)) if isinstance(raw, bool) else float(raw >= threshold)
        safety = (safety_for(snapshot_id) if safety_for else None) or {}
        out.append(
            BacktestRow(
                snapshot_id=snapshot_id,
                ts=int(snap["ts"]),
                chain=snap.get("chain"),
                ticker=snap.get("ticker"),
                features=extract_features(snap, safety),
                outcome=outcome,
                age_minutes=_num(snap.get("age_at_trigger_minutes")),
                regime=snap.get("regime"),
            )
        )
    out.sort(key=lambda r: r.ts)
    return out


def run(
    rows: Sequence[BacktestRow],
    *,
    label: str = DEFAULT_LABEL,
    threshold: float = DEFAULT_THRESHOLD,
    triggered_tokens: int = 0,
    dead_per_survivor: float | None = None,
    complete_social: int = 0,
) -> BacktestReport:
    names: list[str] = []
    for row in rows:
        for name in row.features:
            if name not in names:
                names.append(name)
    results = tuple(evaluate_feature(name, rows) for name in names)
    # Strongest separation first, in either direction, so a feature that predicts
    # the outcome backwards is as visible as one that predicts it forwards. An AUC
    # of exactly 0.0 is the strongest backwards result there is, not a missing one.
    results = tuple(
        sorted(
            results,
            key=lambda r: abs(r.area - 0.5) if r.area is not None else 0.0,
            reverse=True,
        )
    )
    positives = sum(int(r.outcome or 0) for r in rows)
    # Phase 0's gate as calibration.fit.ExitCriteria states it: 300 triggered
    # tokens *with a complete social series*, not 300 snapshots. Counting
    # snapshots alone let this report call the gate met while the calibration
    # panel beside it, reading the same dataset, said it was not.
    gate_met = (
        complete_social >= MIN_TRIGGERED_TOKENS
        and dead_per_survivor is not None
        and dead_per_survivor >= MIN_DEAD_PER_SURVIVOR
    )
    return BacktestReport(
        label=label,
        threshold=threshold,
        rows=len(rows),
        positives=positives,
        results=results,
        gate_met=gate_met,
        triggered_tokens=triggered_tokens,
        dead_per_survivor=dead_per_survivor,
        complete_social=complete_social,
    )


def rows_from_store(
    store: Any,
    *,
    label: str = DEFAULT_LABEL,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = 5000,
) -> list[BacktestRow]:
    return build_rows(
        store.recent_snapshots(limit),
        store.latest_labels,
        safety_for=store.latest_safety,
        label=label,
        threshold=threshold,
    )


def render(report: BacktestReport) -> str:
    """The report as text. Every figure carries what it is worth beside it."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("EXPLORATORY BACKTEST -- not a calibration, and no weight is changed by it")
    lines.append("=" * 78)
    lines.append("")
    lines.append(report.verdict())
    lines.append("")
    lines.append(
        f"{'feature':24s} {'n':>4s} {'AUC':>6s} {'95% CI':>15s} {'OOS':>6s} "
        f"{'lift':>5s} {'tied':>5s}  verdict"
    )
    lines.append("-" * 110)
    for result in report.results:
        area = f"{result.area:.3f}" if result.area is not None else "  -  "
        interval = (
            f"[{result.interval[0]:.2f},{result.interval[1]:.2f}]"
            if result.interval
            else "       -       "
        )
        oos = (
            f"{result.out_of_sample_area:.3f}"
            if result.out_of_sample_area is not None
            else "  -  "
        )
        lift = f"{result.decile_lift:.2f}" if result.decile_lift is not None else "  -  "
        lines.append(
            f"{result.name:24s} {result.n:4d} {area:>6s} {interval:>15s} {oos:>6s} "
            f"{lift:>5s} {result.tie_share:5.0%}  {result.verdict()}"
        )
    lines.append("")
    lines.append("AUC by age band (the control). A pooled figure that collapses here was age:")
    header = "  " + f"{'feature':24s}" + "".join(f"{name:>10s}" for name, _, _ in AGE_BANDS)
    lines.append(header)
    for result in report.results[:12]:
        cells = "".join(
            f"{(f'{v:.3f}' if v is not None else '-'):>10s}" for v in result.strata.values()
        )
        lines.append(f"  {result.name:24s}{cells}")
    lines.append("")
    lines.append(
        "Read the intervals, not the point estimates. A sample this size makes every "
        "AUC here compatible with a wide range of truths, and the rows are not "
        "independent -- tokens launched in the same hour share a regime and often a "
        "deployer -- so the real intervals are wider than the printed ones."
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure whether any collected feature separates forward outcomes."
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    from collectors.config import load_config
    from collectors.store import Store  # local: keeps DuckDB off the import path

    config = load_config()
    with Store(args.db or str(config.db_path), read_only=True) as store:
        rows = rows_from_store(store, label=args.label, threshold=args.threshold)
        survivors, dead = store.survivor_counts("7d")
        report = run(
            rows,
            label=args.label,
            threshold=args.threshold,
            triggered_tokens=store.snapshot_count(),
            dead_per_survivor=(dead / survivors) if survivors else None,
            complete_social=store.snapshots_with_complete_social(),
        )

    print(json.dumps(report.to_dict(), indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
