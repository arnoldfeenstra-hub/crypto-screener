"""Safety source tests.

The failure mode this module has to be defended against is not "wrong number", it
is **false pass**. Six of the eight hard filters now take their answer from here,
and a parser that turns a missing field into a reassuring boolean would quietly
convert the screen into decoration. So most of what follows checks the direction of
the errors: unknown stays unknown, disagreement resolves toward the unsafe answer,
and a degraded lookup makes the screener stricter rather than looser.

Nothing here opens a socket -- tests/conftest.py makes that impossible.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from collectors.safety import (
    GoPlusClient,
    RugCheckClient,
    SafetyReport,
    SafetySource,
    merge,
    parse_goplus_address_security,
    parse_goplus_evm,
    parse_goplus_solana,
    parse_rugcheck,
)
from collectors.schema import now_ms
from collectors.store import Store
from filters.hard_filters import Outcome, apply
from scoring.candidate import filter_input_from_candidate

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "safety_responses.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))

CLEAN_EVM = "0xaaa0000000000000000000000000000000000001"
HONEYPOT_EVM = "0xbbb0000000000000000000000000000000000002"
LOCKED_EVM = "0xccc0000000000000000000000000000000000003"
SOL_MINT = "SoLMint11111111111111111111111111111111111"


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub_opener(routes, calls=None):
    def opener(request, timeout=None):
        url = request.full_url
        if calls is not None:
            calls.append(url)
        for fragment, payload in routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return _Response(payload)
        raise AssertionError(f"no stub route for {url}")

    return opener


def goplus(routes, calls=None) -> GoPlusClient:
    client = GoPlusClient(opener=stub_opener(routes, calls), sleep=lambda _s: None)
    client.throttle._sleep = lambda _s: None
    client.throttle._clock = lambda: 0.0
    return client


def rugcheck(routes) -> RugCheckClient:
    client = RugCheckClient(opener=stub_opener(routes), sleep=lambda _s: None)
    client.throttle._sleep = lambda _s: None
    client.throttle._clock = lambda: 0.0
    return client


# ---------------------------------------------------------------------------
# EVM parsing
# ---------------------------------------------------------------------------


class TestGoPlusEvm:
    def clean(self) -> SafetyReport:
        return parse_goplus_evm(DATA["goplus_evm_clean"], "bnb")[CLEAN_EVM]

    def honeypot(self) -> SafetyReport:
        return parse_goplus_evm(DATA["goplus_evm_honeypot"], "bnb")[HONEYPOT_EVM]

    def test_a_clean_token_reads_clean(self):
        report = self.clean()
        assert report.honeypot is False
        assert report.sells_failing is False
        assert report.mint_revoked is True
        assert report.freeze_active is False
        assert report.upgradeable is False
        assert report.admin_renounced is True

    def test_taxes_are_converted_from_fractions_to_percent(self):
        """GoPlus sends "0.01" for 1%; the filter's threshold is in percent."""
        report = self.clean()
        assert report.buy_tax_pct == pytest.approx(1.0)
        assert report.sell_tax_pct == pytest.approx(1.0)

    def test_a_honeypot_reads_as_a_honeypot(self):
        report = self.honeypot()
        assert report.honeypot is True
        assert report.sells_failing is True
        assert report.sell_tax_pct == pytest.approx(99.0)
        assert report.mint_revoked is False
        assert report.freeze_active is True
        assert report.upgradeable is True

    def test_ownership_that_can_be_taken_back_is_not_renounced(self):
        assert self.honeypot().admin_renounced is False

    def test_burned_lp_is_burned_and_unlocked_lp_is_evidence(self):
        assert self.clean().lp_burned is True
        assert self.honeypot().lp_burned is False

    def test_lp_locked_with_no_stated_expiry_stays_unknown(self):
        """check_liquidity_lock asks for 30 days. Neither API reports an expiry.

        Reading a locker balance as a 30-day lock would be inventing the one number
        the filter is actually about.
        """
        report = parse_goplus_evm(DATA["goplus_evm_locked_no_expiry"], "bnb")[LOCKED_EVM]
        assert report.lp_burned is None
        assert report.lp_locked_pct == pytest.approx(97.0)

    def test_concentration_excludes_pools_and_exchanges(self):
        """A Uniswap pool and a Binance omnibus are not concentrated ownership."""
        assert self.clean().top10_ex_lp_pct == pytest.approx(7.4)

    def test_risk_labels_are_collected_even_where_no_filter_maps_to_them(self):
        labels = self.honeypot().risk_labels
        assert "hidden_owner" in labels
        assert "is_blacklisted" in labels

    def test_the_creator_address_is_carried(self):
        assert self.clean().deployer_address.startswith("0xdeployer")

    @pytest.mark.parametrize("payload", DATA["malformed_responses"])
    def test_malformed_responses_produce_nothing_rather_than_an_exception(self, payload):
        parsed = parse_goplus_evm(payload, "bnb")
        assert all(isinstance(r, SafetyReport) for r in parsed.values())

    def test_an_absent_field_is_unknown_not_safe(self):
        """The single most important property in this module."""
        report = parse_goplus_evm({"result": {"0xz": {}}}, "bnb")["0xz"]
        assert report.honeypot is None
        assert report.mint_revoked is None
        assert report.freeze_active is None
        assert report.lp_burned is None
        assert report.top10_ex_lp_pct is None
        assert report.to_filter_fields() == {}


