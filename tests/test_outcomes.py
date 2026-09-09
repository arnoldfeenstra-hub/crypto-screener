"""Outcome tracker tests -- BUILD_BRIEF.md section 2.

The label maths is where a backtest gets quietly flattered. Three ways it can
happen, each with tests below:

* A horizon that has not elapsed yet gets a number anyway, computed from a partial
  window, and reads as a modest result rather than an unknown one.
* Drawdown is measured from the peak instead of from the entry, which hides exactly
  the -60%-then-5x path the brief says makes a token untradeable.
* A token nobody managed to re-price is scored as flat or dead rather than unknown.
"""

from __future__ import annotations

import pytest

from collectors.outcomes import (
    HORIZONS_MINUTES,
    SURVIVAL_FLOOR_RATIO,
    Labels,
    OutcomeTracker,
    PriceObservation,
    compute_labels,
    due_for_repricing,
)
from collectors.schema import Market, Snapshot
from collectors.store import AppendOnlyViolation, Store

T0 = 1788912000000  # 2026-09-09T00:00:00Z
MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR


def obs(minutes: int, mcap: float | None, snapshot_id: str = "snap1") -> PriceObservation:
    return PriceObservation(
        snapshot_id=snapshot_id, ts=T0 + minutes * MIN, mcap_usd=mcap, source="test"
    )


def labels_for(path, baseline=100_000.0, as_of_minutes=10_080, **kw) -> Labels:
    return compute_labels(
        "snap1", T0, baseline, path, as_of_ms=T0 + as_of_minutes * MIN, **kw
    )


class TestMultiples:
    def test_the_peak_in_each_window_becomes_the_multiple(self):
        path = [obs(10, 200_000.0), obs(120, 500_000.0), obs(2000, 1_000_000.0)]
        labels = labels_for(path)
        assert labels.max_multiple_1h == pytest.approx(2.0)
        assert labels.max_multiple_6h == pytest.approx(5.0)
        assert labels.max_multiple_24h == pytest.approx(5.0)
        assert labels.max_multiple_72h == pytest.approx(10.0)

    def test_a_later_collapse_does_not_lower_the_peak(self):
        """Max multiple is the best the token reached, not where it ended."""
        labels = labels_for([obs(30, 900_000.0), obs(600, 1_000.0)])
        assert labels.max_multiple_24h == pytest.approx(9.0)

    def test_a_token_that_only_fell_has_a_multiple_below_one(self):
        labels = labels_for([obs(30, 40_000.0), obs(600, 5_000.0)])
        assert labels.max_multiple_24h == pytest.approx(0.4)

    def test_observations_outside_the_window_are_excluded(self):
        labels = labels_for([obs(10, 150_000.0), obs(90, 900_000.0)])
        assert labels.max_multiple_1h == pytest.approx(1.5)  # not the 90-minute spike
        assert labels.max_multiple_6h == pytest.approx(9.0)


class TestWhatIsNotKnowable:
    def test_an_open_window_yields_null_not_a_partial_number(self):
        """The failure that makes a backtest look calm: a 7d label from 2h of data."""
        labels = labels_for([obs(10, 300_000.0)], as_of_minutes=120)
        assert labels.max_multiple_1h == pytest.approx(3.0)
        assert labels.max_multiple_6h is None
        assert labels.max_multiple_24h is None
        assert labels.max_multiple_7d is None

    def test_an_elapsed_window_with_no_observation_is_unknown_not_flat(self):
        labels = labels_for([obs(5000, 300_000.0)])
        assert labels.max_multiple_1h is None  # nothing observed in the first hour
        assert labels.max_multiple_7d == pytest.approx(3.0)

    def test_no_baseline_means_no_multiples(self):
        assert labels_for([obs(10, 300_000.0)], baseline=None).max_multiple_24h is None

    def test_a_zero_baseline_does_not_produce_an_infinite_multiple(self):
        labels = labels_for([obs(10, 300_000.0)], baseline=0.0)
        assert labels.max_multiple_24h is None

    def test_observations_without_an_mcap_are_not_read_as_zero(self):
        """A failed re-price is not a total loss; it is a failed re-price."""
        labels = labels_for([obs(10, None), obs(20, 300_000.0)])
        assert labels.max_multiple_1h == pytest.approx(3.0)
        assert labels.max_drawdown_before_peak_24h == pytest.approx(0.0)

    def test_an_empty_path_produces_an_all_null_row(self):
        labels = labels_for([])
        assert not labels.complete
        assert labels.max_multiple_24h is None
        assert labels.survived_24h is None


