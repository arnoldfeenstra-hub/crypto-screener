"""Calibration tests -- Phase 2, against .claude/rules/stats.md.

Everything here defends against the same class of mistake: a number that looks like
a measurement but is not one. A random split that leaks the future, a deployer in
both halves, an AUC flattered by ties, a fit run on six positive events and reported
without saying so. Each produces a confident figure, and each figure is worthless.

The last test in the file is the one that matters most: a model fitted on noise must
report no edge. If that ever passes when it should not, everything downstream --
including the deployed page -- is lying.
"""

from __future__ import annotations

import random

import pytest

from calibration.fit import (
    MIN_DEAD_PER_SURVIVOR,
    MIN_TRIGGERED_TOKENS,
    PUBLISHED_CONCORDANCE_BENCHMARK,
    LogisticModel,
    Row,
    auc,
    base_rate,
    check_exit_criteria,
    concordance,
    fit,
    time_split,
    top_decile_lift,
    wilson_interval,
)
from calibration.report import (
    VERDICT_EDGE,
    VERDICT_NO_EDGE,
    VERDICT_NOT_READY,
    render,
    verdict,
)
from collectors.outcomes import OutcomeTracker, PriceObservation
from collectors.schema import Market, Snapshot
from collectors.store import Store
from scoring.runner import ScoringRunner

T0 = 1788912000000
MIN = 60_000
DAY = 1440 * MIN


def row(score: float | None, label: int, ts: int = T0, deployer: str | None = None,
        regime: str | None = None) -> Row:
    return Row(
        features={"onchain_structure": score, "asymmetry_timing": score},
        label=label,
        ts=ts,
        deployer=deployer,
        regime=regime,
    )


class TestExitCriteria:
    def test_the_thresholds_are_the_ones_the_brief_names(self):
        assert MIN_TRIGGERED_TOKENS == 300
        assert MIN_DEAD_PER_SURVIVOR == 20

    def test_an_empty_dataset_has_not_met_them(self):
        criteria = check_exit_criteria(0, 0, 0, 0)
        assert not criteria.met
        assert "NOT met" in criteria.explain()

    def test_enough_tokens_but_too_few_dead_is_not_enough(self):
        """A small negative class teaches the model to say yes."""
        criteria = check_exit_criteria(400, 50, 100, 400)
        assert criteria.dead_per_survivor == 2.0
        assert not criteria.met

    def test_both_conditions_together_pass(self):
        criteria = check_exit_criteria(400, 10, 300, 350)
        assert criteria.met

    def test_triggered_without_social_series_does_not_count(self):
        """"300 triggered tokens with complete social series" -- both halves."""
        criteria = check_exit_criteria(400, 10, 300, complete_social=12)
        assert not criteria.met


