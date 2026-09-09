"""Scoring tests -- prompts/score.md steps 1-3.

Two things are being defended here.

The first is that the arithmetic is deterministic and reproducible, because Phase 2
fits coefficients against it. A composite that drifts between runs cannot be
calibrated, and would make every fitted weight meaningless.

The second is that a missing pillar is not a zero pillar. In Phase 0 three of the
five pillars have no collector behind them, so a scorer that quietly treated null
as 0 would rank every token by how much of it happened to be measured -- and would
produce a confident-looking ordering out of nothing at all.
"""

from __future__ import annotations

import json

import pytest

from collectors.schema import Market, Snapshot
from collectors.store import Store
from scoring import pillars as P
from scoring.runner import (
    NO_EDGE_THRESHOLD,
    WEIGHTS_VERSION,
    Narrative,
    ScoringRunner,
    candidate_from_row,
    prompt_version,
    system_prompt,
)

SAFE = {
    "honeypot": False,
    "sells_failing": False,
    "buy_tax_pct": 0.0,
    "sell_tax_pct": 0.0,
    "lp_burned": True,
}


def candidate(**overrides) -> dict:
    base = {
        "snapshot_id": "snap1",
        "ticker": "$TEST",
        "chain": "solana",
        "contract": "Tok1",
        "age_hours": 4.0,
        "market_cap_usd": 400_000.0,
        "liquidity_usd": 40_000.0,
        "volume_24h_usd": 600_000.0,
        "holders": {"count": 700, "growth_6h_pct": 60.0, "top10_ex_lp_pct": 18.0},
        "authorities": {"mint_revoked": True, "freeze_active": False, "lp_locked_until": None},
        "deployer": {"address": "Dep1", "prior_launches": 2, "prior_rugs": 0},
        "launch": {"bundled_supply_pct": None, "sniper_wallets": None, "initial_buy_sol": None},
        "flows": {"net_flow_by_cohort": None, "smart_money_entries": None,
                  "smart_money_hit_rate": None},
        "social_x": {},
        "social_tg": {},
        "socials_declared": {"telegram": None, "x": None, "website": None},
        "trends": {},
        "lineage": {},
        "listings": ["dex"],
        "data_completeness": 1.0,
    }
    base.update(overrides)
    return base


class TestPillarsResolveOrStayNull:
    def test_a_phase_zero_candidate_resolves_only_the_onchain_pillars(self):
        result = P.score_candidate(candidate())
        scores = result.pillar_scores()
        assert scores["onchain_structure"] is not None
        assert scores["asymmetry_timing"] is not None
        # No collector fills these yet. Null, not zero.
        assert scores["attention_velocity"] is None
        assert scores["community_depth"] is None

    def test_an_unresolved_pillar_does_not_drag_the_composite_to_zero(self):
        """The composite is renormalised over what resolved, not averaged with zeros."""
        result = P.score_candidate(candidate())
        assert result.raw_score is not None
        assert result.raw_score > 0
        assert result.resolved_weight == pytest.approx(
            P.WEIGHTS["onchain_structure"] + P.WEIGHTS["asymmetry_timing"]
        )

    def test_a_candidate_with_nothing_measurable_scores_null_not_zero(self):
        blank = candidate(
            market_cap_usd=None,
            liquidity_usd=None,
            volume_24h_usd=None,
            age_hours=None,
            listings=None,
            holders={},
        )
        assert P.score_candidate(blank).raw_score is None

    def test_a_missing_component_is_skipped_rather_than_counted_as_zero(self):
        full = P.onchain_structure(candidate())
        partial = P.onchain_structure(candidate(holders={"top10_ex_lp_pct": 18.0}))
        assert full.score is not None and partial.score is not None
        assert partial.components["holder_growth"] is None

    def test_scoring_is_deterministic(self):
        first = P.score_candidate(candidate()).score
        for _ in range(20):
            assert P.score_candidate(candidate()).score == first


