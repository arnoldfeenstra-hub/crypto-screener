"""The exploratory backtest, and the two checks that stop it flattering a feature.

The module exists because a real run produced a feature with AUC 0.70 across three
horizons that was worth nothing. These tests reconstruct that failure synthetically
and assert the report now names it, plus the ordinary properties -- no imputation,
no fitted weight, an honest verdict on noise.
"""

from __future__ import annotations

import math

import pytest

from calibration.backtest import (
    CONTROL_FEATURE,
    DEFAULT_LABEL,
    DEFAULT_THRESHOLD,
    BacktestRow,
    auc_interval,
    build_rows,
    evaluate_feature,
    extract_features,
    run,
    tie_mass,
)


def make_rows(specs):
    """``specs`` is a list of ``(ts, age_minutes, features, outcome)``."""
    return [
        BacktestRow(
            snapshot_id=f"s{index}",
            ts=ts,
            chain="solana",
            ticker=f"$T{index}",
            features=features,
            outcome=float(outcome),
            age_minutes=age,
        )
        for index, (ts, age, features, outcome) in enumerate(specs)
    ]


class TestTieMass:
    def test_reports_the_modal_share_and_value(self):
        share, value = tie_mass([4.0, 4.0, 4.0, 1.0])
        assert share == pytest.approx(0.75)
        assert value == 4.0

    def test_empty_is_not_a_division(self):
        assert tie_mass([]) == (0.0, None)

    def test_all_distinct_values_tie_at_the_floor(self):
        share, _ = tie_mass([1.0, 2.0, 3.0, 4.0])
        assert share == pytest.approx(0.25)


class TestAucInterval:
    def test_brackets_the_point_estimate(self):
        labels = [0, 0, 1, 1, 0, 1]
        scores = [1.0, 2.0, 3.0, 4.0, 1.5, 5.0]
        interval = auc_interval(labels, scores)
        assert interval is not None
        low, high = interval
        assert 0.0 <= low <= high <= 1.0

    def test_widens_as_the_sample_shrinks(self):
        small = auc_interval([0, 1, 0, 1], [1.0, 3.0, 2.0, 1.5])
        large = auc_interval([0, 1, 0, 1] * 30, [1.0, 3.0, 2.0, 1.5] * 30)
        assert small is not None and large is not None
        assert (small[1] - small[0]) > (large[1] - large[0])

    def test_perfect_separation_is_not_reported_as_exact(self):
        # Four rows that separate perfectly. The closed form has zero variance
        # here; an interval of [1.00, 1.00] off two positives would be the most
        # confident and least justified thing the module could print.
        interval = auc_interval([0, 0, 1, 1], [1.0, 2.0, 3.0, 4.0])
        assert interval is not None
        assert interval[0] < 0.9 and interval[1] == 1.0

    def test_perfect_separation_narrows_with_more_pairs(self):
        few = auc_interval([0, 0, 1, 1], [1.0, 2.0, 3.0, 4.0])
        many = auc_interval([0] * 40 + [1] * 40, list(range(80)))
        assert few is not None and many is not None
        assert many[0] > few[0]

    def test_perfect_inversion_is_bounded_from_the_other_side(self):
        interval = auc_interval([1, 1, 0, 0], [1.0, 2.0, 3.0, 4.0])
        assert interval is not None
        assert interval[0] == 0.0 and interval[1] > 0.1

    def test_one_class_has_no_interval(self):
        assert auc_interval([1, 1, 1], [1.0, 2.0, 3.0]) is None


class TestSaturationArtefact:
    """The exact failure the module was written after.

    A ratio between two nested windows pins at the window ratio for any token
    younger than the longer window. Young tokens surge more often, so pooling the
    two produces a strong-looking AUC built entirely out of a variable the feature
    is not measuring.
    """

    def build(self):
        specs = []
        ts = 1_000_000
        # Young tokens: the ratio is pinned at 4.0 for every one of them, and they
        # surge at 50%. Inside the band the feature orders nothing.
        for index in range(20):
            specs.append((ts + index, 30.0, {"ratio": 4.0}, index % 2 == 0))
        # Old tokens: the ratio varies, and they surge at 10%. Inside the band the
        # feature still orders nothing -- the surges are not on the high values.
        for index in range(20):
            surged = index in (3, 11)
            specs.append((ts + 100 + index, 5000.0, {"ratio": 0.5 + index * 0.1}, surged))
        return make_rows(specs)

    def test_pooled_auc_looks_like_a_signal(self):
        result = evaluate_feature("ratio", self.build())
        assert result.area is not None and result.area > 0.65

    def test_tie_mass_exposes_the_pinning(self):
        result = evaluate_feature("ratio", self.build())
        assert result.tie_value == 4.0
        assert result.tie_share == pytest.approx(0.5)
        assert result.degenerate

    def test_no_separation_survives_inside_an_age_band(self):
        result = evaluate_feature("ratio", self.build())
        assert not result.survives_strata

    def test_the_verdict_says_artefact_and_it_is_not_a_lead(self):
        report = run(self.build())
        assert "artefact" in dict(
            (r.name, r.verdict()) for r in report.results
        )["ratio"]
        assert "ratio" not in [r.name for r in report.leads()]