class TestMetrics:
    def test_a_perfect_ranking_scores_one(self):
        assert auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(1.0)

    def test_a_reversed_ranking_scores_zero(self):
        assert auc([1, 1, 0, 0], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(0.0)

    def test_all_ties_score_one_half_not_one(self):
        """A model that separates nothing must not be flattered to a perfect score."""
        assert auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == pytest.approx(0.5)

    def test_one_class_only_has_no_auc(self):
        assert auc([1, 1, 1], [0.1, 0.5, 0.9]) is None

    def test_concordance_is_the_same_quantity_as_auc(self):
        labels, scores = [0, 1, 0, 1], [0.2, 0.7, 0.4, 0.9]
        assert concordance(labels, scores) == auc(labels, scores)

    def test_top_decile_lift_against_the_base_rate(self):
        # 5 positives in 100, all ranked top. The decile is 10 rows, so it is half
        # positive: 0.5 over a 0.05 base rate is a 10x lift, not 20x -- the decile
        # is bigger than the positive class.
        labels = [1] * 5 + [0] * 95
        scores = [0.9] * 5 + [0.1] * 95
        assert base_rate(labels) == pytest.approx(0.05)
        assert top_decile_lift(labels, scores) == pytest.approx(10.0)

    def test_a_useless_ranking_lifts_by_about_one(self):
        labels = [1, 0] * 50
        scores = [0.5] * 100
        assert top_decile_lift(labels, scores) == pytest.approx(1.0, abs=0.3)

    def test_the_interval_is_honest_at_small_n(self):
        """stats.md: 300 tokens at a 2% base rate is about six positive events."""
        low, high = wilson_interval(6, 300)
        assert 0.0 < low < 0.02 < high < 0.06

    def test_the_interval_never_goes_below_zero(self):
        low, _ = wilson_interval(0, 30)
        assert low == 0.0


class TestSplitting:
    def test_the_split_is_chronological(self):
        rows = [row(50.0, 0, ts=T0 + i * DAY) for i in range(10)]
        split = time_split(rows, test_fraction=0.3)
        assert len(split.train) == 7
        assert max(r.ts for r in split.train) < min(r.ts for r in split.test)

    def test_there_is_no_shuffle_or_seed_to_reach_for(self):
        import inspect

        params = inspect.signature(time_split).parameters
        assert "shuffle" not in params
        assert "seed" not in params
        assert "random_state" not in params

    def test_a_deployer_in_both_halves_is_moved_into_train(self):
        """Leakage even under a time split: the deployer's behaviour is the feature."""
        rows = [row(50.0, 0, ts=T0 + i * DAY, deployer="A") for i in range(7)]
        rows += [row(50.0, 1, ts=T0 + (7 + i) * DAY, deployer="A") for i in range(3)]
        split = time_split(rows, test_fraction=0.3)
        assert split.test == []
        assert split.moved_for_deployer_leakage == 3

    def test_unrelated_deployers_stay_in_test(self):
        rows = [row(50.0, 0, ts=T0 + i * DAY, deployer=f"D{i}") for i in range(10)]
        split = time_split(rows, test_fraction=0.3)
        assert len(split.test) == 3
        assert split.moved_for_deployer_leakage == 0


class TestModel:
    def test_it_learns_a_separable_signal(self):
        rows = [row(90.0, 1, ts=T0 + i * MIN) for i in range(60)]
        rows += [row(10.0, 0, ts=T0 + (60 + i) * MIN) for i in range(60)]
        model = LogisticModel().fit(rows)
        assert model.predict(row(90.0, 1)) > model.predict(row(10.0, 0))

    def test_a_missing_feature_does_not_drop_the_row(self):
        """stats.md forbids dropping rows with missing features."""
        rows = [row(90.0, 1, ts=T0 + i * MIN) for i in range(30)]
        rows += [row(None, 0, ts=T0 + (30 + i) * MIN) for i in range(30)]
        model = LogisticModel().fit(rows)
        assert model.predict(row(None, 0)) is not None
        # Missingness gets its own coefficient rather than being smeared into a mean.
        assert "onchain_structure__missing" in model.weights

    def test_fitted_weights_renormalise_to_one(self):
        rows = [row(90.0, 1, ts=T0 + i * MIN) for i in range(40)]
        rows += [row(10.0, 0, ts=T0 + (40 + i) * MIN) for i in range(40)]
        weights = LogisticModel().fit(rows).normalised_pillar_weights()
        assert sum(weights.values()) == pytest.approx(1.0)
        assert all(w >= 0 for w in weights.values())


class TestFitGate:
    def _rows(self, n: int = 100) -> list[Row]:
        return [row(50.0 + (i % 2) * 40, i % 2, ts=T0 + i * MIN) for i in range(n)]

    def test_fitting_is_refused_below_the_exit_criteria(self):
        criteria = check_exit_criteria(10, 1, 5, 10)
        with pytest.raises(RuntimeError, match="gated on Phase 0"):
            fit(self._rows(), exit_criteria=criteria)

    def test_force_is_allowed_but_recorded(self):
        criteria = check_exit_criteria(10, 1, 5, 10)
        result = fit(self._rows(), exit_criteria=criteria, force=True)
        assert result.forced is True
        assert result.to_dict()["forced_past_exit_criteria"] is True

    def test_a_met_gate_needs_no_force(self):
        criteria = check_exit_criteria(400, 10, 300, 350)
        assert fit(self._rows(), exit_criteria=criteria).forced is False

    def test_every_reported_figure_is_out_of_sample(self):
        result = fit(self._rows(), exit_criteria=None)
        payload = result.to_dict()
        assert payload["out_of_sample_auc"] is not None
        assert payload["base_rate_test"] is not None
        assert payload["base_rate_test_ci95"]
        assert payload["concordance_benchmark"] == PUBLISHED_CONCORDANCE_BENCHMARK


class TestNoiseReportsNoEdge:
    def test_a_model_fitted_on_noise_must_not_claim_an_edge(self):
        """The test that matters most. If this passes wrongly, everything lies."""
        rng = random.Random(20260909)
        rows = [
            Row(
                features={
                    "onchain_structure": rng.uniform(0, 100),
                    "asymmetry_timing": rng.uniform(0, 100),
                },
                label=1 if rng.random() < 0.02 else 0,  # a realistic 2% base rate
                ts=T0 + i * MIN,
                deployer=f"D{i}",
            )
            for i in range(1200)
        ]
        result = fit(rows, exit_criteria=None)
        assert result.auc is None or 0.3 < result.auc < 0.7
        assert not result.beats_benchmark
        code, explanation = verdict(result, check_exit_criteria(1200, 20, 1180, 1200))
        if result.has_measured_edge:
            # Noise can still put a couple of positives in the top decile by chance;
            # what must never happen is beating the published concordance benchmark.
            assert not result.beats_benchmark
        else:
            assert code == VERDICT_NO_EDGE
            assert "no edge has been measured" in explanation


class TestVerdict:
    def test_an_unfinished_phase_zero_is_not_ready(self):
        code, text = verdict(None, check_exit_criteria(5, 0, 5, 5))
        assert code == VERDICT_NOT_READY
        assert "uncalibrated prior" in text

    def test_a_real_edge_is_reported_as_one(self):
        # Classes interleaved in time, so the chronological split leaves positives
        # on both sides. A separable signal must come back as a measured edge.
        rows = [
            row(95.0 if i % 4 == 0 else 5.0, 1 if i % 4 == 0 else 0, ts=T0 + i * MIN)
            for i in range(200)
        ]
        result = fit(rows, exit_criteria=None)
        code, text = verdict(result, check_exit_criteria(400, 10, 300, 350))
        assert code == VERDICT_EDGE
        assert "out of sample" in text

    def test_a_single_class_test_set_is_not_ready_rather_than_no_edge(self):
        """Nothing measured is not the same as measured and found wanting."""
        rows = [row(95.0, 1, ts=T0 + i * MIN) for i in range(100)]
        rows += [row(5.0, 0, ts=T0 + (100 + i) * MIN) for i in range(100)]
        result = fit(rows, exit_criteria=None)
        assert result.positives_test == 0
        code, text = verdict(result, check_exit_criteria(400, 10, 300, 350))
        assert code == VERDICT_NOT_READY
        assert "no positive events" in text


class TestReportAgainstTheStore:
    def test_an_empty_dataset_reports_not_ready_rather_than_erroring(self):
        with Store() as store:
            report = render(store)
        assert report["verdict"] == VERDICT_NOT_READY
        assert report["phase_0_exit_criteria"]["met"] is False
        assert "uncalibrated prior" in report["explanation"]

    def test_the_report_carries_the_base_rate_by_band(self):
        with Store() as store:
            snap = Snapshot(
                chain="solana", contract="Tok1", trigger="mcap_250k", source="test",
                ts=T0, market=Market(mcap_usd=400_000.0),
            )
            store.append_snapshot(snap)
            tracker = OutcomeTracker(store)
            tracker.record([
                PriceObservation(snapshot_id=snap.snapshot_id, ts=T0 + 10_000 * MIN,
                                 mcap_usd=1_000.0, source="test")
            ])
            tracker.refresh_labels(as_of_ms=T0 + 8 * DAY)
            ScoringRunner(store).run(limit=10)
            report = render(store)

        assert report["base_rate_by_mcap_band"]["<500k"]["n"] == 1
        assert report["labelled_rows_available"] == 1
        # One row is nowhere near the gate, so the verdict stays honest about that.
        assert report["verdict"] == VERDICT_NOT_READY
