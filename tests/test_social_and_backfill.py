"""Social collector and backfill tests -- Phase 0 items 3 and 5.

Two failure modes get most of the attention here.

For the social collectors: a failed poll that gets recorded as a zero. Since this
data cannot be backfilled, a zero written today is a wrong number nobody can ever
correct, and it would look exactly like a token nobody talked about.

For the backfill: snapshotting a historical token at today's price. That silently
destroys the lifecycle match the whole calibration design rests on, and it leaves
no trace -- the row looks perfectly normal.
"""

from __future__ import annotations

import pytest

from collectors.backfill import BACKFILL_SOURCE, BackfillRunner, reconstruct_trigger
from collectors.metrics import TokenMetrics
from collectors.schema import Market, Snapshot
from collectors.social_base import (
    OFFSETS_MINUTES,
    SocialObservation,
    derive_tg_metrics,
    derive_x_metrics,
    due_offsets,
    nearest_offset,
)
from collectors.social_tg import (
    TelegramCollector,
    TelegramError,
    extract_handle,
    parse_preview,
)
from collectors.social_x import XCollector, XError, build_query, parse_search
from collectors.store import AppendOnlyViolation, Store

T0 = 1788912000000
MIN = 60_000


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestOffsets:
    def test_the_offsets_are_the_ones_the_brief_names(self):
        assert OFFSETS_MINUTES == (0, 60, 360, 1440)

    @pytest.mark.parametrize(
        ("age", "expected"), [(0, 0), (5, 0), (58, 60), (370, 360), (1435, 1440), (700, None)]
    )
    def test_a_collection_is_filed_under_its_scheduled_offset(self, age, expected):
        assert nearest_offset(age) == expected

    def test_only_elapsed_offsets_are_due(self):
        assert due_offsets(90, collected=[]) == [0, 60]

    def test_a_collected_offset_is_not_due_again(self):
        assert due_offsets(1500, collected=[0, 60, 360]) == [1440]

    def test_a_missed_offset_stays_due(self):
        """A late count beats a hole, but it is filed under the offset it was for."""
        assert 60 in due_offsets(2000, collected=[0])


# ---------------------------------------------------------------------------
# X
# ---------------------------------------------------------------------------


class TestXParsing:
    def _payload(self) -> dict:
        return {
            "data": [
                {"author_id": "1", "public_metrics": {"like_count": 5, "retweet_count": 2}},
                {"author_id": "1", "public_metrics": {"like_count": 1, "retweet_count": 0}},
                {
                    "author_id": "2",
                    "public_metrics": {"like_count": 40, "retweet_count": 10},
                    "referenced_tweets": [{"type": "replied_to", "id": "9"}],
                },
            ],
            "includes": {
                "users": [
                    {"id": "1", "username": "a", "public_metrics": {"followers_count": 900}},
                    {"id": "2", "username": "b", "public_metrics": {"followers_count": 250_000}},
                ]
            },
        }

    def test_counts_are_raw(self):
        counts = parse_search(self._payload(), 360)
        assert counts["mentions_window"] == 3
        assert counts["unique_authors"] == 2
        assert counts["replies"] == 1
        assert counts["posts"] == 2
        assert counts["window_minutes"] == 360

    def test_a_big_engaged_account_counts_as_tier_one(self):
        assert parse_search(self._payload(), 360)["tier1_organic_engagements"] == 1

    def test_reach_is_summed_per_post_not_per_author(self):
        """Reach is impressions, so two posts from one account count twice.

        Author 1 posts twice at 900 followers, author 2 once at 250k: 251,800.
        Author *diversity* is the separate check that stops a single loud account
        reading as a crowd.
        """
        assert parse_search(self._payload(), 360)["follower_weighted_reach"] == 251_800.0

    def test_an_empty_response_is_zero_mentions_not_a_crash(self):
        counts = parse_search({}, 60)
        assert counts["mentions_window"] == 0
        assert counts["unique_authors"] is None

    def test_retweets_are_excluded_from_the_query(self):
        """Author diversity would read a retweet cascade as many independent voices."""
        assert "-is:retweet" in build_query("$AAA", None)

    def test_the_query_covers_ticker_and_contract(self):
        query = build_query("$AAA", "Mint111")
        assert "$AAA" in query and "Mint111" in query

    def test_a_bare_ticker_gets_its_dollar_sign(self):
        assert "$AAA" in build_query("AAA", None)

    def test_no_ticker_and_no_contract_is_an_error_not_an_empty_search(self):
        with pytest.raises(ValueError, match="ticker or a contract"):
            build_query(None, None)