# ---------------------------------------------------------------------------
# Solana parsing
# ---------------------------------------------------------------------------


class TestGoPlusSolana:
    def report(self) -> SafetyReport:
        return parse_goplus_solana(DATA["goplus_solana"])[SOL_MINT]

    def test_authority_status_maps_to_the_filters_question(self):
        report = self.report()
        assert report.mint_revoked is True
        assert report.freeze_active is True

    def test_an_incinerated_lp_is_burned(self):
        assert self.report().lp_burned is True

    def test_the_raydium_pool_is_not_counted_as_a_holder(self):
        assert self.report().top10_ex_lp_pct == pytest.approx(10.0)

    def test_deployer_history_is_unknown_on_solana_not_clean(self):
        """GoPlus address security covers EVM only, and unknown must stay unknown."""
        report = self.report()
        assert report.deployer_address is not None
        assert report.deployer_prior_rugs is None

    def test_a_clean_mint_is_measured_sellable_not_left_unknown(self):
        """The gap the first live run exposed.

        GoPlus has no is_honeypot off EVM, and treating that as unknown made
        check_sellability unanswerable for every Solana token -- 12 of the 13 real
        tokens in the first collector run. The question is answerable from the SPL
        mechanics that actually block a sale.
        """
        report = self.report()
        assert report.honeypot is False
        assert report.sells_failing is False

    def test_an_absent_transfer_fee_extension_is_a_measured_zero(self):
        """`transfer_fee: {}` on a real report means the extension is off."""
        report = self.report()
        assert report.buy_tax_pct == 0.0
        assert report.sell_tax_pct == 0.0

    def test_a_transfer_hook_is_treated_as_a_honeypot(self):
        """An unaudited program that can refuse your sell is what the filter is for.

        Some legitimate Token-2022 tokens are excluded by this. That is the safe
        direction of the error, and the same reasoning that maps EVM
        transfer_pausable onto freeze_active.
        """
        report = parse_goplus_solana(DATA["goplus_solana_hooked"])[
            "HookMint1111111111111111111111111111111111"
        ]
        assert report.honeypot is True

    def test_a_non_transferable_mint_cannot_be_sold(self):
        report = parse_goplus_solana(DATA["goplus_solana_non_transferable"])[
            "FrozenMint11111111111111111111111111111111"
        ]
        assert report.honeypot is True
        assert report.sells_failing is True

    def test_a_transfer_fee_is_read_as_a_tax_on_both_sides(self):
        report = parse_goplus_solana(DATA["goplus_solana_taxed"])[
            "TaxMint1111111111111111111111111111111111"
        ]
        assert report.buy_tax_pct == pytest.approx(9.5)
        assert report.sell_tax_pct == pytest.approx(9.5)

    def test_an_envelope_covering_nothing_stays_unknown(self):
        """The guard that matters most: absence of evidence is not a pass.

        A report that answered none of the structural questions must not have its
        silence read as "no blockers found".
        """
        report = parse_goplus_solana(DATA["goplus_solana_empty_report"])[
            "BareMint111111111111111111111111111111111"
        ]
        assert report.honeypot is None
        assert report.sells_failing is None
        assert report.buy_tax_pct is None
        assert report.to_filter_fields() == {}

    def test_proxy_risk_is_not_asserted_on_solana(self):
        """The chain registry answers that; a second answer here could disagree."""
        assert self.report().upgradeable is None