class TestPillarSignals:
    def test_coordinated_posting_caps_the_attention_pillar(self):
        """Author diversity under 0.25 implies coordination; the pillar is capped."""
        loud = candidate(
            social_x={
                "mentions_6h": 900, "mentions_24h": 1200, "unique_authors_24h": 60,
                "follower_weighted_reach": 4_000_000.0, "tier1_organic_engagements": 4,
                "reply_to_post_ratio": 1.2,
            }
        )
        result = P.attention_velocity(loud)
        assert result.score <= P.COORDINATED_PILLAR_CAP
        assert any("coordinated" in note for note in result.notes)

    def test_a_decelerating_curve_at_a_high_level_is_flagged(self):
        fading = candidate(
            social_x={"mentions_6h": 100, "mentions_24h": 2000, "unique_authors_24h": 1400}
        )
        assert any("decelerating" in n for n in P.attention_velocity(fading).notes)

    def test_a_big_room_with_no_speakers_is_flagged(self):
        dead = candidate(
            social_tg={"members": 20_000, "unique_speakers_24h": 30, "msgs_per_hour": 2.0}
        )
        assert any("dead room" in n for n in P.community_depth(dead).notes)

    def test_all_three_declared_socials_outscore_none(self):
        """The best-evidenced feature in the schema: 17.4x graduation lift."""
        none_declared = candidate(
            socials_declared={"telegram": False, "x": False, "website": False}
        )
        all_declared = candidate(
            socials_declared={"telegram": True, "x": True, "website": True}
        )
        low = P.lineage_meta_fit(none_declared).components["socials_declared"]
        high = P.lineage_meta_fit(all_declared).components["socials_declared"]
        assert high > low

    def test_undeclared_and_unchecked_are_not_the_same(self):
        unchecked = candidate(socials_declared={"telegram": None, "x": None, "website": None})
        assert P.lineage_meta_fit(unchecked).components["socials_declared"] is None

    def test_smart_money_needs_a_hit_rate_not_just_a_label(self):
        labelled_only = candidate(
            flows={"smart_money_entries": 8, "smart_money_hit_rate": None,
                   "net_flow_by_cohort": None}
        )
        assert P.onchain_structure(labelled_only).components["smart_money"] is None

    def test_a_late_derivative_scores_below_a_first_mover(self):
        first = candidate(lineage={"meta_tag": "cats", "position_in_meta": "first"})
        late = candidate(lineage={"meta_tag": "cats", "position_in_meta": "derivative"})
        assert P.lineage_meta_fit(first).score > P.lineage_meta_fit(late).score

    def test_a_higher_rung_on_the_listing_ladder_scores_higher(self):
        dex = candidate(listings=["dex"])
        spot = candidate(listings=["dex", "aggregator", "cex_spot"])
        assert P.asymmetry_timing(spot).score > P.asymmetry_timing(dex).score


class TestModifiers:
    def test_data_completeness_multiplies_the_score(self):
        full = P.score_candidate(candidate(), data_completeness=1.0)
        third = P.score_candidate(candidate(), data_completeness=0.35)
        assert third.score == pytest.approx(full.score * 0.35, rel=1e-6)
        assert any("completeness" in m for m in third.modifiers)

    def test_a_cold_tape_compresses_toward_the_midpoint(self):
        """Not a scale-down: in a cold tape most signals stop separating tokens."""
        neutral = P.score_candidate(candidate(), regime="neutral")
        cold = P.score_candidate(candidate(), regime="cold")
        assert abs(cold.score - 50.0) < abs(neutral.score - 50.0)

    def test_the_contradiction_penalty_fires_on_the_local_top_shape(self):
        topping = candidate(
            social_x={
                "mentions_6h": 800, "mentions_24h": 1000, "unique_authors_24h": 700,
                "follower_weighted_reach": 4_500_000.0, "tier1_organic_engagements": 5,
                "reply_to_post_ratio": 1.4,
            },
            flows={"net_flow_by_cohort": {"large": -50_000, "small": 40_000},
                   "smart_money_entries": None, "smart_money_hit_rate": None},
        )
        result = P.score_candidate(topping)
        assert any("contradiction" in m for m in result.modifiers)

    def test_no_contradiction_without_cohort_flow_data(self):
        result = P.score_candidate(candidate())
        assert not any("contradiction" in m for m in result.modifiers)