class TestDrawdown:
    def test_drawdown_is_measured_from_the_snapshot_not_the_peak(self):
        """The brief's example: -60% first, then a 5x. Both must show."""
        path = [obs(10, 40_000.0), obs(60, 500_000.0), obs(600, 300_000.0)]
        labels = labels_for(path)
        assert labels.max_multiple_24h == pytest.approx(5.0)
        assert labels.max_drawdown_before_peak_24h == pytest.approx(0.6)

    def test_only_the_path_before_the_peak_counts(self):
        """A collapse after the peak is not a drawdown you had to sit through."""
        path = [obs(10, 120_000.0), obs(60, 500_000.0), obs(600, 1_000.0)]
        assert labels_for(path).max_drawdown_before_peak_24h == pytest.approx(0.0)

    def test_a_token_that_only_rose_has_no_drawdown(self):
        path = [obs(10, 150_000.0), obs(60, 400_000.0)]
        assert labels_for(path).max_drawdown_before_peak_24h == pytest.approx(0.0)

    def test_drawdown_is_computed_at_both_required_horizons(self):
        path = [obs(10, 50_000.0), obs(60, 200_000.0), obs(3000, 20_000.0), obs(4000, 900_000.0)]
        labels = labels_for(path)
        assert labels.max_drawdown_before_peak_24h == pytest.approx(0.5)
        # By 72h the peak is at 4000min, so the 20k trough at 3000min is in scope.
        assert labels.max_drawdown_before_peak_72h == pytest.approx(0.8)


class TestSurvival:
    def test_survival_is_the_twenty_percent_floor(self):
        assert SURVIVAL_FLOOR_RATIO == 0.20
        alive = labels_for([obs(1400, 25_000.0)])
        dead = labels_for([obs(1400, 15_000.0)])
        assert alive.survived_24h is True
        assert dead.survived_24h is False

    def test_exactly_at_the_floor_counts_as_survived(self):
        assert labels_for([obs(1400, 20_000.0)]).survived_24h is True

    def test_survival_uses_the_state_at_the_horizon_not_the_peak(self):
        """A token that 10x'd and then died is dead. It is also a valuable row."""
        labels = labels_for([obs(60, 1_000_000.0), obs(1400, 500.0)])
        assert labels.max_multiple_24h == pytest.approx(10.0)
        assert labels.survived_24h is False

    def test_an_unresolved_horizon_gives_no_survival_verdict(self):
        labels = labels_for([obs(10, 300_000.0)], as_of_minutes=120)
        assert labels.survived_24h is None


class TestTimeToPeak:
    def test_minutes_from_snapshot_to_the_best_observation(self):
        labels = labels_for([obs(10, 200_000.0), obs(240, 800_000.0), obs(600, 300_000.0)])
        assert labels.time_to_peak_minutes == 240

    def test_it_never_reaches_into_an_unclosed_window(self):
        labels = labels_for([obs(10, 200_000.0), obs(500, 900_000.0)], as_of_minutes=120)
        assert labels.time_to_peak_minutes == 10


class TestCompleteness:
    def test_complete_means_the_widest_horizon_resolved(self):
        assert labels_for([obs(10_000, 300_000.0)]).complete
        assert not labels_for([obs(10, 300_000.0)], as_of_minutes=120).complete

    def test_all_five_horizons_exist(self):
        assert set(HORIZONS_MINUTES) == {"1h", "6h", "24h", "72h", "7d"}