class TestXCollector:
    def _store_with_snapshot(self) -> tuple[Store, Snapshot]:
        store = Store()
        snap = Snapshot(
            chain="solana", contract="Tok1", trigger="mcap_250k", source="test",
            ticker="$AAA", ts=T0, market=Market(mcap_usd=300_000.0),
        )
        store.append_snapshot(snap)
        return store, snap

    def test_a_failed_poll_is_a_row_with_an_error_not_a_zero(self):
        """The failure that cannot be undone: this data is not backfillable."""

        class Broken:
            def search_recent(self, *a, **k):
                raise XError("rate limited")

        store, snap = self._store_with_snapshot()
        with store:
            observation = XCollector(store, Broken()).collect_one(
                {"snapshot_id": snap.snapshot_id, "ticker": "$AAA", "contract": "Tok1"}, 60
            )
            assert observation.error == "rate limited"
            assert observation.mentions_window is None  # not 0

    def test_no_client_still_records_the_attempt(self):
        store, snap = self._store_with_snapshot()
        with store:
            observation = XCollector(store, None).collect_one(
                {"snapshot_id": snap.snapshot_id, "ticker": "$AAA", "contract": "Tok1"}, 0
            )
            assert observation.error == "no client configured"
            assert observation.mentions_window is None

    def test_a_successful_poll_stores_counts(self):
        class Fake:
            def search_recent(self, query, minutes, max_results=100):
                return {
                    "data": [{"author_id": "1"}],
                    "includes": {"users": [{"id": "1", "public_metrics": {"followers_count": 10}}]},
                }

        store, _ = self._store_with_snapshot()
        with store:
            collector = XCollector(store, Fake())
            written = collector.run(as_of_ms=T0 + 90 * MIN)
            assert [o.offset_minutes for o in written] == [0, 60]
            assert all(o.mentions_window == 1 for o in written)
            assert store.social_observation_count() == 2

    def test_the_same_offset_is_never_collected_twice(self):
        class Fake:
            def search_recent(self, *a, **k):
                return {"data": []}

        store, _ = self._store_with_snapshot()
        with store:
            collector = XCollector(store, Fake())
            collector.run(as_of_ms=T0 + 90 * MIN)
            again = collector.run(as_of_ms=T0 + 90 * MIN)
            assert again == []
            assert store.social_observation_count() == 2

    def test_a_duplicate_offset_write_is_refused(self):
        store, snap = self._store_with_snapshot()
        with store:
            observation = SocialObservation(
                snapshot_id=snap.snapshot_id, platform="x", offset_minutes=0, mentions_window=5
            )
            store.append_social_observations([observation])
            with pytest.raises(AppendOnlyViolation):
                store.append_social_observations(
                    [SocialObservation(snapshot_id=snap.snapshot_id, platform="x",
                                       offset_minutes=0, mentions_window=999)]
                )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class TestTelegramParsing:
    def test_member_and_online_counts(self):
        html = (
            '<div class="tgme_page"><div class="tgme_page_extra">'
            "12 345 members, 678 online</div></div>"
        )
        counts = parse_preview(html)
        assert counts == {"exists": True, "members": 12345, "online": 678}

    def test_counts_with_separators(self):
        html = '<div class="tgme_page">1,234 subscribers</div>'
        assert parse_preview(html)["members"] == 1234

    def test_a_page_that_exists_with_no_counts_is_not_a_zero(self):
        html = '<div class="tgme_page">no numbers here</div>'
        counts = parse_preview(html)
        assert counts["exists"] is True
        assert counts["members"] is None

    def test_a_missing_channel_is_marked_absent(self):
        assert parse_preview("<html>404</html>")["exists"] is False

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("https://t.me/mychannel", "mychannel"),
            ("t.me/mychannel", "mychannel"),
            ("@mychannel", "mychannel"),
            ("mychannel", "mychannel"),
            ("$TICKER", None),
            (None, None),
        ],
    )
    def test_handle_extraction(self, value, expected):
        assert extract_handle(value) == expected


