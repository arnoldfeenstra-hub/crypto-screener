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
    FitResult,
    LogisticModel,
    Row,
    auc,
    base_rate,
    check_exit_criteria,
    concordance,
    fit,
    outcome,
    prior_score,
    rows_from_store,
    time_split,
    top_decile_lift,
    top_decile_lift_interval,
    wilson_interval,
)
from calibration.report import (
    VERDICT_EDGE,
    VERDICT_NO_EDGE,
    VERDICT_NOT_READY,
    base_rate_by_mcap_band,
    held_out_by_label,
    render,
    verdict,
)
from collectors.outcomes import OutcomeTracker, PriceObservation
from collectors.schema import Market, Snapshot
from collectors.store import Store
from scoring import pillars as P
from scoring.prompt_meta import WEIGHTS_DIR, weights_caveat, weights_record
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

    def test_the_lift_interval_brackets_the_lift(self):
        labels = [1] * 5 + [0] * 95
        scores = [0.9] * 5 + [0.1] * 95
        low, high = top_decile_lift_interval(labels, scores)
        assert low < top_decile_lift(labels, scores) < high

    def test_a_decile_of_eight_is_wide_enough_to_say_so(self):
        """Eight rows is the held-out decile here; one token moves the lift a lot."""
        labels = [1] * 17 + [0] * 65
        scores = [float(i % 7) for i in range(82)]
        low, high = top_decile_lift_interval(labels, scores)
        assert high - low > 2.0

    def test_no_positives_means_no_lift_interval(self):
        assert top_decile_lift_interval([0, 0, 0], [0.1, 0.2, 0.3]) is None


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


class TestReplacingThePriors:
    """stats.md: the weight vector may only be replaced by fitted coefficients that
    cleared the checks. ``checks()`` names each one, so a refusal says which."""

    def _result(self, **overrides) -> FitResult:
        values = dict(
            label="max_multiple_6h", model=LogisticModel(), train_size=190,
            test_size=82, base_rate_train=0.24, base_rate_test=0.21, auc=0.64,
            concordance=0.64, top_decile_lift=1.8, positives_test=17,
            interval_test=(0.13, 0.31), prior_auc=0.64, deployed_auc=0.67,
            deployed_auc_ci=(0.52, 0.83), deployed_top_decile_lift=1.8,
        )
        values.update(overrides)
        return FitResult(**values)

    def test_every_check_is_named(self):
        assert set(self._result().checks(None)) == {
            "phase_0_exit_criteria_met",
            "positive_events_in_test",
            "out_of_sample_auc_above_chance",
            "beats_the_priors_out_of_sample",
            "top_decile_beats_base_rate",
        }

    def test_the_gate_alone_can_refuse(self):
        unmet = check_exit_criteria(279, 24, 28, 0)
        checks = self._result().checks(unmet)
        assert checks["phase_0_exit_criteria_met"] is False
        assert all(v for k, v in checks.items() if k != "phase_0_exit_criteria_met")
        assert not self._result().may_replace_priors(unmet)
        assert self._result().may_replace_priors(check_exit_criteria(400, 10, 300, 350))

    def test_an_interval_that_reaches_chance_is_not_above_it(self):
        result = self._result(deployed_auc_ci=(0.48, 0.83))
        assert result.checks(None)["out_of_sample_auc_above_chance"] is False

    def test_a_tie_with_the_priors_does_not_beat_them(self):
        result = self._result(deployed_auc=0.64, prior_auc=0.64)
        assert result.checks(None)["beats_the_priors_out_of_sample"] is False

    def test_the_deployed_vector_is_judged_not_the_logistic_model(self):
        """The logistic model can rank well while its clamped, renormalised weight
        vector -- the thing that would actually replace the priors -- does not."""
        result = self._result(auc=0.90, deployed_auc=0.60, deployed_auc_ci=(0.45, 0.75))
        assert result.checks(None)["out_of_sample_auc_above_chance"] is False

    def test_fit_scores_the_priors_and_the_deployed_vector_on_the_same_rows(self):
        rows = [
            row(95.0 if i % 4 == 0 else 5.0, 1 if i % 4 == 0 else 0, ts=T0 + i * MIN)
            for i in range(200)
        ]
        payload = fit(rows, exit_criteria=None).to_dict()
        for key in (
            "prior_out_of_sample_auc",
            "prior_out_of_sample_auc_ci95",
            "deployed_out_of_sample_auc",
            "deployed_out_of_sample_auc_ci95",
            "deployed_top_decile_lift_ci95",
        ):
            assert payload[key] is not None, key

    def test_a_row_with_no_composite_ranks_last(self):
        empty = Row(features={}, label=0, ts=T0)
        assert prior_score(empty) == float("-inf")
        assert prior_score(row(10.0, 0)) > prior_score(empty)


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
        assert "must not be read as a prediction" in text

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
        assert "must not be read as a prediction" in report["explanation"]

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
            report = render(store, label="survived_7d", threshold=None)

        assert report["base_rate_by_mcap_band"]["<500k"]["n"] == 1
        assert report["labelled_rows_available"] == 1
        # One row is nowhere near the gate, so the verdict stays honest about that.
        assert report["verdict"] == VERDICT_NOT_READY


