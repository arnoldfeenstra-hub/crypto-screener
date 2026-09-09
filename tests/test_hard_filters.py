"""Hard filter tests.

BUILD_BRIEF.md section 3 asks for these: "pure functions with unit tests". The
functions need no calibration, so unlike everything downstream they can be checked
against their spec exactly -- prompts/score.md step 1 is the spec, and each test
below names the condition it comes from.

The recurring theme is the three-way outcome. Most of these tests exist to pin down
that unknown is not clean, because that is the direction the filter quietly fails
in: a missing field read as "fine" turns a safety check into decoration.
"""

from __future__ import annotations

import pytest

from collectors.schema import Authorities, Deployer, Holders, Market, Snapshot
from filters.hard_filters import (
    CHECKS,
    FILTER_NAMES,
    FilterInput,
    Outcome,
    apply,
    apply_to_snapshot,
    check_concentration,
    check_deployer_history,
    check_freeze_authority,
    check_liquidity_depth,
    check_liquidity_lock,
    check_mint_authority,
    check_proxy_risk,
    check_sellability,
)

BASE_MS = 1788912000000  # 2026-09-09T00:00:00Z
DAY = 86_400_000


def clean(**overrides) -> FilterInput:
    """A token that passes all eight. Vary one field to test one filter."""
    defaults = dict(
        chain="solana",
        ticker="$OK",
        contract="Tok1",
        evaluated_at_ms=BASE_MS,
        honeypot=False,
        sells_failing=False,
        buy_tax_pct=0.0,
        sell_tax_pct=0.0,
        mint_revoked=True,
        freeze_active=False,
        lp_burned=True,
        top10_ex_lp_pct=18.0,
        liquidity_usd=30_000.0,
        mcap_usd=500_000.0,
        deployer_prior_rugs=0,
    )
    defaults.update(overrides)
    return FilterInput(**defaults)


def test_the_baseline_token_actually_passes():
    """Guard the fixture: if this token failed, every test below would be vacuous."""
    verdict = apply(clean())
    assert verdict.passed, verdict.reasons()
    assert verdict.rejected_by == []
    assert verdict.indeterminate_on == []


def test_all_eight_filters_from_the_prompt_are_implemented():
    assert len(CHECKS) == len(FILTER_NAMES) == 8
    assert {check(clean()).name for check in CHECKS} == set(FILTER_NAMES)


class TestSellability:
    def test_honeypot_is_rejected(self):
        assert check_sellability(clean(honeypot=True)).outcome is Outcome.REJECT

    def test_failing_sells_are_rejected(self):
        assert check_sellability(clean(sells_failing=True)).outcome is Outcome.REJECT

    @pytest.mark.parametrize("side", ["buy_tax_pct", "sell_tax_pct"])
    def test_tax_above_five_percent_either_side(self, side):
        assert check_sellability(clean(**{side: 5.01})).outcome is Outcome.REJECT
        # The condition is "> 5%", so exactly 5 passes.
        assert check_sellability(clean(**{side: 5.0})).outcome is Outcome.PASS

    def test_a_known_tax_still_rejects_even_when_honeypot_is_unknown(self):
        """Evidence beats absence: a 40% tax is a rejection, not an open question."""
        result = check_sellability(clean(honeypot=None, sell_tax_pct=40.0))
        assert result.outcome is Outcome.REJECT

    @pytest.mark.parametrize("missing", ["honeypot", "sells_failing", "buy_tax_pct"])
    def test_unknown_does_not_pass(self, missing):
        assert check_sellability(clean(**{missing: None})).outcome is Outcome.UNKNOWN


class TestAuthorities:
    def test_unrevoked_mint_is_rejected(self):
        assert check_mint_authority(clean(mint_revoked=False)).outcome is Outcome.REJECT

    def test_unknown_mint_authority_is_not_treated_as_revoked(self):
        assert check_mint_authority(clean(mint_revoked=None)).outcome is Outcome.UNKNOWN

    def test_active_freeze_is_rejected(self):
        assert check_freeze_authority(clean(freeze_active=True)).outcome is Outcome.REJECT

    def test_unknown_freeze_is_not_treated_as_inactive(self):
        assert check_freeze_authority(clean(freeze_active=None)).outcome is Outcome.UNKNOWN


