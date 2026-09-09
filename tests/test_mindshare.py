"""Mindshare tests.

Mindshare is the first feature this repo added that did not come from the brief, so
these tests spend most of their effort on the two ways a new feature can quietly
corrupt a dataset: imputing a zero where there was no measurement, and putting an
invented weight into the composite.

The third concern is recomputability. BUILD_BRIEF.md section 3 item 3 says to store
raw counts rather than derived scores, because derived formulas change. Mindshare
is a derived score, so the raw components *and the denominators* have to be on the
row -- and there is a test here that changes the formula and re-derives an old row
under it, because that is the property the storage design exists to provide.
"""

from __future__ import annotations

import pytest

from collectors import mindshare as M
from collectors.metrics import TokenMetrics
from collectors.schema import Snapshot
from collectors.snapshot import build_snapshot
from collectors.trigger_rule import evaluate
from scoring import pillars as P

TS = 1788912000000


def token(contract: str, **fields) -> TokenMetrics:
    base = dict(chain="solana", contract=contract, observed_at_ms=TS, source="dexscreener")
    base.update(fields)
    return TokenMetrics(**base)


class TestShareArithmetic:
    def test_shares_are_of_the_universe_and_sum_to_one_hundred(self):
        universe = [
            token("A", txns_24h=600, volume_24h_usd=600.0, boost_total=600.0),
            token("B", txns_24h=300, volume_24h_usd=300.0, boost_total=300.0),
            token("C", txns_24h=100, volume_24h_usd=100.0, boost_total=100.0),
        ]
        out = M.compute(universe)
        assert out[("solana", "A")].share_pct == pytest.approx(60.0)
        assert sum(o.share_pct for o in out.values()) == pytest.approx(100.0)

    def test_components_are_averaged_over_the_ones_that_resolved(self):
        """A token missing one component is scored on the other two, not penalised.

        Treating an unreported component as zero would score the collector, not the
        token -- the same mistake _mean() avoids in every other pillar.
        """
        universe = [
            token("A", txns_24h=100, volume_24h_usd=900.0),  # no boost figure at all
            token("B", txns_24h=100, volume_24h_usd=100.0, boost_total=50.0),
        ]
        out = M.compute(universe)
        a = out[("solana", "A")]
        # 50% of transactions, 90% of volume, boosts skipped -> 70, not 46.7.
        assert a.share_pct == pytest.approx(70.0)
        assert a.components["boost_total"] is None

    def test_a_measured_zero_is_a_real_zero_and_a_missing_field_is_not(self):
        """The distinction the whole no-imputation rule rests on, in one test."""
        measured = M.compute(
            [
                token("A", boost_total=0.0, txns_24h=100),
                token("B", boost_total=100.0, txns_24h=100),
            ]
        )
        assert measured[("solana", "A")].components["boost_total"] == 0.0

        absent = M.compute(
            [token("A", txns_24h=100), token("B", boost_total=100.0, txns_24h=100)]
        )
        assert absent[("solana", "A")].components["boost_total"] is None
        # The zero drags A's share down; the absence does not.
        assert measured[("solana", "A")].share_pct < absent[("solana", "A")].share_pct

    def test_a_token_with_nothing_measured_has_no_mindshare_not_zero(self):
        out = M.compute([token("A"), token("B", txns_24h=10)])
        a = out[("solana", "A")]
        assert a.share_pct is None
        assert a.measured is False
        assert a.rank is None  # not ranked last -- not ranked

    def test_a_universe_with_no_denominator_yields_no_shares(self):
        out = M.compute([token("A"), token("B")])
        assert all(o.share_pct is None for o in out.values())

    def test_a_universe_of_one_still_reports_its_size(self):
        """100% of a universe of one is not a fact about the market, and says so."""
        out = M.compute([token("A", txns_24h=5)])
        observation = out[("solana", "A")]
        assert observation.share_pct == pytest.approx(100.0)
        assert observation.universe_size == 1

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5, True])
    def test_junk_values_are_treated_as_missing(self, bad):
        out = M.compute([token("A", txns_24h=bad), token("B", txns_24h=10)])
        assert out[("solana", "A")].components["txns_24h"] is None

    def test_rank_and_percentile_cover_only_the_measured_tokens(self):
        out = M.compute(
            [
                token("A", txns_24h=100),
                token("B", txns_24h=50),
                token("C"),  # unmeasured
            ]
        )
        assert out[("solana", "A")].rank == 1
        assert out[("solana", "B")].rank == 2
        assert out[("solana", "C")].percentile is None
        assert out[("solana", "A")].percentile == pytest.approx(100.0)

    def test_it_works_on_dicts_as_well_as_records(self):
        out = M.compute([{"chain": "bnb", "contract": "X", "txns_24h": 10}])
        assert out[("bnb", "X")].share_pct == pytest.approx(100.0)