class TestGenuineFeature:
    def build(self):
        specs = []
        ts = 1_000_000
        # The same ordering holds inside both age bands, which is what makes it a
        # feature rather than a proxy for the band.
        for band_index, age in enumerate((30.0, 5000.0)):
            for index in range(20):
                value = float(index)
                surged = index >= 14
                specs.append((ts + band_index * 100 + index, age, {"real": value}, surged))
        return make_rows(specs)

    def test_it_separates_and_survives_stratification(self):
        result = evaluate_feature("real", self.build())
        assert result.separates
        assert result.survives_strata
        assert not result.degenerate

    def test_it_is_reported_as_a_lead_and_never_as_a_result(self):
        report = run(self.build())
        assert [r.name for r in report.leads()] == ["real"]
        assert "lead" in report.verdict()
        assert "not as a measured edge" in report.verdict()


class TestOutOfSample:
    """"Separated out of sample" is a claim, so it is checked rather than assumed."""

    @staticmethod
    def build():
        # Time-ordered. On the earlier 70% the feature is high on the surges; on
        # the later 30% it is low on them. Pooled, it still separates -- and both
        # age bands agree with the pool, because both hold earlier rows.
        specs = []
        for index in range(100):
            surged = index % 5 == 0
            sign = 1.0 if index < 70 else -1.0
            value = 10.0 + sign * (5.0 if surged else 0.0) + (index % 7) * 0.1
            age = 30.0 if index % 2 == 0 else 5000.0
            specs.append((1_000_000 + index, age, {"f": value}, surged))
        return make_rows(specs)

    def test_a_direction_that_reverses_on_the_later_rows_is_not_a_lead(self):
        result = evaluate_feature("f", self.build())
        assert result.separates and result.survives_strata  # the in-sample picture
        assert result.out_of_sample_area is not None and result.out_of_sample_area < 0.5
        assert not result.holds_out_of_sample
        assert "in sample only" in result.verdict()
        assert run(self.build()).leads() == ()

    def test_the_out_of_sample_figure_carries_its_own_interval(self):
        rows = TestGenuineFeature().build()
        payload = evaluate_feature("real", rows).to_dict()
        low, high = payload["auc_out_of_sample_ci"]
        assert 0.0 <= low <= payload["auc_out_of_sample"] <= high <= 1.0


class TestDirection:
    def test_the_lift_is_taken_at_the_tail_the_feature_predicts_from(self):
        # Lower is better: the surges are the smallest values. The raw top decile
        # is the worst decile, which is how the sample's one lead read 0.00.
        specs = [
            (1_000_000 + index, 30.0 + index, {"conc": float(index)}, index < 10)
            for index in range(100)
        ]
        result = evaluate_feature("conc", make_rows(specs))
        assert result.area is not None and result.area < 0.5
        assert result.decile_lift is not None and result.decile_lift > 1.0

    def test_a_perfectly_backwards_feature_sorts_first(self):
        specs = [
            (1_000_000 + index, 30.0, {"backwards": float(index), "weak": float(index % 3)},
             index < 10)
            for index in range(40)
        ]
        report = run(make_rows(specs))
        assert report.results[0].name == "backwards"
        assert report.results[0].area == 0.0


class TestControlFeature:
    def test_age_is_never_judged_against_its_own_strata(self):
        specs = [
            (1_000_000 + i, float(age), {CONTROL_FEATURE: float(age)}, age < 100)
            for i, age in enumerate([30, 40, 50, 60, 5000, 6000, 7000, 8000] * 4)
        ]
        result = evaluate_feature(CONTROL_FEATURE, make_rows(specs))
        assert result.is_control
        assert result.survives_strata  # exempt, not measured
        assert "control variable" in result.verdict()

    def test_the_control_is_excluded_from_the_leads(self):
        specs = [
            (1_000_000 + i, float(age), {CONTROL_FEATURE: float(age)}, age < 100)
            for i, age in enumerate([30, 40, 50, 60, 5000, 6000, 7000, 8000] * 4)
        ]
        assert run(make_rows(specs)).leads() == ()