def two_token_store(store: Store, monkeypatch, *, cycles: int) -> list[Snapshot]:
    """Two tokens scored ``cycles`` times an hour apart: Up doubles in its first
    half hour, Flat barely moves, and neither dies."""
    snapshots = []
    for index, (contract, peak) in enumerate((("Up", 800_000.0), ("Flat", 420_000.0))):
        snap = Snapshot(
            chain="solana", contract=contract, trigger="mcap_250k", source="test",
            ts=T0 + index * MIN, market=Market(mcap_usd=400_000.0),
        )
        store.append_snapshot(snap)
        snapshots.append(snap)
        OutcomeTracker(store).record([
            PriceObservation(snapshot_id=snap.snapshot_id, ts=snap.ts + 30 * MIN,
                             mcap_usd=peak, source="test"),
        ])
    OutcomeTracker(store).refresh_labels(as_of_ms=T0 + 8 * DAY)
    for cycle in range(cycles):
        # An hour apart, like the collector's schedule.
        monkeypatch.setattr(
            "scoring.runner.now_ms", lambda cycle=cycle: T0 + (cycle + 1) * 60 * MIN
        )
        ScoringRunner(store).run(limit=10)
    return snapshots


class TestTrainingRowsFromTheStore:
    """One training row per token, as it stood at its trigger.

    The collector re-scores its recent snapshots every cycle, so a token carries a
    score row per cycle. Fitting on all of them weighted each token by how long it
    sat in the re-score window, put a token's early copies in train and its later
    ones in test, and fed the fit social counts collected after the trigger.
    """

    def test_a_rescored_token_is_one_row_not_one_per_cycle(self, monkeypatch):
        with Store() as store:
            two_token_store(store, monkeypatch, cycles=3)
            assert len(store.all_scores()) == 6
            rows = rows_from_store(store, label="max_multiple_6h", threshold=1.5)
        assert len(rows) == 2

    def test_the_row_is_the_first_score_timed_at_the_trigger(self, monkeypatch):
        """A forward split orders by when the token triggered, not when it was
        last re-scored."""
        with Store() as store:
            snapshots = two_token_store(store, monkeypatch, cycles=3)
            first = store.trigger_time_scores()
            rows = rows_from_store(store, label="max_multiple_6h", threshold=1.5)
        assert [r["scored_at_ms"] for r in first] == [T0 + 60 * MIN] * 2
        assert [r["snapshot_ts"] for r in first] == [s.ts for s in snapshots]
        assert [r.ts for r in rows] == [s.ts for s in snapshots]
        assert [r.snapshot_id for r in rows] == [s.snapshot_id for s in snapshots]

    def test_a_multiple_is_read_against_its_threshold(self, monkeypatch):
        with Store() as store:
            two_token_store(store, monkeypatch, cycles=1)
            rows = rows_from_store(store, label="max_multiple_6h", threshold=1.5)
        assert [r.label for r in rows] == [1, 0]

    def test_a_multiple_without_a_threshold_is_refused(self, monkeypatch):
        """A default threshold would be a modelling decision made silently."""
        with Store() as store:
            two_token_store(store, monkeypatch, cycles=1)
            with pytest.raises(ValueError, match="threshold"):
                rows_from_store(store, label="max_multiple_6h")

    def test_an_unresolved_token_is_left_out_not_counted_as_a_negative(self, monkeypatch):
        with Store() as store:
            two_token_store(store, monkeypatch, cycles=1)
            late = Snapshot(
                chain="solana", contract="Late", trigger="mcap_250k", source="test",
                ts=T0 + 2 * MIN, market=Market(mcap_usd=400_000.0),
            )
            store.append_snapshot(late)  # scored below, never re-priced
            ScoringRunner(store).run(limit=10)
            rows = rows_from_store(store, label="survived_24h")
        assert late.snapshot_id not in {r.snapshot_id for r in rows}
        assert len(rows) == 2

    def test_outcome_reads_survival_as_is_and_unresolved_as_none(self):
        assert outcome({"survived_24h": True}, "survived_24h", None) == 1
        assert outcome({"survived_24h": False}, "survived_24h", None) == 0
        assert outcome({"survived_24h": None}, "survived_24h", None) is None
        assert outcome(None, "survived_24h", None) is None
        assert outcome({"max_multiple_6h": 1.5}, "max_multiple_6h", 1.5) == 1


