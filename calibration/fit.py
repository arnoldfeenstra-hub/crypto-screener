"""Phase 2 -- model fitting.

.claude/rules/stats.md is binding here, and the two rules that shape this module
are the ones easiest to break by accident:

**Split forward in time, never at random.** A random split leaks the future --
tokens from the same launch hour share a regime, a meta, and often a deployer, so a
shuffled split lets the model see the answer. :func:`time_split` sorts by snapshot
time and cuts; there is no shuffle parameter anywhere.

**Group by deployer.** The same deployer's tokens in both train and test is
leakage even under a time split, because a deployer's behaviour is the feature.
:func:`time_split` moves any deployer straddling the cut entirely into train.

**The gate is real.** Phase 0 exits at >=300 triggered tokens with complete social
series and >=20 dead per survivor. :func:`fit` refuses below that, because with a
base rate near 2% a 300-token sample is roughly six positive events, and a model
fitted on six events is a description of six events. Pass ``force=True`` to fit
anyway -- it is recorded on the result and every downstream report says so.

The logistic regression is plain gradient descent in pure Python. numpy and
scikit-learn are not dependencies here on purpose: the whole fit is a handful of
features over a few hundred rows, and a readable implementation whose every step is
visible is worth more at this scale than a faster opaque one.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from scoring.pillars import WEIGHTS, composite, score_candidate

PHASE = "2"

MIN_TRIGGERED_TOKENS = 300
MIN_DEAD_PER_SURVIVOR = 20
PUBLISHED_CONCORDANCE_BENCHMARK = 0.858

# The pillars a fit sees. `mindshare` is here even though its prior weight in
# prompts/score.md is 0.00, and that is the point: the prior is zero because nobody
# has fitted it, and it can only ever be fitted if the fitter is given it. Its
# fitted coefficient comes out of normalised_pillar_weights() alongside the other
# five, which is the moment its weight stops being a guess.
#
# Two cautions carried from prompts/score.md: mindshare and onchain_structure both
# read 24h volume, so their coefficients are collinear and must not be read
# independently; and momentum_flow reads the same volume over shorter windows, so
# it is collinear with both.
FEATURE_NAMES = (
    "attention_velocity",
    "community_depth",
    "lineage_meta_fit",
    "onchain_structure",
    "asymmetry_timing",
    "mindshare",
    "momentum_flow",
)


# The BUILD_BRIEF.md section 2 labels a model can be fitted against, each as a
# yes/no outcome. A multiple is read as a doubling at every horizon -- the surge the
# screener exists to find -- so the horizons answer one question and can be
# compared (section 2: "fit five models and look at which horizon is actually
# predictable"). Fixed here, before any fit is looked at, so the threshold cannot
# be tuned to whichever horizon happens to look best.
CALIBRATION_LABELS: tuple[tuple[str, float | None], ...] = (
    ("max_multiple_1h", 2.0),
    ("max_multiple_6h", 2.0),
    ("max_multiple_24h", 2.0),
    ("max_multiple_72h", 2.0),
    ("max_multiple_7d", 2.0),
    ("survived_24h", None),
    ("survived_7d", None),
)


@dataclass(frozen=True)
class ExitCriteria:
    """Phase 0's exit test, evaluated against the dataset as it stands."""

    triggered_tokens: int
    survivors: int
    dead: int
    complete_social: int = 0

    @property
    def dead_per_survivor(self) -> float | None:
        return self.dead / self.survivors if self.survivors else None

    @property
    def met(self) -> bool:
        ratio = self.dead_per_survivor
        return (
            self.complete_social >= MIN_TRIGGERED_TOKENS
            and ratio is not None
            and ratio >= MIN_DEAD_PER_SURVIVOR
        )

    def explain(self) -> str:
        ratio = self.dead_per_survivor
        ratio_text = f"{ratio:.1f}" if ratio is not None else "n/a (no survivors yet)"
        verdict = "met" if self.met else "NOT met, keep collecting"
        return (
            f"triggered={self.triggered_tokens} "
            f"complete_social={self.complete_social}/{MIN_TRIGGERED_TOKENS} "
            f"dead_per_survivor={ratio_text}/{MIN_DEAD_PER_SURVIVOR} -> {verdict}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered_tokens": self.triggered_tokens,
            "complete_social": self.complete_social,
            "survivors": self.survivors,
            "dead": self.dead,
            "dead_per_survivor": self.dead_per_survivor,
            "min_triggered_tokens": MIN_TRIGGERED_TOKENS,
            "min_dead_per_survivor": MIN_DEAD_PER_SURVIVOR,
            "met": self.met,
            "explanation": self.explain(),
        }