class TestRepricingSchedule:
    def test_a_token_never_priced_is_always_due(self):
        assert due_for_repricing(5, None) is True

    @pytest.mark.parametrize(
        ("age", "since_last", "expected"),
        [
            (30, 4, False),      # first hour, 5-minute cadence
            (30, 5, True),
            (200, 14, False),    # to 6h, quarter-hourly
            (200, 15, True),
            (1000, 59, False),   # to 24h, hourly
            (1000, 60, True),
            (5000, 359, False),  # to 7d, six-hourly
            (5000, 360, True),
        ],
    )
    def test_cadence_tightens_for_young_tokens(self, age, since_last, expected):
        assert due_for_repricing(age, since_last) is expected

    def test_nothing_is_due_after_the_widest_horizon(self):
        assert due_for_repricing(10_081, None) is False


class TestTrackerAgainstTheStore:
    def _seeded(self) -> tuple[Store, Snapshot]:
        store = Store()
        snap = Snapshot(
            chain="solana",
            contract="Tok1",
            trigger="mcap_250k",
            source="test",
            ts=T0,
            market=Market(mcap_usd=100_000.0),
        )
        store.append_snapshot(snap)
        return store, snap

    def test_observations_are_appended_with_their_offset(self):
        store, snap = self._seeded()
        with store:
            tracker = OutcomeTracker(store)
            tracker.record(
                [
                    obs(10, 200_000.0, snap.snapshot_id),
                    obs(120, 500_000.0, snap.snapshot_id),
                ]
            )
            stored = store.outcome_observations(snap.snapshot_id)
            assert [o.minutes_since(T0) for o in stored] == [10, 120]
            assert store.outcome_observation_count() == 2

    def test_an_observation_for_an_unknown_snapshot_is_refused(self):
        store, _ = self._seeded()
        with store, pytest.raises(AppendOnlyViolation, match="no snapshot"):
            OutcomeTracker(store).record([obs(10, 1.0, "does-not-exist")])

    def test_labels_are_appended_and_the_latest_one_wins(self):
        store, snap = self._seeded()
        with store:
            tracker = OutcomeTracker(store)
            tracker.record([obs(30, 400_000.0, snap.snapshot_id)])

            early = tracker.refresh_labels(as_of_ms=T0 + 2 * HOUR)
            assert len(early) == 1
            assert early[0].max_multiple_1h == pytest.approx(4.0)
            assert early[0].max_multiple_24h is None

            # 15k is below the 20k floor (20% of the 100k baseline), so this token
            # peaked at 4x and then died.
            tracker.record([obs(1400, 15_000.0, snap.snapshot_id)])
            late = tracker.refresh_labels(as_of_ms=T0 + 8 * DAY)
            assert late[0].max_multiple_24h == pytest.approx(4.0)
            assert late[0].survived_24h is False

            # Two label rows now exist; the early one was not overwritten.
            latest = store.latest_labels(snap.snapshot_id)
            assert latest is not None
            assert latest["max_multiple_24h"] == pytest.approx(4.0)

    def test_nothing_knowable_writes_no_row(self):
        store, snap = self._seeded()
        with store:
            tracker = OutcomeTracker(store)
            tracker.record([obs(5, 200_000.0, snap.snapshot_id)])
            assert tracker.refresh_labels(as_of_ms=T0 + 10 * MIN) == []
            assert store.labelled_snapshot_count() == 0

    def test_survivor_counts_ignore_unresolved_tokens(self):
        store, snap = self._seeded()
        with store:
            tracker = OutcomeTracker(store)
            tracker.record([obs(10_000, 5_000.0, snap.snapshot_id)])
            tracker.refresh_labels(as_of_ms=T0 + 8 * DAY)
            survivors, dead = store.survivor_counts("7d")
            assert (survivors, dead) == (0, 1)

    def test_due_reflects_the_schedule(self):
        store, snap = self._seeded()
        with store:
            tracker = OutcomeTracker(store)
            assert len(tracker.due(as_of_ms=T0 + 10 * MIN)) == 1
            tracker.record([obs(10, 200_000.0, snap.snapshot_id)])
            assert tracker.due(as_of_ms=T0 + 12 * MIN) == []
            assert len(tracker.due(as_of_ms=T0 + 30 * MIN)) == 1