# ---------------------------------------------------------------------------
# RugCheck
# ---------------------------------------------------------------------------


class TestRugCheck:
    def test_a_null_authority_means_revoked_and_a_present_one_means_live(self):
        report = parse_rugcheck(DATA["rugcheck_report"], SOL_MINT)
        assert report.mint_revoked is True
        assert report.freeze_active is True

    def test_the_least_locked_market_is_the_one_reported(self):
        """A token is only as locked as its thinnest escape route."""
        report = parse_rugcheck(DATA["rugcheck_report"], SOL_MINT)
        assert report.lp_locked_pct == pytest.approx(84.2)

    def test_a_rugged_flag_is_carried_even_though_no_filter_reads_it(self):
        report = parse_rugcheck(DATA["rugcheck_rugged"], "RugMint")
        assert report.rugged is True
        assert report.mint_revoked is False

    @pytest.mark.parametrize("payload", [None, {}, [], "nope"])
    def test_an_empty_report_is_no_report(self, payload):
        assert parse_rugcheck(payload, "x") is None


# ---------------------------------------------------------------------------
# Deployer history
# ---------------------------------------------------------------------------


class TestAddressSecurity:
    def test_a_checked_clean_wallet_is_a_real_zero(self):
        """"Checked and clean" is what lets check_deployer_history pass at all."""
        count, labels = parse_goplus_address_security(DATA["goplus_address_clean"], "0xa")
        assert count == 0
        assert labels == ()

    def test_a_flagged_wallet_counts_its_flags(self):
        count, labels = parse_goplus_address_security(DATA["goplus_address_flagged"], "0xa")
        assert count >= 1
        assert "honeypot_related_address" in labels

    def test_an_empty_answer_is_not_a_clean_answer(self):
        count, _ = parse_goplus_address_security(DATA["goplus_address_empty"], "0xa")
        assert count is None

    @pytest.mark.parametrize("payload", [None, {}, [], {"result": "nope"}])
    def test_malformed_answers_leave_it_unknown(self, payload):
        assert parse_goplus_address_security(payload, "0xa")[0] is None


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


class TestMerge:
    def base(self, **fields) -> SafetyReport:
        return SafetyReport(
            chain="solana", contract="A", source="goplus", collected_at_ms=1, **fields
        )

    def test_an_unknown_never_overwrites_a_measurement(self):
        measured = self.base(mint_revoked=True, freeze_active=False)
        silent = replace(self.base(), source="rugcheck")
        merged = merge(measured, silent)
        assert merged.mint_revoked is True
        assert merged.freeze_active is False

    def test_the_unsafe_answer_wins_a_disagreement(self):
        """Sources disagree because one is stale. Picking the reassuring one is how
        a screen quietly stops screening."""
        optimistic = self.base(mint_revoked=True, freeze_active=False, honeypot=False)
        pessimistic = replace(
            self.base(mint_revoked=False, freeze_active=True, honeypot=True),
            source="rugcheck",
        )
        merged = merge(optimistic, pessimistic)
        assert merged.mint_revoked is False
        assert merged.freeze_active is True
        assert merged.honeypot is True

    def test_numeric_disagreements_take_the_pessimistic_reading(self):
        a = self.base(sell_tax_pct=1.0, top10_ex_lp_pct=10.0, lp_locked_pct=100.0)
        b = replace(
            self.base(sell_tax_pct=9.0, top10_ex_lp_pct=40.0, lp_locked_pct=12.0),
            source="rugcheck",
        )
        merged = merge(a, b)
        assert merged.sell_tax_pct == 9.0
        assert merged.top10_ex_lp_pct == 40.0
        assert merged.lp_locked_pct == 12.0

    def test_both_sources_are_named_on_the_result(self):
        merged = merge(self.base(), replace(self.base(), source="rugcheck"))
        assert merged.source == "goplus+rugcheck"

    def test_merging_with_nothing_is_a_no_op(self):
        report = self.base(honeypot=False)
        assert merge(report, None) is report
        assert merge(None, report) is report