class TestRunner:
    def test_paper_mode_is_the_only_mode(self):
        with pytest.raises(NotImplementedError, match="paper mode"):
            ScoringRunner(paper_mode=False)

    def test_hard_filters_run_before_scoring_and_null_the_score(self):
        runner = ScoringRunner()
        row = runner.score_one(
            candidate(authorities={"mint_revoked": False, "freeze_active": False,
                                   "lp_locked_until": None}),
            safety=SAFE,
        )
        assert row["excluded"] is True
        assert row["score"] is None
        assert "mint_authority" in json.loads(row["rejected_by"])

    def test_a_strong_social_profile_cannot_override_a_hard_filter(self):
        """score.md step 1: no exceptions, no overrides."""
        runner = ScoringRunner()
        row = runner.score_one(
            candidate(
                authorities={"mint_revoked": False, "freeze_active": True,
                             "lp_locked_until": None},
                social_x={"mentions_6h": 5000, "mentions_24h": 6000,
                          "unique_authors_24h": 4000, "follower_weighted_reach": 9e6,
                          "tier1_organic_engagements": 20, "reply_to_post_ratio": 1.4},
                socials_declared={"telegram": True, "x": True, "website": True},
            ),
            safety=SAFE,
        )
        assert row["score"] is None

    def test_every_row_carries_its_inputs_and_versions(self):
        """Hard rule 6. A score without its inputs cannot be back-tested."""
        runner = ScoringRunner()
        row = runner.score_one(candidate(), safety=SAFE)
        assert row["prompt_version"] == prompt_version()
        assert row["weights_version"] == WEIGHTS_VERSION
        assert row["paper_mode"] is True
        stored = json.loads(row["input_snapshot"])
        assert stored["contract"] == "Tok1"
        assert stored["market_cap_usd"] == 400_000.0

    def test_without_a_narrator_the_qualitative_fields_stay_null(self):
        row = ScoringRunner().score_one(candidate(), safety=SAFE)
        assert row["thesis"] is None
        assert row["narrative_source"] == "none"
        assert row["model"] == "deterministic-only"

    def test_data_gaps_fall_back_to_the_unresolved_pillars(self):
        row = ScoringRunner().score_one(candidate(), safety=SAFE)
        gaps = json.loads(row["data_gaps"])
        assert "attention_velocity" in gaps
        assert "community_depth" in gaps

    def test_ranking_puts_the_best_score_first(self):
        runner = ScoringRunner()
        batch = runner.score_batch(
            [
                # Liquidity kept proportional to the cap so this one clears the
                # depth filter and actually reaches the ranking.
                candidate(snapshot_id="a", ticker="$WEAK", market_cap_usd=40_000_000.0,
                          liquidity_usd=1_200_000.0,
                          holders={"count": 700, "growth_6h_pct": 1.0,
                                   "top10_ex_lp_pct": 34.0}),
                candidate(snapshot_id="b", ticker="$STRONG"),
            ],
            safety=SAFE,
        )
        assert [r["ticker"] for r in batch.ranked] == ["$STRONG", "$WEAK"]
        assert [r["rank"] for r in batch.ranked] == [1, 2]

    def test_the_batch_verdict_says_plainly_when_there_is_no_edge(self):
        batch = ScoringRunner().score_batch(
            # Clears every hard filter, and is still a dull token: large cap, old,
            # barely growing, dex-only.
            [candidate(market_cap_usd=40_000_000.0, liquidity_usd=1_200_000.0,
                       age_hours=400.0,
                       holders={"count": 700, "growth_6h_pct": 0.5,
                                "top10_ex_lp_pct": 34.0})],
            safety=SAFE,
        )
        assert batch.ranked, "the candidate should clear the filters"
        assert "no edge in this batch" in batch.verdict()

    def test_even_a_high_score_is_reported_as_unmeasured(self):
        """There is no wording in which uncalibrated priors become a signal."""
        batch = ScoringRunner().score_batch([candidate()], safety=SAFE)
        assert "uncalibrated priors" in batch.verdict()

    def test_exclusion_summary_separates_evidence_from_ignorance(self):
        runner = ScoringRunner()
        batch = runner.score_batch(
            [
                candidate(snapshot_id="a", ticker="$BAD",
                          authorities={"mint_revoked": False, "freeze_active": False,
                                       "lp_locked_until": None}),
                candidate(snapshot_id="b", ticker="$UNKNOWN"),
            ],
        )
        summary = batch.exclusion_summary()
        assert summary["excluded_on_evidence"] == 1
        assert summary["excluded_as_unmeasured"] == 1

    def test_a_narrator_failure_does_not_lose_the_row(self):
        class Exploding:
            def narrate(self, *a, **k):
                raise RuntimeError("api down")

        row = ScoringRunner(narrator=Exploding()).score_one(candidate(), safety=SAFE)
        assert row["score"] is not None
        assert row["narrative_source"] == "error"

    def test_a_narrator_supplies_the_qualitative_fields(self):
        class Fake:
            def narrate(self, candidate, pillars, regime):
                return Narrative(
                    thesis="t", bear_case="b", falsifier="f", confidence="low",
                    data_gaps=("social_x",), source="fake-model",
                )

        row = ScoringRunner(narrator=Fake()).score_one(candidate(), safety=SAFE)
        assert (row["thesis"], row["bear_case"], row["falsifier"]) == ("t", "b", "f")
        assert json.loads(row["data_gaps"]) == ["social_x"]

    def test_a_rejected_candidate_does_not_spend_an_api_call(self):
        calls = []

        class Counting:
            def narrate(self, *a, **k):
                calls.append(1)
                return Narrative()

        ScoringRunner(narrator=Counting()).score_one(
            candidate(authorities={"mint_revoked": False, "freeze_active": False,
                                   "lp_locked_until": None}),
            safety=SAFE,
        )
        assert calls == []