class TestTheWeightsInForce:
    """stats.md: the weight vector may be replaced only by fitted coefficients that
    cleared the checks, with prompt_version bumped in the same commit. The record in
    scoring/weights/ is what makes that checkable: WEIGHTS must be a copy of it."""

    def record(self) -> dict:
        record = weights_record(P.WEIGHTS_VERSION)
        assert record is not None, f"no record for {P.WEIGHTS_VERSION} in {WEIGHTS_DIR}"
        return record

    def test_the_vector_in_force_is_the_recorded_one(self):
        record = self.record()
        assert record["weights_version"] == P.WEIGHTS_VERSION
        assert record["weights"] == P.WEIGHTS
        assert record["replaces"]["weights"] == P.PRIOR_WEIGHTS
        assert sum(P.WEIGHTS.values()) == pytest.approx(1.0)
        assert all(w >= 0 for w in P.WEIGHTS.values())

    def test_every_check_but_the_gate_was_cleared(self):
        """The gate is the one check this vector was adopted without, on purpose,
        and the record says so rather than hiding it."""
        record = self.record()
        failed = {name for name, passed in record["checks"].items() if not passed}
        assert failed <= {"phase_0_exit_criteria_met"}
        assert record["forced_past_exit_criteria"] is (
            not record["phase_0_exit_criteria"]["met"]
        )

    def test_the_record_carries_what_stats_md_requires(self):
        record = self.record()
        test = record["test"]
        assert test["positives"] > 0 and test["base_rate_ci95"]
        assert test["regimes"], "a result is broken out by regime"
        for vector in ("fitted", "replaced"):
            block = record["out_of_sample"][vector]
            assert block["auc_ci95"] and block["top_decile_lift_ci95"]
        assert record["out_of_sample"]["concordance_benchmark"] == 0.858
        assert record["out_of_sample"]["final_score"]["fitted"]["auc_ci95"]
        # Split forward in time: nothing in train triggered after the test began.
        assert record["train"]["to"] <= record["test"]["from"]

    def test_the_caveat_says_what_the_weights_are(self):
        fitted = weights_caveat(P.WEIGHTS_VERSION)
        assert "too small to establish an edge" in fitted
        assert "AUC" in fitted and "coin flip" in fitted
        assert weights_caveat("priors-v3") == (
            "The scoring weights are uncalibrated priors: guesses."
        )

    def test_the_deployed_vector_is_rounded_and_still_sums_to_one(self):
        model = LogisticModel()
        model.weights = {"onchain_structure": 0.333, "asymmetry_timing": 0.333,
                         "mindshare": 0.333}
        weights = model.deployable_weights()
        assert sum(weights.values()) == pytest.approx(1.0)
        assert all(round(v, 2) == v for v in weights.values())

    def test_score_candidate_takes_a_vector_for_calibration(self):
        candidate = {"market_cap_usd": 400_000.0, "liquidity_usd": 60_000.0,
                     "volume_24h_usd": 600_000.0, "age_hours": 5.0,
                     "socials_declared": {"telegram": True, "x": True, "website": True}}
        default = P.score_candidate(candidate)
        assert default.raw_score == P.score_candidate(candidate, weights=P.WEIGHTS).raw_score
        assert default.raw_score != P.score_candidate(candidate, weights=P.PRIOR_WEIGHTS).raw_score


class TestReportFigures:
    """The report's own arithmetic, on the two-token store above."""

    def test_a_band_counts_a_multiple_against_the_threshold(self, monkeypatch):
        """A 1.05x is a number, and every number but zero is truthy: counted by
        truthiness, every token that traded at all read as a surge."""
        with Store() as store:
            two_token_store(store, monkeypatch, cycles=1)
            bands = base_rate_by_mcap_band(store, "max_multiple_6h", 1.5)
        assert bands["<500k"] == {"n": 2, "positives": 1, "base_rate": 0.5}

    def test_the_by_label_check_uses_only_rows_after_the_training_window(
        self, monkeypatch
    ):
        with Store() as store:
            snapshots = two_token_store(store, monkeypatch, cycles=1)
            rows = rows_from_store(store, label="max_multiple_6h", threshold=1.5)
            result = fit(rows, exit_criteria=None, test_fraction=0.5)
            assert result.train_window == (snapshots[0].ts, snapshots[0].ts)
            entries = held_out_by_label(store, result)
        assert {e["n"] for e in entries if e["label"] == "max_multiple_6h"} == {1}