# ---------------------------------------------------------------------------
# What the filters do with it -- the point of the whole module
# ---------------------------------------------------------------------------


def candidate(chain="bnb", contract=CLEAN_EVM, **market):
    base = {
        "chain": chain,
        "contract": contract,
        "market_cap_usd": 800_000.0,
        "fdv_usd": 900_000.0,
        "liquidity_usd": 120_000.0,
        "authorities": {},
        "holders": {},
        "deployer": {},
    }
    base.update(market)
    return base


class TestFiltersActuallyAnswerNow:
    def test_a_clean_report_clears_every_filter(self):
        """Before this module existed, no row could ever reach this state."""
        report = replace(
            parse_goplus_evm(DATA["goplus_evm_clean"], "bnb")[CLEAN_EVM],
            deployer_prior_rugs=0,
        )
        verdict = apply(
            filter_input_from_candidate(candidate(), **report.to_filter_fields())
        )
        assert verdict.rejected_by == []
        assert verdict.indeterminate_on == []
        assert verdict.passed is True

    def test_a_honeypot_is_rejected_on_evidence(self):
        report = replace(
            parse_goplus_evm(DATA["goplus_evm_honeypot"], "bnb")[HONEYPOT_EVM],
            deployer_prior_rugs=2,
        )
        verdict = apply(
            filter_input_from_candidate(
                candidate(contract=HONEYPOT_EVM), **report.to_filter_fields()
            )
        )
        assert "sellability" in verdict.rejected_by
        assert "mint_authority" in verdict.rejected_by
        assert "freeze_authority" in verdict.rejected_by
        assert "deployer_history" in verdict.rejected_by
        assert verdict.indeterminate_on == []

    def test_a_locked_but_undated_lp_leaves_exactly_that_filter_unmeasured(self):
        report = replace(
            parse_goplus_evm(DATA["goplus_evm_locked_no_expiry"], "bnb")[LOCKED_EVM],
            deployer_prior_rugs=0,
        )
        verdict = apply(
            filter_input_from_candidate(
                candidate(contract=LOCKED_EVM), **report.to_filter_fields()
            )
        )
        assert verdict.indeterminate_on == ["liquidity_lock"]
        assert verdict.rejected_by == []

    def test_a_real_solana_token_now_clears_sellability(self):
        """End to end on the chain that is most of the sample.

        Before this, sellability was unanswerable on Solana and every Solana row
        was excluded as unmeasured no matter how clean it was.
        """
        report = parse_goplus_solana(DATA["goplus_solana"])[SOL_MINT]
        verdict = apply(
            filter_input_from_candidate(
                candidate(chain="solana", contract=SOL_MINT), **report.to_filter_fields()
            )
        )
        assert "sellability" not in verdict.indeterminate_on
        assert "sellability" not in verdict.rejected_by

    def test_a_taxed_solana_token_is_rejected_on_evidence(self):
        report = parse_goplus_solana(DATA["goplus_solana_taxed"])[
            "TaxMint1111111111111111111111111111111111"
        ]
        verdict = apply(
            filter_input_from_candidate(
                candidate(chain="solana", contract="TaxMint"), **report.to_filter_fields()
            )
        )
        assert "sellability" in verdict.rejected_by

    def test_no_report_leaves_the_row_exactly_as_it_was_before(self):
        verdict = apply(filter_input_from_candidate(candidate()))
        assert set(verdict.indeterminate_on) >= {
            "sellability",
            "mint_authority",
            "freeze_authority",
            "liquidity_lock",
            "concentration",
            "deployer_history",
        }


# ---------------------------------------------------------------------------
# The source, end to end
# ---------------------------------------------------------------------------