class TestPromptWiring:
    def test_the_system_block_comes_from_the_versioned_file(self):
        text = system_prompt()
        assert text.startswith("## SYSTEM")
        assert "Gain Potential Score" in text
        assert "Hard filters run first" in text

    def test_the_prompt_version_is_read_from_the_file(self):
        assert prompt_version() == 2

    def test_the_no_edge_threshold_matches_the_prompt(self):
        assert NO_EDGE_THRESHOLD == 55.0


class TestAgainstTheStore:
    def test_scoring_stored_snapshots_writes_rows_with_their_inputs(self):
        with Store() as store:
            snap = Snapshot(
                chain="solana", contract="Tok1", trigger="mcap_250k", source="test",
                ticker="$TEST", market=Market(mcap_usd=400_000.0, liquidity_usd=40_000.0),
            )
            store.append_snapshot(snap)
            runner = ScoringRunner(store)
            batch = runner.run(limit=10, regime="neutral")

            assert len(batch.rows) == 1
            assert store.score_count() == 1
            stored = store.latest_scores()[0]
            assert stored["prompt_version"] == prompt_version()
            assert json.loads(stored["input_snapshot"])["contract"] == "Tok1"

    def test_rescoring_appends_rather_than_overwriting(self):
        with Store() as store:
            store.append_snapshot(
                Snapshot(chain="solana", contract="Tok1", trigger="mcap_250k",
                         source="test", market=Market(mcap_usd=400_000.0))
            )
            runner = ScoringRunner(store)
            runner.run(limit=10)
            runner.run(limit=10)
            assert store.score_count() == 2
            # The ranking reads only the newest run, not both mixed together.
            assert len(store.latest_scores()) == 1

    def test_candidate_packet_has_the_step_4_shape(self):
        with Store() as store:
            snap = Snapshot(
                chain="solana", contract="Tok1", trigger="mcap_250k", source="test",
                ticker="$TEST", listings=["dex"], market=Market(mcap_usd=400_000.0),
            )
            store.append_snapshot(snap)
            packet = candidate_from_row(store.recent_snapshots(1)[0])
        for key in ("ticker", "chain", "contract", "age_hours", "market_cap_usd",
                    "holders", "authorities", "deployer", "launch", "flows",
                    "social_x", "social_tg", "trends", "lineage", "listings"):
            assert key in packet, key
        assert packet["listings"] == ["dex"]
