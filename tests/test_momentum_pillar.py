"""Pillar G, and the promise that it changes no score.

The momentum group is new data and a new pillar. Both are wired all the way
through -- parsed, stored, scored, exported, served -- and the composite is
numerically identical with the pillar present and absent, because
``.claude/rules/stats.md`` admits only fitted coefficients into the weight vector
and nothing has fitted this one. That identity is asserted here rather than
promised in a comment.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from collectors.dexscreener import parse_pairs
from collectors.metrics import TokenMetrics
from collectors.schema import FEATURE_GROUPS, SCHEMA_VERSION, Momentum, Snapshot
from collectors.snapshot import build_snapshot
from collectors.trigger_rule import evaluate
from scoring.candidate import candidate_from_row
from scoring.pillars import (
    MOMENTUM_PRIOR_WEIGHT,
    WEIGHTS,
    composite,
    momentum_flow,
    score_candidate,
)


def candidate(**momentum):
    return {"momentum": momentum}


class TestWeightIsZero:
    def test_the_prior_weight_is_zero_and_the_vector_says_so(self):
        assert MOMENTUM_PRIOR_WEIGHT == 0.0
        assert WEIGHTS["momentum_flow"] == 0.0

    def test_the_composite_is_identical_with_and_without_the_pillar(self):
        scores = {
            "attention_velocity": 61.0,
            "community_depth": 44.0,
            "lineage_meta_fit": 70.0,
            "onchain_structure": 88.0,
            "asymmetry_timing": 37.0,
        }
        without, _ = composite(scores)
        with_pillar, _ = composite({**scores, "momentum_flow": 99.0})
        assert with_pillar == pytest.approx(without)

    def test_a_zero_weighted_pillar_cannot_carry_a_composite_alone(self):
        # Mindshare has the same property, and for the same reason: a resolved set
        # whose weights sum to zero has no composite at all, which is not a score
        # of zero.
        score, resolved_weight = composite({"momentum_flow": 92.0})
        assert score is None
        assert resolved_weight == 0.0

    def test_the_end_to_end_score_does_not_move(self):
        base = {
            "market_cap_usd": 400_000.0,
            "liquidity_usd": 40_000.0,
            "volume_24h_usd": 600_000.0,
            "age_hours": 2.0,
            "listings": ["dex"],
            "data_completeness": 0.5,
        }
        quiet = score_candidate(base, regime="neutral")
        loud = score_candidate(
            {**base, "momentum": {"buys_1h": 900, "sells_1h": 10}}, regime="neutral"
        )
        assert loud.score == pytest.approx(quiet.score)
        assert loud.pillar_scores()["momentum_flow"] is not None
        assert quiet.pillar_scores()["momentum_flow"] is None


class TestBuyPressure:
    def test_a_one_sided_book_scores_high(self):
        result = momentum_flow(candidate(buys_1h=180, sells_1h=20))
        assert result.components["buy_pressure_1h"] == 100.0

    def test_a_missing_sell_count_is_unknown_not_perfect(self):
        # The single most flattering way to be wrong about a token: a pool that
        # did not report sells is not a pool with no sells.
        result = momentum_flow(candidate(buys_1h=180, sells_1h=None))
        assert result.components["buy_pressure_1h"] is None

    def test_a_dead_window_is_not_a_division(self):
        assert momentum_flow(candidate(buys_1h=0, sells_1h=0)).components[
            "buy_pressure_1h"
        ] is None

    def test_pressure_falling_from_the_day_to_the_hour_is_called_out(self):
        result = momentum_flow(
            candidate(buys_1h=30, sells_1h=70, buys_24h=1200, sells_24h=800)
        )
        assert any("selling into the day's bid" in note for note in result.notes)
        assert result.components["pressure_trend"] is not None


class TestWindowSaturation:
    """The check that keeps Pillar G from repeating the mistake that made it.

    A token younger than six hours has all of its volume inside the six-hour
    window, so ``volume_1h == volume_6h`` and the acceleration is pinned at 6.0 by
    arithmetic. That is a fact about the token's age, not about its momentum.
    """

    def test_identical_windows_are_dropped_not_scored(self):
        result = momentum_flow(candidate(volume_1h_usd=5_000.0, volume_6h_usd=5_000.0))
        assert result.components["volume_acceleration"] is None

    def test_and_the_pillar_says_why(self):
        result = momentum_flow(candidate(volume_1h_usd=5_000.0, volume_6h_usd=5_000.0))
        assert any("younger than six hours" in note for note in result.notes)

    def test_a_real_ratio_is_scored(self):
        result = momentum_flow(candidate(volume_1h_usd=60_000.0, volume_6h_usd=180_000.0))
        assert result.components["volume_acceleration"] is not None

    def test_a_token_younger_than_the_window_is_not_scored_on_it(self):
        """Identical windows are only the under-an-hour end of the artefact.

        A 2h-old token trading perfectly flat has 1h = 10k and 6h = 20k -- two
        hours of trading, not six -- which reads as 3.0x the 6h rate and scored
        100. The same flat token at 8h reads 1.0x. That gap is its age.
        """
        young = momentum_flow(
            {"age_hours": 2.0, "momentum": {"volume_1h_usd": 10_000.0, "volume_6h_usd": 20_000.0}}
        )
        assert young.components["volume_acceleration"] is None
        assert any("younger than the six-hour window" in note for note in young.notes)
        grown = momentum_flow(
            {"age_hours": 8.0, "momentum": {"volume_1h_usd": 10_000.0, "volume_6h_usd": 60_000.0}}
        )
        assert grown.components["volume_acceleration"] is not None

    def test_a_young_tokens_price_slope_is_its_hour_alone(self):
        # Up 10% in two hours is 5%/h, not the 1.7%/h a sixth of it implies; the
        # six-hour average does not exist yet, so the hour is scored on its own.
        young = momentum_flow(
            {
                "age_hours": 2.0,
                "momentum": {"price_change_1h_pct": 5.0, "price_change_6h_pct": 10.0},
            }
        )
        alone = momentum_flow({"momentum": {"price_change_1h_pct": 5.0}})
        assert young.components["price_slope"] == alone.components["price_slope"]

    def test_draining_attention_is_called_out(self):
        result = momentum_flow(candidate(volume_1h_usd=5_000.0, volume_6h_usd=180_000.0))
        assert any("attention is draining" in note for note in result.notes)


class TestPriceSlope:
    def test_a_move_rolling_over_is_called_out(self):
        result = momentum_flow(candidate(price_change_1h_pct=-8.0, price_change_6h_pct=60.0))
        assert any("rolling over" in note for note in result.notes)

    def test_the_last_hour_beating_the_six_hour_average_scores_above_the_midpoint(self):
        result = momentum_flow(candidate(price_change_1h_pct=9.0, price_change_6h_pct=12.0))
        assert result.components["price_slope"] > 50.0

    def test_an_hour_alone_still_scores(self):
        result = momentum_flow(candidate(price_change_1h_pct=5.0))
        assert result.components["price_slope"] is not None


class TestNothingResolvesToNothing:
    def test_an_empty_momentum_group_is_null_not_zero(self):
        assert momentum_flow({}).score is None
        assert momentum_flow(candidate()).score is None

    def test_a_row_written_before_schema_7_scores_null(self):
        # Every pre-schema-7 row restores with these columns NULL, which is the
        # truth about those rows. A zero would say the collector measured no buys.
        assert momentum_flow(
            candidate(
                volume_1h_usd=None,
                volume_6h_usd=None,
                buys_1h=None,
                sells_1h=None,
                buys_24h=None,
                sells_24h=None,
                price_change_1h_pct=None,
                price_change_6h_pct=None,
            )
        ).score is None


class TestWiredThrough:
    def test_the_group_is_stored_but_not_counted_toward_completeness(self):
        assert "momentum" in FEATURE_GROUPS
        snapshot = Snapshot(chain="solana", contract="C", trigger="mcap_250k", source="t")
        assert snapshot.completeness()[1] == 54

    def test_momentum_data_moves_neither_completeness_nor_the_score(self):
        """The zero-weight claim, end to end. The composite-only identity test
        pinned data_completeness in its input, so it could not see this: the
        modifier multiplies the final score, and counting momentum's fields lifted
        a typical new row's completeness from 21/54 to 32/65."""
        common = dict(
            chain="solana",
            contract="C" * 32,
            observed_at_ms=1_789_000_000_000,
            source="dexscreener",
            ticker="$M",
            mcap_usd=300_000.0,
            liquidity_usd=45_000.0,
            volume_24h_usd=900_000.0,
            first_seen_at_ms=1_788_999_000_000,
        )
        flow = dict(
            volume_1h_usd=90_000.0,
            volume_6h_usd=300_000.0,
            txns_1h=120,
            buys_1h=80,
            sells_1h=40,
            buys_24h=900,
            sells_24h=700,
            price_change_5m_pct=0.4,
            price_change_1h_pct=6.0,
            price_change_6h_pct=22.0,
            price_change_24h_pct=48.0,
        )
        bare = build_snapshot(TokenMetrics(**common), evaluate(300_000.0, None))
        full = build_snapshot(TokenMetrics(**common, **flow), evaluate(300_000.0, None))
        assert full.momentum.buys_1h == 80 and bare.momentum.buys_1h is None
        assert full.completeness() == bare.completeness()

        bare_score = score_candidate(candidate_from_row(bare.to_row()), regime="neutral")
        full_score = score_candidate(candidate_from_row(full.to_row()), regime="neutral")
        assert full_score.score == pytest.approx(bare_score.score)
        assert full_score.pillar_scores()["momentum_flow"] is not None

    def test_the_schema_version_was_bumped_with_the_group(self):
        # The group landed at 7. The assertion is >= rather than == because later
        # versions add unrelated columns; what it defends is that the group never
        # ships without a bump, which is what lets a row say which schema wrote it.
        assert SCHEMA_VERSION >= 7

    def test_the_parser_keeps_every_window_the_api_reports(self):
        payload = json.loads(
            json.dumps(
                {
                    "pairs": [
                        {
                            "chainId": "solana",
                            "baseToken": {"address": "A" * 32, "symbol": "$M"},
                            "priceUsd": "0.001",
                            "marketCap": 300_000,
                            "liquidity": {"usd": 50_000},
                            "volume": {"h24": 900_000, "h6": 300_000, "h1": 90_000},
                            "txns": {
                                "h24": {"buys": 900, "sells": 700},
                                "h6": {"buys": 300, "sells": 200},
                                "h1": {"buys": 80, "sells": 40},
                            },
                            "priceChange": {"m5": 0.4, "h1": 6.0, "h6": 22.0, "h24": 48.0},
                            "pairCreatedAt": 1_788_900_000_000,
                        }
                    ]
                }
            )
        )
        aggregate = next(iter(parse_pairs(payload).values()))
        assert (aggregate.txns_1h, aggregate.buys_1h, aggregate.sells_1h) == (120, 80, 40)
        assert aggregate.price_change_1h_pct == 6.0
        assert aggregate.price_change_5m_pct == 0.4

    def test_a_pool_with_no_hour_block_reports_unknown_not_zero(self):
        payload = {
            "pairs": [
                {
                    "chainId": "solana",
                    "baseToken": {"address": "B" * 32, "symbol": "$Q"},
                    "txns": {"h24": {"buys": 10, "sells": 8}},
                }
            ]
        }
        aggregate = next(iter(parse_pairs(payload).values()))
        assert aggregate.txns_1h is None
        assert aggregate.buys_1h is None

    def test_the_snapshot_carries_it_and_the_candidate_packet_reads_it_back(self):
        metrics = TokenMetrics(
            chain="solana",
            contract="C" * 32,
            observed_at_ms=1_789_000_000_000,
            source="dexscreener",
            ticker="$M",
            mcap_usd=300_000.0,
            first_seen_at_ms=1_788_999_000_000,
            volume_1h_usd=90_000.0,
            volume_6h_usd=300_000.0,
            txns_1h=120,
            buys_1h=80,
            sells_1h=40,
            buys_24h=900,
            sells_24h=700,
            price_change_1h_pct=6.0,
            price_change_6h_pct=22.0,
        )
        snapshot = build_snapshot(metrics, evaluate(metrics.mcap_usd, None))
        assert snapshot.momentum.buys_1h == 80
        assert snapshot.momentum.price_change_6h_pct == 22.0

        row = snapshot.to_row()
        assert row["momentum_buys_1h"] == 80
        packet = candidate_from_row(row)
        assert packet["momentum"]["buys_1h"] == 80
        assert momentum_flow(packet).score is not None

    def test_the_group_default_is_all_null(self):
        assert all(value is None for value in asdict(Momentum()).values())
