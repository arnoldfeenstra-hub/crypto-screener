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

PHASE = "2"

MIN_TRIGGERED_TOKENS = 300
MIN_DEAD_PER_SURVIVOR = 20
PUBLISHED_CONCORDANCE_BENCHMARK = 0.858

FEATURE_NAMES = (
    "attention_velocity",
    "community_depth",
    "lineage_meta_fit",
    "onchain_structure",
    "asymmetry_timing",
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
            "train_size": self.train_size,
            "test_size": self.test_size,
            "positives_test": self.positives_test,
            "base_rate_train": self.base_rate_train,
            "base_rate_test": self.base_rate_test,
            "out_of_sample_auc": self.auc,
            "concordance": self.concordance,
            "concordance_benchmark": PUBLISHED_CONCORDANCE_BENCHMARK,
            "beats_benchmark": self.beats_benchmark,
            "top_decile_lift_over_base_rate": self.top_decile_lift,
            "has_measured_edge": self.has_measured_edge,
            "base_rate_test_ci95": list(self.interval_test),
            "fitted_weights": self.model.normalised_pillar_weights(),
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
    )


def rows_from_store(store: Any, *, label: str = "survived_7d") -> list[Row]:
    """Build training rows by joining scores to their labels.

    Only snapshots with a resolved outcome are included -- an unresolved token is
    not a negative, and counting it as one would manufacture the graveyard rather
    than observe it.
    """
    rows: list[Row] = []
    for score_row in store.all_scores():
        labels = store.latest_labels(score_row["snapshot_id"])
        if not labels or labels.get(label) is None:
            continue
        pillars = json.loads(score_row["pillar_scores"] or "{}")
        snapshot = json.loads(score_row["input_snapshot"] or "{}")
        rows.append(
            Row(
                features={name: pillars.get(name) for name in FEATURE_NAMES},
                label=1 if labels[label] else 0,
                ts=score_row["scored_at_ms"],
                deployer=(snapshot.get("deployer") or {}).get("address"),
                meta_tag=(snapshot.get("lineage") or {}).get("meta_tag"),
                regime=score_row.get("batch_regime"),
            )
        )
    return rows