class TestLiquidityLock:
    def test_burned_lp_passes(self):
        assert check_liquidity_lock(clean(lp_burned=True)).outcome is Outcome.PASS

    def test_neither_burned_nor_locked_is_rejected(self):
        result = check_liquidity_lock(clean(lp_burned=False, lp_locked_until_ms=None))
        assert result.outcome is Outcome.REJECT

    def test_a_lock_expiring_inside_thirty_days_is_rejected(self):
        result = check_liquidity_lock(
            clean(lp_burned=False, lp_locked_until_ms=BASE_MS + 29 * DAY)
        )
        assert result.outcome is Outcome.REJECT
        assert "29" in result.reason

    def test_a_lock_beyond_thirty_days_passes(self):
        result = check_liquidity_lock(
            clean(lp_burned=False, lp_locked_until_ms=BASE_MS + 31 * DAY)
        )
        assert result.outcome is Outcome.PASS

    def test_an_expired_lock_is_rejected(self):
        result = check_liquidity_lock(
            clean(lp_burned=False, lp_locked_until_ms=BASE_MS - 5 * DAY)
        )
        assert result.outcome is Outcome.REJECT

    def test_unknown_lock_status_does_not_pass(self):
        result = check_liquidity_lock(clean(lp_burned=None, lp_locked_until_ms=None))
        assert result.outcome is Outcome.UNKNOWN


class TestConcentration:
    @pytest.mark.parametrize(
        ("pct", "expected"),
        [(34.9, Outcome.PASS), (35.0, Outcome.PASS), (35.1, Outcome.REJECT)],
    )
    def test_the_threshold_is_strictly_above_thirty_five(self, pct, expected):
        assert check_concentration(clean(top10_ex_lp_pct=pct)).outcome is expected

    def test_unknown_concentration_does_not_pass(self):
        assert check_concentration(clean(top10_ex_lp_pct=None)).outcome is Outcome.UNKNOWN


class TestDeployerHistory:
    def test_one_prior_rug_is_enough(self):
        assert check_deployer_history(clean(deployer_prior_rugs=1)).outcome is Outcome.REJECT

    def test_a_clean_deployer_passes(self):
        assert check_deployer_history(clean(deployer_prior_rugs=0)).outcome is Outcome.PASS

    def test_an_unchecked_deployer_is_not_a_clean_one(self):
        assert (
            check_deployer_history(clean(deployer_prior_rugs=None)).outcome is Outcome.UNKNOWN
        )


class TestLiquidityDepth:
    def test_thin_liquidity_is_rejected(self):
        result = check_liquidity_depth(clean(liquidity_usd=9_000.0, mcap_usd=500_000.0))
        assert result.outcome is Outcome.REJECT  # 1.8%

    def test_exactly_two_percent_passes(self):
        result = check_liquidity_depth(clean(liquidity_usd=10_000.0, mcap_usd=500_000.0))
        assert result.outcome is Outcome.PASS

    def test_fdv_is_preferred_over_mcap_and_the_choice_is_recorded(self):
        with_fdv = check_liquidity_depth(
            clean(liquidity_usd=30_000.0, mcap_usd=500_000.0, fdv_usd=3_000_000.0)
        )
        assert with_fdv.outcome is Outcome.REJECT  # 1% of FDV, though 6% of mcap
        assert "FDV" in with_fdv.reason
        assert "mcap" in check_liquidity_depth(clean()).reason

    def test_a_zero_valuation_is_unknown_not_infinite_depth(self):
        result = check_liquidity_depth(clean(liquidity_usd=30_000.0, mcap_usd=0.0))
        assert result.outcome is Outcome.UNKNOWN

    def test_missing_liquidity_does_not_pass(self):
        assert check_liquidity_depth(clean(liquidity_usd=None)).outcome is Outcome.UNKNOWN