class TestTelegramCollector:
    def _store(self) -> tuple[Store, Snapshot]:
        store = Store()
        snap = Snapshot(
            chain="solana", contract="Tok1", trigger="mcap_250k", source="test",
            ticker="$AAA", ts=T0, market=Market(mcap_usd=300_000.0),
        )
        store.append_snapshot(snap)
        return store, snap

    def test_no_handle_is_recorded_as_such(self):
        store, snap = self._store()
        with store:
            observation = TelegramCollector(store, None).collect_one(
                {"snapshot_id": snap.snapshot_id, "ticker": "$AAA"}, 0
            )
            assert "no telegram handle" in observation.error

    def test_a_failed_fetch_is_an_error_row(self):
        class Broken:
            def fetch(self, handle):
                raise TelegramError("HTTP 503")

        store, snap = self._store()
        with store:
            observation = TelegramCollector(store, Broken()).collect_one(
                {"snapshot_id": snap.snapshot_id, "telegram": "t.me/mychannel"}, 60
            )
            assert observation.error == "HTTP 503"
            assert observation.members is None

    def test_a_successful_fetch_stores_members(self):
        class Fake:
            def fetch(self, handle):
                return {"exists": True, "members": 4200, "online": 130}

        store, snap = self._store()
        with store:
            observation = TelegramCollector(store, Fake()).collect_one(
                {"snapshot_id": snap.snapshot_id, "telegram": "t.me/mychannel"}, 60
            )
            assert observation.members == 4200
            assert observation.handle == "mychannel"

    def test_the_preview_path_leaves_speaker_fields_null(self):
        """It cannot read them, so it says so rather than implying a dead room."""

        class Fake:
            def fetch(self, handle):
                return {"exists": True, "members": 4200, "online": 130}

        store, snap = self._store()
        with store:
            observation = TelegramCollector(store, Fake()).collect_one(
                {"snapshot_id": snap.snapshot_id, "telegram": "t.me/mychannel"}, 60
            )
            assert observation.unique_speakers is None
            assert observation.messages_window is None


# ---------------------------------------------------------------------------
# Derivation at read time
# ---------------------------------------------------------------------------


class TestDerivation:
    def test_x_metrics_are_derived_from_raw_counts(self):
        observations = [
            SocialObservation(snapshot_id="s", platform="x", offset_minutes=360,
                              mentions_window=120, unique_authors=80),
            SocialObservation(snapshot_id="s", platform="x", offset_minutes=1440,
                              mentions_window=500, unique_authors=300,
                              replies=100, posts=400,
                              follower_weighted_reach=1_000_000.0),
        ]
        derived = derive_x_metrics(observations)
        assert derived["mentions_6h"] == 120
        assert derived["mentions_24h"] == 500
        assert derived["unique_authors_24h"] == 300
        assert derived["reply_to_post_ratio"] == pytest.approx(0.25)

    def test_a_missing_offset_derives_to_null_not_zero(self):
        derived = derive_x_metrics(
            [SocialObservation(snapshot_id="s", platform="x", offset_minutes=0,
                               mentions_window=3)]
        )
        assert derived["mentions_24h"] is None

    def test_tg_growth_is_derived_across_offsets(self):
        observations = [
            SocialObservation(snapshot_id="s", platform="telegram", offset_minutes=0,
                              exists=True, members=1000),
            SocialObservation(snapshot_id="s", platform="telegram", offset_minutes=360,
                              exists=True, members=1500),
        ]
        derived = derive_tg_metrics(observations)
        assert derived["members"] == 1500
        assert derived["member_growth_6h_pct"] == pytest.approx(50.0)

    def test_no_observations_derive_to_nothing(self):
        assert derive_x_metrics([]) == {}
        assert derive_tg_metrics([]) == {}


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def history_point(minutes: int, mcap: float | None, holders: int | None = None) -> TokenMetrics:
    return TokenMetrics(
        chain="solana",
        contract="Hist1",
        observed_at_ms=T0 + minutes * MIN,
        source="archive",
        ticker="$HIST",
        first_seen_at_ms=T0,
        mcap_usd=mcap,
        holder_count=holders,
    )