class TestRecomputability:
    def test_the_denominators_travel_on_the_row(self):
        universe = [
            token("A", txns_24h=600, volume_24h_usd=1000.0, boost_total=5.0),
            token("B", txns_24h=400, volume_24h_usd=3000.0),
        ]
        out = M.compute(universe)
        a = out[("solana", "A")]
        assert a.universe_txns_24h == 1000
        assert a.universe_volume_24h_usd == pytest.approx(4000.0)
        assert a.universe_boost_total == pytest.approx(5.0)

    def test_a_stored_row_can_be_re_derived_under_a_different_formula(self):
        """The payoff for storing denominators: old rows survive a formula change.

        Social history cannot be backfilled, so a share computed today under weights
        that get revised next month has to be recomputable or it is lost.
        """
        universe = [
            token("A", txns_24h=600, volume_24h_usd=1000.0),
            token("B", txns_24h=400, volume_24h_usd=3000.0),
        ]
        shares = M.compute(universe)
        snapshot = build_snapshot(
            token("A", txns_24h=600, volume_24h_usd=1000.0, mcap_usd=300_000.0),
            evaluate(300_000.0, None),
            mindshare=shares[("solana", "A")],
        )
        row = snapshot.to_row()

        assert M.recompute_share(row) == pytest.approx(row["mindshare_share_pct"])
        # Volume only: 1000/4000.
        volume_only = M.recompute_share(row, {"volume_24h_usd": 1.0})
        assert volume_only == pytest.approx(25.0)
        # Transactions only: 600/1000.
        txns_only = M.recompute_share(row, {"txns_24h": 1.0})
        assert txns_only == pytest.approx(60.0)

    def test_recomputing_a_row_with_no_components_gives_none(self):
        row = Snapshot(chain="solana", contract="X", trigger="mcap_250k", source="t").to_row()
        assert M.recompute_share(row) is None


class TestSchemaGroup:
    def test_no_observation_gives_an_all_null_group(self):
        group = M.to_schema_group(None)
        assert all(getattr(group, f) is None for f in group.__slots__)

    def test_the_group_round_trips_into_a_snapshot_row(self):
        shares = M.compute([token("A", txns_24h=10, boost_total=2.0, pair_count=3)])
        snapshot = build_snapshot(
            token("A", mcap_usd=260_000.0),
            evaluate(260_000.0, None),
            mindshare=shares[("solana", "A")],
        )
        row = snapshot.to_row()
        assert row["mindshare_share_pct"] == pytest.approx(100.0)
        assert row["mindshare_pair_count"] == 3
        assert row["mindshare_universe_size"] == 1

    def test_mindshare_counts_toward_data_completeness(self):
        """Missingness is a feature; a row with mindshare is a more complete row."""
        without = build_snapshot(token("A", mcap_usd=260_000.0), evaluate(260_000.0, None))
        shares = M.compute([token("A", txns_24h=10, volume_24h_usd=5.0)])
        with_it = build_snapshot(
            token("A", mcap_usd=260_000.0),
            evaluate(260_000.0, None),
            mindshare=shares[("solana", "A")],
        )
        assert with_it.completeness()[0] > without.completeness()[0]
        assert with_it.completeness()[1] == without.completeness()[1]