class TestNoise:
    def test_pure_noise_produces_no_leads_and_says_so(self):
        # A deterministic pseudo-random feature with no relation to the outcome.
        specs = []
        for index in range(120):
            value = math.sin(index * 12.9898) * 43758.5453
            value -= math.floor(value)
            specs.append((1_000_000 + index, 30.0 + index, {"noise": value}, index % 5 == 0))
        report = run(make_rows(specs))
        assert report.leads() == ()
        assert "nothing has been measured" in report.verdict()


class TestGateAndFraming:
    def test_the_verdict_names_the_phase_0_gate_when_it_is_not_met(self):
        report = run(
            make_rows([(1, 30.0, {"x": 1.0}, True), (2, 30.0, {"x": 0.0}, False)]),
            triggered_tokens=85,
            dead_per_survivor=None,
        )
        assert "Phase 0's gate is NOT met" in report.verdict()
        assert "no weight in prompts/score.md may be changed on it" in report.verdict()

    def test_snapshots_without_a_social_series_do_not_meet_the_gate(self):
        """The same gate calibration.fit.ExitCriteria enforces: 300 triggered tokens
        *with a complete social series*. Counting snapshots alone called it met
        while the calibration panel reading the same dataset said it was not."""
        rows = make_rows([(1, 30.0, {"x": 1.0}, True), (2, 30.0, {"x": 0.0}, False)])
        short = run(rows, triggered_tokens=400, dead_per_survivor=30.0, complete_social=12)
        assert not short.gate_met
        assert "Phase 0's gate is NOT met" in short.verdict()
        met = run(rows, triggered_tokens=400, dead_per_survivor=30.0, complete_social=350)
        assert met.gate_met

    def test_the_payload_declares_it_is_not_a_calibration(self):
        report = run(make_rows([(1, 30.0, {"x": 1.0}, True), (2, 30.0, {"x": 0.0}, False)]))
        assert report.to_dict()["is_calibration"] is False

    def test_an_empty_sample_says_nothing_was_measured(self):
        assert "Nothing has been measured" in run([]).verdict()


class TestNoImputation:
    def test_a_snapshot_with_no_label_is_dropped_not_defaulted(self):
        snapshots = [
            {"snapshot_id": "a", "ts": 1, "chain": "solana", "market_mcap_usd": 300_000.0},
            {"snapshot_id": "b", "ts": 2, "chain": "solana", "market_mcap_usd": 400_000.0},
        ]
        labels = {"a": {DEFAULT_LABEL: 2.0}}
        rows = build_rows(snapshots, labels.get)
        assert [r.snapshot_id for r in rows] == ["a"]

    def test_a_missing_sell_count_is_unknown_pressure_not_perfect_pressure(self):
        features = extract_features({"momentum_buys_1h": 40, "momentum_sells_1h": None})
        assert features["buy_pressure_1h"] is None

    def test_a_missing_denominator_never_becomes_a_ratio(self):
        features = extract_features({"market_volume_24h_usd": 900.0, "market_mcap_usd": 0.0})
        assert features["turnover_24h"] is None

    def test_a_false_boolean_is_zero_and_an_absent_one_is_none(self):
        assert extract_features({"socials_declared_x": False})["declared_x"] == 0.0
        assert extract_features({})["declared_x"] is None

    def test_rows_missing_a_feature_are_dropped_from_that_feature_only(self):
        rows = make_rows(
            [
                (1, 30.0, {"a": 1.0, "b": None}, True),
                (2, 30.0, {"a": 2.0, "b": 5.0}, False),
                (3, 30.0, {"a": 3.0, "b": 6.0}, True),
            ]
        )
        assert evaluate_feature("a", rows).n == 3
        assert evaluate_feature("b", rows).n == 2

    def test_a_boolean_label_is_read_as_the_outcome_itself(self):
        snapshots = [{"snapshot_id": "a", "ts": 1, "chain": "solana"}]
        rows = build_rows(
            snapshots,
            {"a": {"survived_24h": False}}.get,
            label="survived_24h",
            threshold=DEFAULT_THRESHOLD,
        )
        assert rows[0].outcome == 0.0


class TestRender:
    def test_the_header_refuses_the_word_calibration(self):
        from calibration.backtest import render

        text = render(run(make_rows([(1, 30.0, {"x": 1.0}, True), (2, 30.0, {"x": 0.0}, False)])))
        assert "not a calibration" in text
        assert "no weight is changed by it" in text