class TestReconstruction:
    def test_the_first_crossing_is_the_one_that_counts(self):
        history = [
            history_point(0, 50_000.0),
            history_point(60, 260_000.0),   # first crossing
            history_point(120, 900_000.0),
        ]
        crossing = reconstruct_trigger(history)
        assert crossing is not None
        assert crossing.metrics.mcap_usd == 260_000.0
        assert crossing.metrics.observed_at_ms == T0 + 60 * MIN

    def test_an_out_of_order_archive_still_finds_the_real_first(self):
        """An archive that comes back unsorted must not produce a false 'first'."""
        history = [
            history_point(120, 900_000.0),
            history_point(60, 260_000.0),
            history_point(0, 50_000.0),
        ]
        crossing = reconstruct_trigger(history)
        assert crossing is not None
        assert crossing.metrics.observed_at_ms == T0 + 60 * MIN

    def test_a_token_that_never_crossed_returns_nothing(self):
        history = [history_point(0, 10_000.0), history_point(60, 90_000.0)]
        assert reconstruct_trigger(history) is None

    def test_the_holders_rule_applies_the_same_way(self):
        crossing = reconstruct_trigger(
            [history_point(0, None, 100), history_point(60, None, 640)]
        )
        assert crossing is not None
        assert crossing.decision.trigger == "holders_500"

    def test_an_empty_history_returns_nothing(self):
        assert reconstruct_trigger([]) is None


class TestBackfillRunner:
    def test_the_snapshot_carries_the_historical_time_not_today(self):
        """The failure that silently breaks the cohort match for the whole sample."""
        with Store() as store:
            runner = BackfillRunner(store)
            runner.ingest([[history_point(0, 50_000.0), history_point(60, 260_000.0)]])
            row = store.fetch_by_contract("solana", "Hist1")
            assert row is not None
            assert row["ts"] == T0 + 60 * MIN
            assert row["market_mcap_usd"] == 260_000.0
            assert row["age_at_trigger_minutes"] == 60

    def test_backfilled_rows_are_labelled_as_such(self):
        with Store() as store:
            BackfillRunner(store).ingest([[history_point(60, 260_000.0)]])
            row = store.fetch_by_contract("solana", "Hist1")
            assert row["source"] == BACKFILL_SOURCE

    def test_a_token_that_never_crossed_is_not_written(self):
        with Store() as store:
            runner = BackfillRunner(store)
            runner.ingest([[history_point(0, 10_000.0)]])
            assert store.snapshot_count() == 0
            assert runner.stats["considered"] == 1
            assert runner.stats["crossed"] == 0

    def test_a_live_row_is_never_overwritten_by_a_backfill(self):
        with Store() as store:
            live = Snapshot(
                chain="solana", contract="Hist1", trigger="mcap_250k", source="bitquery",
                ts=T0 + 60 * MIN, market=Market(mcap_usd=255_000.0),
            )
            store.append_snapshot(live)
            runner = BackfillRunner(store)
            runner.ingest([[history_point(60, 260_000.0)]])
            assert store.snapshot_count() == 1
            assert runner.stats["already_present"] == 1
            assert store.fetch_by_contract("solana", "Hist1")["market_mcap_usd"] == 255_000.0

    def test_the_backfill_uses_the_same_rule_as_the_live_watcher(self):
        """Not a reimplementation: it imports evaluate() so the two cannot drift."""
        from collectors import backfill
        from collectors.trigger_watcher import evaluate

        assert backfill.evaluate is evaluate