class TestSafetySource:
    def routes(self):
        return {
            "/api/v1/token_security/56": DATA["goplus_evm_clean"],
            "/api/v1/solana/token_security": DATA["goplus_solana"],
            "/api/v1/address_security/": DATA["goplus_address_clean"],
            "/v1/tokens/": DATA["rugcheck_report"],
        }

    def source(self, calls=None, **kwargs):
        return SafetySource(
            goplus=goplus(self.routes(), calls),
            rugcheck=rugcheck(self.routes()),
            **kwargs,
        )

    def test_it_returns_a_report_per_token_it_could_answer(self):
        found = self.source().fetch([("bnb", CLEAN_EVM), ("solana", SOL_MINT)])
        assert set(found) == {("bnb", CLEAN_EVM), ("solana", SOL_MINT)}

    def test_the_deployer_lookup_makes_the_history_filter_answerable(self):
        found = self.source().fetch([("bnb", CLEAN_EVM)])
        assert found[("bnb", CLEAN_EVM)].deployer_prior_rugs == 0

    def test_one_deployer_behind_many_tokens_is_looked_up_once(self):
        """An uncapped per-token sweep would spend the rate limit re-asking."""
        calls: list[str] = []
        payload = {
            "result": {
                f"0xtok{i}": {"creator_address": "0xsamedeployer", "is_honeypot": "0"}
                for i in range(5)
            }
        }
        routes = {**self.routes(), "/api/v1/token_security/56": payload}
        source = SafetySource(
            goplus=goplus(routes, calls), rugcheck=rugcheck(routes), use_rugcheck=False
        )
        source.fetch([("bnb", f"0xtok{i}") for i in range(5)])
        assert sum(1 for c in calls if "address_security" in c) == 1

    def test_rugcheck_is_merged_into_the_solana_report(self):
        found = self.source().fetch([("solana", SOL_MINT)])
        report = found[("solana", SOL_MINT)]
        assert "rugcheck" in report.source
        assert report.lp_locked_pct == pytest.approx(84.2)

    def test_a_chain_with_no_safety_source_is_left_unmeasured(self):
        """Robinhood Chain today. Unknown excludes, so this is the safe direction."""
        found = self.source().fetch([("robinhood", "0xrh")])
        assert found == {}

    def test_a_failing_lookup_loses_that_chain_and_no_more(self):
        routes = {
            **self.routes(),
            "/api/v1/token_security/56": urllib.error.HTTPError("u", 500, "x", {}, None),
        }
        source = SafetySource(goplus=goplus(routes), rugcheck=rugcheck(routes))
        found = source.fetch([("bnb", CLEAN_EVM), ("solana", SOL_MINT)])
        assert ("bnb", CLEAN_EVM) not in found
        assert ("solana", SOL_MINT) in found

    def test_disabling_the_deployer_lookup_leaves_the_history_unknown(self):
        found = self.source(deployer_limit=0).fetch([("bnb", CLEAN_EVM)])
        assert found[("bnb", CLEAN_EVM)].deployer_prior_rugs is None


class TestStorage:
    def test_a_safety_row_is_appended_and_never_replaces_an_earlier_one(self):
        """"Live when we looked, revoked an hour later" is a real sequence of events."""
        with Store() as store:
            first = SafetyReport(
                chain="bnb",
                contract="0xA",
                source="goplus",
                collected_at_ms=now_ms(),
                mint_revoked=False,
            )
            later = replace(
                first,
                collected_at_ms=first.collected_at_ms + 3_600_000,
                mint_revoked=True,
            )
            store.append_safety_observations([first])
            store.append_safety_observations([later])
            assert store.safety_observation_count() == 2

    def test_the_latest_row_is_the_one_read_back(self):
        with Store() as store:
            from collectors.schema import Market, Snapshot

            snapshot = Snapshot(
                chain="bnb",
                contract="0xA",
                trigger="mcap_250k",
                source="test",
                market=Market(mcap_usd=300_000.0),
            )
            store.append_snapshot(snapshot)
            ids = {("bnb", "0xA"): snapshot.snapshot_id}
            base = SafetyReport(
                chain="bnb",
                contract="0xA",
                source="goplus",
                collected_at_ms=1_000,
                honeypot=False,
            )
            store.append_safety_observations([base], ids)
            store.append_safety_observations(
                [replace(base, collected_at_ms=2_000, honeypot=True)], ids
            )
            assert store.latest_safety(snapshot.snapshot_id)["honeypot"] is True
            assert store.snapshots_with_safety() == 1


def test_a_clean_verdict_requires_evidence_from_every_filter():
    """Guard the guard: if `apply` ever started passing on unknowns, tests above
    that assert a clean pass would keep passing for the wrong reason."""
    verdict = apply(filter_input_from_candidate(candidate()))
    assert verdict.passed is False
    assert all(
        r.outcome is not Outcome.PASS
        for r in verdict.results
        if r.name in ("sellability", "mint_authority")
    )