def check_exit_criteria(
    triggered: int, survivors: int, dead: int, complete_social: int = 0
) -> ExitCriteria:
    return ExitCriteria(
        triggered_tokens=triggered,
        survivors=survivors,
        dead=dead,
        complete_social=complete_social,
    )


@dataclass(frozen=True)
class Row:
    """One training row: pillar features, an outcome, and its grouping keys."""

    features: dict[str, float | None]
    label: int
    ts: int
    deployer: str | None = None
    meta_tag: str | None = None
    regime: str | None = None
    snapshot_id: str | None = None


# --- metrics ----------------------------------------------------------------


def auc(labels: Sequence[int], scores: Sequence[float]) -> float | None:
    """Area under the ROC curve, by rank. ``None`` if one class is absent.

    Ties are averaged, which matters here: a model that cannot separate anything
    produces many equal scores, and counting ties as wins would flatter it to 1.0.
    """
    pairs = sorted(zip(scores, labels, strict=True), key=lambda p: p[0])
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None

    ranks: list[float] = [0.0] * len(pairs)
    index = 0
    while index < len(pairs):
        end = index
        while end + 1 < len(pairs) and pairs[end + 1][0] == pairs[index][0]:
            end += 1
        average_rank = (index + end) / 2 + 1
        for position in range(index, end + 1):
            ranks[position] = average_rank
        index = end + 1

    positive_rank_sum = sum(r for r, (_, label) in zip(ranks, pairs, strict=True) if label)
    return (positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def concordance(labels: Sequence[int], scores: Sequence[float]) -> float | None:
    """Concordance index. For a binary outcome this equals the AUC.

    Named separately because CLAUDE.md's benchmark is quoted as a concordance
    (0.858, from a Cox model), and comparing our number to it should be explicit
    about being the same quantity rather than silently assuming it.
    """
    return auc(labels, scores)


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


def base_rate(labels: Sequence[int]) -> float:
    return sum(labels) / len(labels) if labels else 0.0


def top_decile_lift(labels: Sequence[int], scores: Sequence[float]) -> float | None:
    """Positive rate in the top decile, over the overall base rate.

    This is the number BUILD_BRIEF.md section 3 gates Phase 3 on. Below 1.0 the
    screener is worse than picking at random from the same pool.
    """
    if not labels:
        return None
    overall = base_rate(labels)
    if overall == 0:
        return None
    ranked = sorted(zip(scores, labels, strict=True), key=lambda p: -p[0])
    size = max(1, len(ranked) // 10)
    decile = [label for _, label in ranked[:size]]
    return (sum(decile) / len(decile)) / overall


def top_decile_lift_interval(
    labels: Sequence[int], scores: Sequence[float], z: float = 1.96
) -> tuple[float, float] | None:
    """A Wilson interval on the top decile's positive rate, over the base rate.

    The decile is a handful of rows at this sample size -- eight of eighty -- so a
    lift of 2.4 can be one token. The base rate is held fixed, which understates
    the width a little; it is the decile that dominates it.
    """
    if not labels:
        return None
    overall = base_rate(labels)
    if overall == 0:
        return None
    ranked = sorted(zip(scores, labels, strict=True), key=lambda p: -p[0])
    decile = [label for _, label in ranked[: max(1, len(ranked) // 10)]]
    low, high = wilson_interval(sum(decile), len(decile), z)
    return (low / overall, high / overall)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. Honest at small n and near 0, unlike the normal one.

    stats.md requires an interval on every reported figure, and this dataset will
    spend a long time at "six positive events", where a normal approximation gives
    a lower bound below zero.
    """
    if total == 0:
        return (0.0, 1.0)
    phat = successes / total
    denominator = 1 + z**2 / total
    centre = (phat + z**2 / (2 * total)) / denominator
    margin = (
        z * math.sqrt(phat * (1 - phat) / total + z**2 / (4 * total**2)) / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


# --- splitting --------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    train: list[Row]
    test: list[Row]
    moved_for_deployer_leakage: int = 0


def time_split(rows: Sequence[Row], *, test_fraction: float = 0.3) -> Split:
    """Split forward in time, then repair deployer leakage across the cut.

    No shuffle, no seed, no random state: the ordering is the data's own.
    """
    ordered = sorted(rows, key=lambda r: r.ts)
    cut = int(len(ordered) * (1 - test_fraction))
    train, test = ordered[:cut], ordered[cut:]

    train_deployers = {r.deployer for r in train if r.deployer}
    straddling = [r for r in test if r.deployer and r.deployer in train_deployers]
    if straddling:
        # A deployer seen in training must not reappear in test: their behaviour is
        # a feature, so this is leakage even though the split is chronological.
        moved = set(id(r) for r in straddling)
        test = [r for r in test if id(r) not in moved]
        train = train + straddling
    return Split(train=train, test=test, moved_for_deployer_leakage=len(straddling))


# --- model ------------------------------------------------------------------


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-min(x, 60)))
    exp_x = math.exp(max(x, -60))
    return exp_x / (1 + exp_x)


@dataclass
class LogisticModel:
    """Logistic regression over the pillar features.

    Missing features are handled by mean-imputation *with an explicit missingness
    indicator per feature*, not by dropping the row. stats.md forbids dropping
    rows with missing features, and in this dataset missingness is itself a signal:
    a token with no social series behaved differently from one with a measured
    zero. The indicator lets the model learn that instead of having it smeared
    into the mean.
    """

    weights: dict[str, float] = field(default_factory=dict)
    bias: float = 0.0
    feature_means: dict[str, float] = field(default_factory=dict)
    features: tuple[str, ...] = FEATURE_NAMES

    def _vector(self, row: Row) -> dict[str, float]:
        vector: dict[str, float] = {}
        for name in self.features:
            value = row.features.get(name)
            vector[name] = (
                value / 100.0 if value is not None else self.feature_means.get(name, 0.0)
            )
            vector[f"{name}__missing"] = 1.0 if value is None else 0.0
        return vector

    def predict(self, row: Row) -> float:
        vector = self._vector(row)
        z = self.bias + sum(self.weights.get(k, 0.0) * v for k, v in vector.items())
        return _sigmoid(z)

    def fit(
        self,
        rows: Sequence[Row],
        *,
        epochs: int = 400,
        learning_rate: float = 0.3,
        l2: float = 0.01,
    ) -> LogisticModel:
        present: dict[str, list[float]] = {name: [] for name in self.features}
        for row in rows:
            for name in self.features:
                value = row.features.get(name)
                if value is not None:
                    present[name].append(value / 100.0)
        self.feature_means = {
            name: (sum(values) / len(values) if values else 0.0)
            for name, values in present.items()
        }

        keys = [k for name in self.features for k in (name, f"{name}__missing")]
        self.weights = dict.fromkeys(keys, 0.0)
        self.bias = 0.0
        if not rows:
            return self

        vectors = [self._vector(r) for r in rows]
        labels = [r.label for r in rows]
        n = len(rows)

        for _ in range(epochs):
            gradients = dict.fromkeys(keys, 0.0)
            bias_gradient = 0.0
            for vector, label in zip(vectors, labels, strict=True):
                z = self.bias + sum(self.weights[k] * vector[k] for k in keys)
                error = _sigmoid(z) - label
                bias_gradient += error
                for k in keys:
                    gradients[k] += error * vector[k]
            self.bias -= learning_rate * bias_gradient / n
            for k in keys:
                self.weights[k] -= learning_rate * (gradients[k] / n + l2 * self.weights[k])
        return self

    def normalised_pillar_weights(self) -> dict[str, float]:
        """Fitted coefficients renormalised to sum to 1, for prompts/score.md step 5.

        Only the pillar coefficients, not the missingness indicators: the prompt's
        weight vector is over pillars. Negative coefficients are clamped at zero --
        a pillar the fit says is actively harmful should be investigated, not
        silently given a negative weight in a prompt that presents weights as
        importances.
        """
        raw = {name: max(0.0, self.weights.get(name, 0.0)) for name in self.features}
        total = sum(raw.values())
        if total == 0:
            return dict.fromkeys(self.features, 1 / len(self.features))
        return {name: value / total for name, value in raw.items()}

    def deployable_weights(self, places: int = 2) -> dict[str, float]:
        """:meth:`normalised_pillar_weights` as it would be written into
        ``scoring/pillars.py::WEIGHTS``: rounded the way prompts/score.md prints a
        weight, with the rounding residue put on the largest weight so the vector
        still sums to one. This, not the unrounded vector, is what gets validated,
        because this is what the screener would compute.
        """
        exact = self.normalised_pillar_weights()
        rounded = {name: round(value, places) for name, value in exact.items()}
        largest = max(rounded, key=lambda name: rounded[name])
        rounded[largest] = round(rounded[largest] + 1.0 - sum(rounded.values()), places)
        return rounded


@dataclass
class FitResult:
    label: str
    model: LogisticModel
    train_size: int
    test_size: int
    base_rate_train: float
    base_rate_test: float
    auc: float | None
    concordance: float | None
    top_decile_lift: float | None
    positives_test: int
    interval_test: tuple[float, float]
    by_regime: dict[str, Any] = field(default_factory=dict)
    forced: bool = False
    moved_for_deployer_leakage: int = 0
    threshold: float | None = None
    auc_ci: tuple[float, float] | None = None
    # The vector in force (scoring/pillars.py::WEIGHTS) when the fit ran, scored on
    # the same held-out rows. Called "prior" because it is what the fit would
    # replace. The question a calibration answers is not "is the fit better than a
    # coin" but "is it better than what the screener already does".
    prior_auc: float | None = None
    prior_auc_ci: tuple[float, float] | None = None
    prior_top_decile_lift: float | None = None
    # The vector that would actually replace it, scored the way the screener
    # scores: deployable_weights() inside composite(). It is not the logistic
    # model above -- a negative coefficient is clamped to zero, the missingness
    # terms are dropped and the rest is rounded, because prompts/score.md holds
    # non-negative weights that sum to one -- so it is validated separately, on
    # the same rows.
    deployed_auc: float | None = None
    deployed_auc_ci: tuple[float, float] | None = None
    deployed_top_decile_lift: float | None = None
    deployed_top_decile_lift_ci: tuple[float, float] | None = None
    prior_top_decile_lift_ci: tuple[float, float] | None = None
    deployed_weights: dict[str, float] = field(default_factory=dict)
    # (first, last) trigger time on each side of the split, epoch millis. The
    # test window is where every out-of-sample figure above was measured; a
    # later check that wants rows the fit never saw starts after train_window.
    train_window: tuple[int, int] | None = None
    test_window: tuple[int, int] | None = None

    def checks(self, criteria: ExitCriteria | None) -> dict[str, bool]:
        """Every condition .claude/rules/stats.md sets before a fitted vector may
        replace the priors, each by name, so a refusal says which one failed.

        Judged on the deployed vector, not the logistic model: the priors are
        replaced by what the screener will compute, so that is what must clear.
        """
        low = self.deployed_auc_ci[0] if self.deployed_auc_ci else None
        lift = self.deployed_top_decile_lift
        return {
            "phase_0_exit_criteria_met": criteria is not None and criteria.met,
            "positive_events_in_test": self.positives_test > 0,
            "out_of_sample_auc_above_chance": low is not None and low > 0.5,
            "beats_the_priors_out_of_sample": (
                self.deployed_auc is not None
                and (self.prior_auc is None or self.deployed_auc > self.prior_auc)
            ),
            "top_decile_beats_base_rate": lift is not None and lift > 1.0,
        }

    def may_replace_priors(self, criteria: ExitCriteria | None) -> bool:
        return all(self.checks(criteria).values())

    @property
    def beats_benchmark(self) -> bool:
        return self.concordance is not None and self.concordance >= PUBLISHED_CONCORDANCE_BENCHMARK

    @property
    def has_measured_edge(self) -> bool:
        """The Phase 3 gate: top decile beats the base rate out of sample."""
        return self.top_decile_lift is not None and self.top_decile_lift > 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "threshold": self.threshold,
            "train_size": self.train_size,
            "test_size": self.test_size,
            "positives_test": self.positives_test,
            "base_rate_train": self.base_rate_train,
            "base_rate_test": self.base_rate_test,
            "out_of_sample_auc": self.auc,
            "out_of_sample_auc_ci95": list(self.auc_ci) if self.auc_ci else None,
            "prior_out_of_sample_auc": self.prior_auc,
            "prior_out_of_sample_auc_ci95": (
                list(self.prior_auc_ci) if self.prior_auc_ci else None
            ),
            "prior_top_decile_lift": self.prior_top_decile_lift,
            "deployed_out_of_sample_auc": self.deployed_auc,
            "deployed_out_of_sample_auc_ci95": (
                list(self.deployed_auc_ci) if self.deployed_auc_ci else None
            ),
            "deployed_top_decile_lift": self.deployed_top_decile_lift,
            "deployed_top_decile_lift_ci95": (
                list(self.deployed_top_decile_lift_ci)
                if self.deployed_top_decile_lift_ci
                else None
            ),
            "prior_top_decile_lift_ci95": (
                list(self.prior_top_decile_lift_ci) if self.prior_top_decile_lift_ci else None
            ),
            "concordance": self.concordance,
            "concordance_benchmark": PUBLISHED_CONCORDANCE_BENCHMARK,
            "beats_benchmark": self.beats_benchmark,
            "top_decile_lift_over_base_rate": self.top_decile_lift,
            "has_measured_edge": self.has_measured_edge,
            "base_rate_test_ci95": list(self.interval_test),
            "fitted_weights": self.model.normalised_pillar_weights(),
            "deployed_weights": self.deployed_weights,
            "train_window_ms": list(self.train_window) if self.train_window else None,
            "test_window_ms": list(self.test_window) if self.test_window else None,
            "by_regime": self.by_regime,
            "forced_past_exit_criteria": self.forced,
            "moved_for_deployer_leakage": self.moved_for_deployer_leakage,
        }


def evaluate(model: LogisticModel, rows: Sequence[Row]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "auc": None, "base_rate": None, "top_decile_lift": None}
    labels = [r.label for r in rows]
    scores = [model.predict(r) for r in rows]
    return {
        "n": len(rows),
        "positives": sum(labels),
        "base_rate": base_rate(labels),
        "auc": auc(labels, scores),
        "top_decile_lift": top_decile_lift(labels, scores),
    }


def fit(
    rows: Sequence[Row],
    *,
    label: str = "survived_7d",
    exit_criteria: ExitCriteria | None = None,
    force: bool = False,
    test_fraction: float = 0.3,
    threshold: float | None = None,
) -> FitResult:
    """Fit and evaluate out of sample. Refuses below Phase 0's exit criteria."""
    if exit_criteria is not None and not exit_criteria.met and not force:
        raise RuntimeError(
            "Phase 2 is gated on Phase 0's exit criteria: "
            f"{exit_criteria.explain()}. BUILD_BRIEF.md section 3 says do not "
            "proceed early. Pass force=True to fit anyway; it will be recorded on "
            "the result and in every report."
        )

    split = time_split(rows, test_fraction=test_fraction)
    model = LogisticModel().fit(split.train)

    test_labels = [r.label for r in split.test]
    test_scores = [model.predict(r) for r in split.test]
    positives = sum(test_labels)
    prior_scores = [prior_score(r) for r in split.test]
    deployed = model.deployable_weights()
    deployed_scores = [prior_score(r, deployed) for r in split.test]

    def window(rows: Sequence[Row]) -> tuple[int, int] | None:
        return (min(r.ts for r in rows), max(r.ts for r in rows)) if rows else None

    by_regime: dict[str, Any] = {}
    for regime in ("hot", "neutral", "cold"):
        subset = [r for r in split.test if r.regime == regime]
        if subset:
            by_regime[regime] = evaluate(model, subset)

    return FitResult(
        label=label,
        model=model,
        train_size=len(split.train),
        test_size=len(split.test),
        base_rate_train=base_rate([r.label for r in split.train]),
        base_rate_test=base_rate(test_labels),
        auc=auc(test_labels, test_scores) if split.test else None,
        concordance=concordance(test_labels, test_scores) if split.test else None,
        top_decile_lift=top_decile_lift(test_labels, test_scores) if split.test else None,
        positives_test=positives,
        interval_test=wilson_interval(positives, len(test_labels)),
        by_regime=by_regime,
        forced=force and (exit_criteria is not None and not exit_criteria.met),
        moved_for_deployer_leakage=split.moved_for_deployer_leakage,
        threshold=threshold,
        auc_ci=auc_interval(test_labels, test_scores) if split.test else None,
        prior_auc=auc(test_labels, prior_scores) if split.test else None,
        prior_auc_ci=auc_interval(test_labels, prior_scores) if split.test else None,
        prior_top_decile_lift=(
            top_decile_lift(test_labels, prior_scores) if split.test else None
        ),
        deployed_auc=auc(test_labels, deployed_scores) if split.test else None,
        deployed_auc_ci=auc_interval(test_labels, deployed_scores) if split.test else None,
        deployed_top_decile_lift=(
            top_decile_lift(test_labels, deployed_scores) if split.test else None
        ),
        deployed_top_decile_lift_ci=(
            top_decile_lift_interval(test_labels, deployed_scores) if split.test else None
        ),
        prior_top_decile_lift_ci=(
            top_decile_lift_interval(test_labels, prior_scores) if split.test else None
        ),
        deployed_weights=deployed,
        train_window=window(split.train),
        test_window=window(split.test),
    )


def prior_score(row: Row, weights: dict[str, float] | None = None) -> float:
    """The composite the screener would rank this row by under ``weights``.

    The vector in force by default. Pillar weights only: the completeness
    multiplier and the regime and contradiction modifiers are applied to whichever
    vector is in force. They can reorder rows, so the record of an adopted fit
    also reports the final score -- see docs/calibration-2026-09-24.md. A row
    with no composite ranks last, as it does on the board.
    """
    vector = weights or WEIGHTS
    score, _ = composite({name: row.features.get(name) for name in vector}, vector)
    return score if score is not None else float("-inf")


def outcome(labels: dict[str, Any] | None, label: str, threshold: float | None) -> int | None:
    """A label row's answer as 0/1, or ``None`` while it is unresolved.

    A survival label is already yes/no. A multiple needs a threshold, and refusing
    one without it is deliberate: a default here would be a modelling decision
    made silently.
    """
    if not labels or labels.get(label) is None:
        return None
    value = labels[label]
    if isinstance(value, bool):
        return int(value)
    if threshold is None:
        raise ValueError(f"{label} is a multiple; pass the threshold that counts as a yes")
    return int(value >= threshold)


def rows_from_store(
    store: Any, *, label: str = "survived_7d", threshold: float | None = None
) -> list[Row]:
    """One training row per token: its pillars at the trigger, and what it did next.

    This used to be one row per *score* row. The collector re-scores its recent
    snapshots every cycle, so each token contributed about a hundred near-copies of
    itself: a fit weighted by how long a token stayed in the re-score window, a
    time split that put a token's early copies in train and its later copies in
    test, and features from re-scores that had folded in social counts taken after
    the trigger. Each of those breaks a rule in .claude/rules/stats.md.

    Now: each snapshot's first score row, computed a minute or so after the
    trigger from what was known then, is the one row. Its pillars are recomputed
    from the stored input packet with the current pillar code -- hard rule 6 keeps
    that packet with every score for exactly this -- so every row in a fit shares
    one definition of every pillar, whatever version first scored it. Only resolved
    outcomes are included: an unresolved token is not a negative.
    """
    rows: list[Row] = []
    for score_row in store.trigger_time_scores():
        result = outcome(store.latest_labels(score_row["snapshot_id"]), label, threshold)
        if result is None:
            continue
        candidate = json.loads(score_row["input_snapshot"] or "{}")
        pillars = score_candidate(candidate, regime=score_row.get("batch_regime")).pillar_scores()
        rows.append(
            Row(
                features={name: pillars.get(name) for name in FEATURE_NAMES},
                label=result,
                ts=int(score_row["snapshot_ts"]),
                deployer=(candidate.get("deployer") or {}).get("address"),
                meta_tag=(candidate.get("lineage") or {}).get("meta_tag"),
                regime=score_row.get("batch_regime"),
                snapshot_id=score_row["snapshot_id"],
            )
        )
    return rows