class TestProxyRisk:
    def test_solana_has_no_evm_style_proxy(self):
        assert check_proxy_risk(clean(chain="solana", upgradeable=None)).outcome is Outcome.PASS

    def test_other_chains_must_answer(self):
        assert check_proxy_risk(clean(chain="bnb", upgradeable=None)).outcome is Outcome.UNKNOWN

    def test_upgradeable_with_unrenounced_admin_is_rejected(self):
        result = check_proxy_risk(clean(chain="bnb", upgradeable=True, admin_renounced=False))
        assert result.outcome is Outcome.REJECT

    def test_upgradeable_with_renounced_admin_passes(self):
        result = check_proxy_risk(clean(chain="bnb", upgradeable=True, admin_renounced=True))
        assert result.outcome is Outcome.PASS

    def test_upgradeable_with_unknown_admin_does_not_pass(self):
        result = check_proxy_risk(clean(chain="bnb", upgradeable=True, admin_renounced=None))
        assert result.outcome is Outcome.UNKNOWN


class TestVerdict:
    def test_every_filter_runs_even_after_a_rejection(self):
        """The reason a token was excluded is worth more than the fact."""
        verdict = apply(clean(honeypot=True, mint_revoked=False, freeze_active=True))
        assert len(verdict.results) == 8
        assert set(verdict.rejected_by) == {
            "sellability",
            "mint_authority",
            "freeze_authority",
        }

    def test_rejection_and_indeterminacy_are_recorded_apart(self):
        verdict = apply(clean(mint_revoked=False, deployer_prior_rugs=None))
        assert verdict.rejected_by == ["mint_authority"]
        assert verdict.indeterminate_on == ["deployer_history"]
        assert verdict.excluded

    def test_both_kinds_exclude(self):
        assert apply(clean(mint_revoked=False)).excluded
        assert apply(clean(mint_revoked=None)).excluded

    def test_the_score_md_shape_flattens_both_into_rejected_by(self):
        payload = apply(clean(honeypot=True, deployer_prior_rugs=None)).to_dict()
        assert payload["score"] is None
        assert set(payload["rejected_by"]) == {"sellability", "deployer_history"}
        assert payload["hard_rejected_by"] == ["sellability"]
        assert payload["indeterminate_on"] == ["deployer_history"]

    def test_reasons_explain_only_the_exclusions(self):
        reasons = apply(clean(top10_ex_lp_pct=60.0)).reasons()
        assert "concentration" in reasons
        assert "35" in reasons["concentration"]
        assert "mint_authority" not in reasons


class TestFromSnapshot:
    def _snapshot(self, **overrides) -> Snapshot:
        return Snapshot(
            chain="solana",
            contract="Tok1",
            trigger="mcap_250k",
            source="test",
            ts=BASE_MS,
            authorities=Authorities(
                mint_revoked=overrides.get("mint_revoked", True),
                freeze_active=overrides.get("freeze_active", False),
                lp_locked_until=overrides.get("lp_locked_until"),
            ),
            holders=Holders(top10_ex_lp_pct=overrides.get("top10", 20.0)),
            market=Market(
                mcap_usd=overrides.get("mcap", 500_000.0),
                liquidity_usd=overrides.get("liq", 30_000.0),
            ),
            deployer=Deployer(prior_rugs=overrides.get("rugs", 0)),
        )

    def test_a_phase_zero_row_comes_back_indeterminate_not_clean(self):
        """The honest answer for a row with no safety data collected yet."""
        verdict = apply_to_snapshot(self._snapshot())
        assert verdict.excluded
        assert verdict.rejected_by == []
        assert set(verdict.indeterminate_on) == {"sellability", "liquidity_lock"}

    def test_supplying_the_safety_fields_completes_the_picture(self):
        verdict = apply_to_snapshot(
            self._snapshot(),
            honeypot=False,
            sells_failing=False,
            buy_tax_pct=0.0,
            sell_tax_pct=0.0,
            lp_burned=True,
        )
        assert verdict.passed, verdict.reasons()

    def test_snapshot_evidence_still_rejects(self):
        verdict = apply_to_snapshot(
            self._snapshot(mint_revoked=False, top10=70.0),
            honeypot=False,
            sells_failing=False,
            buy_tax_pct=0.0,
            sell_tax_pct=0.0,
            lp_burned=True,
        )
        assert set(verdict.rejected_by) == {"mint_authority", "concentration"}