class TestPillar:
    def candidate(self, **mindshare):
        return {"mindshare": mindshare}

    def test_the_pillar_is_none_when_nothing_was_measured(self):
        assert P.mindshare(self.candidate()).score is None
        assert P.mindshare({}).score is None

    def test_percentile_drives_the_score(self):
        high = P.mindshare(self.candidate(percentile=95.0)).score
        low = P.mindshare(self.candidate(percentile=5.0)).score
        assert high > low

    def test_bought_attention_scores_below_traded_attention(self):
        """Bought and earned mindshare are identical in a share number.

        They are opposite signals, so the ratio between them is separated out
        rather than blended into the share.
        """
        organic = P.mindshare(
            self.candidate(
                percentile=80.0,
                boost_total=1.0,
                universe_boost_total=100.0,
                txns_24h=50.0,
                universe_txns_24h=100.0,
            )
        )
        bought = P.mindshare(
            self.candidate(
                percentile=80.0,
                boost_total=90.0,
                universe_boost_total=100.0,
                txns_24h=1.0,
                universe_txns_24h=100.0,
            )
        )
        assert bought.score < organic.score
        assert any("bought" in note for note in bought.notes)
        assert not bought.notes or "bought" not in " ".join(organic.notes)

    def test_a_missing_denominator_leaves_the_tilt_unmeasured(self):
        pillar = P.mindshare(self.candidate(percentile=50.0, boost_total=10.0))
        assert pillar.components["organic_tilt"] is None


class TestZeroWeight:
    """The composite must be numerically identical to the version without mindshare.

    .claude/rules/stats.md allows only fitted coefficients into the weight vector.
    Mindshare has no outcome data behind it, so its prior weight is 0.0 -- and that
    has to be a fact the tests check, not a comment somebody can quietly edit.
    """

    def full_candidate(self):
        return {
            "market_cap_usd": 400_000.0,
            "liquidity_usd": 60_000.0,
            "volume_24h_usd": 600_000.0,
            "age_hours": 5.0,
            "listings": ["dex"],
            "holders": {"growth_6h_pct": 40.0, "top10_ex_lp_pct": 18.0},
            "launch": {"bundled_supply_pct": 4.0},
            "flows": {},
            "socials_declared": {"telegram": True, "x": True, "website": True},
            "lineage": {},
            "trends": {},
            "social_x": {},
            "social_tg": {},
            "mindshare": {
                "percentile": 99.0,
                "share_pct": 40.0,
                "pair_count": 6,
                "txns_24h": 100.0,
                "universe_txns_24h": 100.0,
                "boost_total": 0.0,
                "universe_boost_total": 100.0,
            },
        }

    def test_the_prior_weight_is_zero(self):
        assert P.WEIGHTS["mindshare"] == 0.0
        assert P.MINDSHARE_PRIOR_WEIGHT == 0.0

    def test_the_five_original_weights_are_unchanged(self):
        assert P.WEIGHTS["attention_velocity"] == 0.28
        assert P.WEIGHTS["community_depth"] == 0.20
        assert P.WEIGHTS["lineage_meta_fit"] == 0.15
        assert P.WEIGHTS["onchain_structure"] == 0.22
        assert P.WEIGHTS["asymmetry_timing"] == 0.15
        assert sum(v for k, v in P.WEIGHTS.items() if k != "mindshare") == pytest.approx(1.0)

    def test_a_maximal_mindshare_does_not_move_the_composite(self):
        candidate = self.full_candidate()
        with_it = P.score_candidate(candidate)
        without = P.score_candidate({**candidate, "mindshare": {}})
        assert with_it.score == pytest.approx(without.score)
        assert with_it.raw_score == pytest.approx(without.raw_score)

    def test_the_pillar_is_still_computed_and_reported(self):
        """Zero weight means it does not move the score, not that it is not measured."""
        result = P.score_candidate(self.full_candidate())
        assert result.pillar_scores()["mindshare"] is not None

    def test_a_row_where_only_mindshare_resolved_has_no_composite(self):
        """No score, rather than a score built entirely on a zero-weighted pillar."""
        result = P.score_candidate({"mindshare": {"percentile": 90.0}})
        assert result.raw_score is None
        assert result.score is None
        assert result.pillar_scores()["mindshare"] is not None
