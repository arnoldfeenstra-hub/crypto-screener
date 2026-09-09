"""Bitquery parser tests.

Scope worth being honest about: these prove the parsers are **total** -- that they
survive missing keys, nulls, numbers sent as strings, and outright garbage without
raising and without inventing a value. They do **not** prove the GraphQL queries are
right, because the fixtures were hand-built to the documented EAP shape rather than
captured from a live endpoint.

So when the first real key arrives and `python -m collectors.bitquery --probe`
returns something differently shaped, that is expected, and re-recording
``fixtures/bitquery_responses.json`` from the probe output is the intended fix.
These tests are the harness that makes that a ten-minute job instead of a debugging
session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collectors.bitquery import (
    ReplayFeed,
    merge_metrics,
    parse_holder_counts,
    parse_new_pools,
    parse_token_metrics,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="module")
def responses() -> dict:
    return json.loads((FIXTURES / "bitquery_responses.json").read_text(encoding="utf-8"))


class TestNewPools:
    def test_parses_one_record_per_pooled_token(self, responses):
        pools = parse_new_pools(responses["new_pools"])
        # Three pools in, two out: the third has no BaseCurrency, so there is no
        # token to key on and it is dropped rather than guessed at.
        assert [p.contract for p in pools] == [
            "AAAA1111111111111111111111111111111111111111",
            "BBBB2222222222222222222222222222222222222222",
        ]

    def test_reads_numbers_that_arrive_as_strings(self, responses):
        alpha, beta = parse_new_pools(responses["new_pools"])
        assert alpha.liquidity_usd == pytest.approx(68_900.25)
        assert beta.liquidity_usd is None  # null in, null out

    def test_pool_creation_time_becomes_epoch_millis(self, responses):
        alpha = parse_new_pools(responses["new_pools"])[0]
        assert alpha.first_seen_at_ms == 1788945262000  # 2026-09-09T09:14:22Z

    def test_declared_socials_are_read_from_metadata(self, responses):
        alpha, beta = parse_new_pools(responses["new_pools"])
        assert (alpha.declared_telegram, alpha.declared_x) == (True, True)
        # No URI is "we did not look", not "the token declared nothing". Guessing
        # False here would corrupt the best-evidenced column in the schema.
        assert beta.declared_telegram is None

    def test_empty_and_garbage_responses_return_nothing_rather_than_raising(self, responses):
        assert parse_new_pools(responses["empty"]) == []
        assert parse_new_pools(responses["garbage"]) == []
        assert parse_new_pools({}) == []


class TestTokenMetrics:
    def test_market_cap_and_price(self, responses):
        parsed = parse_token_metrics(responses["token_metrics"])
        alpha = parsed["AAAA1111111111111111111111111111111111111111"]
        assert alpha["mcap_usd"] == pytest.approx(251_300.75)
        assert alpha["price_usd"] == pytest.approx(0.000251)

    def test_an_unparseable_number_is_null_not_zero(self, responses):
        parsed = parse_token_metrics(responses["token_metrics"])
        assert parsed["BBBB2222222222222222222222222222222222222222"]["volume_24h_usd"] is None

    def test_a_null_mint_authority_means_revoked(self, responses):
        parsed = parse_token_metrics(responses["token_metrics"])
        alpha = parsed["AAAA1111111111111111111111111111111111111111"]
        beta = parsed["BBBB2222222222222222222222222222222222222222"]
        assert alpha["mint_revoked"] is True
        assert alpha["freeze_active"] is False
        assert beta["mint_revoked"] is False
        assert beta["freeze_active"] is True

    def test_empty_response(self, responses):
        assert parse_token_metrics(responses["empty"]) == {}
        assert parse_token_metrics({}) == {}


class TestHolderCounts:
    def test_counts_parse_from_string_or_int(self, responses):
        counts = parse_holder_counts(responses["holder_counts"])
        assert counts["AAAA1111111111111111111111111111111111111111"] == 512
        assert counts["BBBB2222222222222222222222222222222222222222"] == 61

    def test_an_unparseable_count_is_absent_not_zero(self, responses):
        """A zero here would say "nobody holds it", which is a very different token."""
        counts = parse_holder_counts(responses["holder_counts"])
        assert "CCCC3333333333333333333333333333333333333333" not in counts


class TestMerge:
    def test_layers_metrics_onto_a_pool_record(self, responses):
        pool = parse_new_pools(responses["new_pools"])[0]
        parsed = parse_token_metrics(responses["token_metrics"])
        merged = merge_metrics(pool, parsed[pool.contract], 512)
        assert merged.mcap_usd == pytest.approx(251_300.75)
        assert merged.holder_count == 512
        assert merged.liquidity_usd == pytest.approx(68_900.25)  # kept from the pool

    def test_a_token_missing_from_the_metrics_response_keeps_its_nulls(self, responses):
        pool = parse_new_pools(responses["new_pools"])[1]
        merged = merge_metrics(pool, None, None)
        assert merged.mcap_usd is None
        assert merged.holder_count is None
        assert merged.contract == pool.contract

    def test_merged_records_still_answer_the_trigger(self, responses):
        from collectors.trigger_watcher import evaluate_metrics

        pool = parse_new_pools(responses["new_pools"])[0]
        parsed = parse_token_metrics(responses["token_metrics"])
        merged = merge_metrics(pool, parsed[pool.contract], 512)
        decision = evaluate_metrics(merged)
        assert decision.fired
        assert decision.trigger == "mcap_250k"
        assert decision.both_crossed  # 251k mcap and 512 holders


class TestReplayFeed:
    def test_reads_the_fixture_batches_in_order(self):
        feed = ReplayFeed.from_path(FIXTURES / "solana_replay.json")
        first = feed.poll()
        second = feed.poll()
        third = feed.poll()
        assert [len(first), len(second), len(third)] == [5, 4, 2]
        assert feed.poll() == []  # exhausted, not looping

    def test_nulls_in_the_fixture_stay_null(self):
        feed = ReplayFeed.from_path(FIXTURES / "solana_replay.json")
        by_contract = {m.contract: m for m in feed.poll()}
        hold = by_contract["HOLD2zYxWvUtSrQpOnMlKjIhGfEdCbA9876543210zz"]
        assert hold.mcap_usd is None
        assert hold.holder_count == 512

    def test_source_name_records_where_the_rows_came_from(self):
        feed = ReplayFeed.from_path(FIXTURES / "solana_replay.json")
        assert feed.source_name == "replay:solana_replay.json"
