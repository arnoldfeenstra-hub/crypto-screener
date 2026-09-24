"""Chain identity, multi-chain collection, and the live endpoint.

Three things are checked here that only started mattering once the watcher could
hold more than one chain at a time:

1. **One name per chain.** DexScreener says ``bsc``, BUILD_BRIEF.md section 4 says
   ``bnb``. If both reach the ``chain`` column, every ``GROUP BY`` and every filter
   silently splits one chain into two.
2. **The trigger still cannot see the chain.** Pooling Solana and BNB into one
   sample is only defensible if they entered on identical terms.
3. **The live endpoint and the collector agree.** ``api/screener.py`` shares the
   trigger rule, the section 4 mapping, the filters and the pillar maths rather
   than reimplementing them, and the test below checks that sharing by identity.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from collectors import chains
from collectors.metrics import TokenMetrics
from collectors.safety import SafetyReport
from collectors.store import Store
from collectors.trigger_watcher import TriggerWatcher, evaluate
from filters.hard_filters import FilterInput, Outcome, check_proxy_risk
from scoring.pillars import score_candidate

REPO_ROOT = Path(__file__).resolve().parent.parent
TS = 1788912000000


def _load_api():
    spec = importlib.util.spec_from_file_location(
        "screener_api", REPO_ROOT / "api" / "screener.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


API = _load_api()


def token(chain: str, contract: str, **fields) -> TokenMetrics:
    base = dict(chain=chain, contract=contract, observed_at_ms=TS, source="dexscreener")
    base.update(fields)
    return TokenMetrics(**base)


# ---------------------------------------------------------------------------
# One name per chain
# ---------------------------------------------------------------------------


class TestChainRegistry:
    @pytest.mark.parametrize(
        ("spelling", "expected"),
        [
            ("bsc", "bnb"),
            ("BSC", "bnb"),
            ("Binance Smart Chain", "bnb"),
            ("bnb", "bnb"),
            ("solana", "solana"),
            ("SOL", "solana"),
            ("  Ethereum  ", "ethereum"),
            ("matic", "polygon"),
        ],
    )
    def test_every_spelling_of_a_chain_resolves_to_one_name(self, spelling, expected):
        assert chains.canonical(spelling) == expected

    def test_a_missing_chain_stays_missing(self):
        """Not defaulted to solana just because most rows are solana."""
        assert chains.canonical(None) is None
        assert chains.canonical("   ") is None

    def test_an_unknown_chain_is_kept_rather_than_dropped_or_renamed(self):
        assert chains.canonical("Some New Chain") == "some-new-chain"
        entry = chains.get("Some New Chain")
        assert entry is not None
        assert entry.known is False

    def test_evm_is_unknown_for_an_unknown_chain(self):
        """The conservative direction: no free pass through the proxy filter."""
        assert chains.is_evm("solana") is False
        assert chains.is_evm("bnb") is True
        assert chains.is_evm("some-new-chain") is None

    def test_a_chain_with_no_source_is_refused_by_name(self):
        with pytest.raises(ValueError, match="robinhood"):
            chains.resolve_requested(["solana", "robinhood"])

    def test_resolution_deduplicates_and_canonicalises(self):
        assert chains.resolve_requested(["bsc", "bnb", "SOL"]) == ["bnb", "solana"]

    def test_the_brief_s_named_targets_are_all_registered(self):
        """BUILD_BRIEF.md section 4 names three; all three must exist as names."""
        for name in ("solana", "bnb", "robinhood"):
            assert chains.get(name) is not None


class TestChainIdOverride:
    """Robinhood Chain, and every chain whose id this repo does not yet know.

    BUILD_BRIEF.md section 4 names Robinhood Chain as a target. It is an Arbitrum
    Orbit rollup, so `evm=True` is a property of the chain and safe to assert;
    its DexScreener `chainId` is not, because nobody here has seen DexScreener
    return one. Hardcoding a guess would produce the worst available outcome -- a
    request that matches nothing, indistinguishable from a quiet chain. So the id
    is configuration, and these tests check that path works end to end.
    """

    def teardown_method(self):
        chains.reload_overrides("")

    def test_binding_an_id_switches_a_registered_chain_on(self):
        chains.reload_overrides("robinhood=robinhood-chain")
        assert chains.dexscreener_id("robinhood") == "robinhood-chain"
        assert chains.resolve_requested(["robinhood"]) == ["robinhood"]
        assert "robinhood" in chains.supported_names()

    def test_a_bound_chain_normalises_back_from_its_dexscreener_id(self):
        """The whole point of the registry: one name in the `chain` column."""
        chains.reload_overrides("robinhood=robinhood-chain")
        assert chains.from_dexscreener("robinhood-chain") == "robinhood"

    def test_robinhood_is_evm_whether_or_not_an_id_is_bound(self):
        assert chains.is_evm("robinhood") is True
        chains.reload_overrides("robinhood=robinhood-chain")
        assert chains.is_evm("robinhood") is True

    def test_an_unbound_chain_still_fails_by_name_with_the_remedy(self):
        chains.reload_overrides("")
        with pytest.raises(ValueError) as exc:
            chains.resolve_requested(["robinhood"])
        assert "SCREENER_CHAIN_IDS" in str(exc.value)
        assert "discover-chains" in str(exc.value)

    def test_a_name_the_registry_has_never_seen_can_also_be_bound(self):
        chains.reload_overrides("newchain=some-dex-id")
        assert chains.resolve_requested(["newchain"]) == ["newchain"]

    def test_a_malformed_override_is_skipped_rather_than_guessed_at(self):
        chains.reload_overrides("robinhood,=nothing,  , solana=")
        assert chains.dexscreener_id("robinhood") is None
        assert chains.dexscreener_id("solana") == "solana"

    def test_the_override_does_not_disturb_the_chains_already_bound(self):
        chains.reload_overrides("robinhood=robinhood-chain")
        assert chains.dexscreener_id("bnb") == "bsc"
        assert chains.canonical("bsc") == "bnb"


class StubDiscovery:
    """A DexScreener client for the discovery path, with both sources stubbed."""

    def __init__(self, boosts_top=None, boosts_latest=None, profiles=None, search=None):
        self._boosts_top = boosts_top if boosts_top is not None else []
        self._boosts_latest = boosts_latest if boosts_latest is not None else []
        self._profiles = profiles if profiles is not None else []
        self._search = search if search is not None else {}
        self.queries_asked: list[str] = []

    def token_boosts_top(self):
        return self._raise_or(self._boosts_top)

    def token_boosts_latest(self):
        return self._raise_or(self._boosts_latest)

    def token_profiles(self):
        return self._raise_or(self._profiles)

    def search(self, query):
        self.queries_asked.append(query)
        return self._raise_or(self._search.get(query, []))

    @staticmethod
    def _raise_or(value):
        if isinstance(value, Exception):
            raise value
        return value


class TestChainDiscovery:
    """The probe that finds an id, so binding one is not guesswork either."""

    def test_it_reports_every_chain_id_seen_and_whether_it_is_bound(self):
        from collectors.dexscreener import discover_chain_ids

        client = StubDiscovery(
            boosts_top=[
                {"chainId": "solana", "tokenAddress": "A"},
                {"chainId": "robinhood-chain", "tokenAddress": "B"},
            ],
            boosts_latest=[{"chainId": "robinhood-chain", "tokenAddress": "C"}],
        )
        found = discover_chain_ids(client, queries=())
        assert found["solana"]["bound_to_a_source"] is True
        assert found["robinhood-chain"]["tokens_seen"] == 2
        assert found["robinhood-chain"]["canonical_name"] == "robinhood"
        assert found["robinhood-chain"]["bound_to_a_source"] is False

    def test_a_failing_endpoint_does_not_lose_the_others(self):
        from collectors.dexscreener import DexScreenerError, discover_chain_ids

        client = StubDiscovery(
            boosts_top=DexScreenerError("down"),
            boosts_latest=[{"chainId": "base", "tokenAddress": "A"}],
        )
        assert "base" in discover_chain_ids(client, queries=())

    def test_search_surfaces_a_chain_the_boost_endpoints_never_see(self):
        """The reason the search sweep exists.

        Boosts and profiles only return chains with a token currently boosted or
        profiled -- a small, paid-for sample. A chain can be live, trading, and
        entirely absent from it, which is exactly the position Robinhood Chain is
        in here.
        """
        from collectors.dexscreener import discover_chain_ids

        client = StubDiscovery(
            boosts_top=[{"chainId": "solana", "tokenAddress": "A"}],
            search={"USDC": [{"chainId": "somenewrollup", "baseToken": {"symbol": "X"}}]},
        )
        found = discover_chain_ids(client, queries=("USDC",))
        assert "somenewrollup" in found
        assert found["somenewrollup"]["seen_via"] == ["search"]
        assert found["solana"]["seen_via"] == ["discovery"]

    def test_a_failing_search_does_not_lose_the_discovery_endpoints(self):
        from collectors.dexscreener import DexScreenerError, discover_chain_ids

        client = StubDiscovery(
            boosts_top=[{"chainId": "base", "tokenAddress": "A"}],
            search={"USDC": DexScreenerError("down")},
        )
        assert "base" in discover_chain_ids(client, queries=("USDC",))


class TestVerifyChainId:
    """Binding a wrong id is the worst failure available: silence that looks quiet."""

    def test_a_real_id_comes_back_with_the_pools_that_prove_it(self):
        from collectors.dexscreener import verify_chain_id

        client = StubDiscovery(
            search={
                "USDC": [
                    {
                        "chainId": "somerollup",
                        "dexId": "someswap",
                        "baseToken": {"symbol": "$AAA", "address": "0xAAA"},
                        "liquidity": {"usd": 41000.0},
                    },
                    {"chainId": "solana", "baseToken": {"symbol": "$B"}},
                ]
            }
        )
        result = verify_chain_id(client, "somerollup", queries=("USDC",))
        assert result["pairs"] == 1
        assert result["sample"][0]["ticker"] == "$AAA"
        assert result["sample"][0]["liquidity_usd"] == 41000.0
        assert "Bind it with" in result["conclusion"]

    def test_finding_nothing_is_not_reported_as_proof_the_id_is_wrong(self):
        from collectors.dexscreener import verify_chain_id

        client = StubDiscovery(search={"USDC": [{"chainId": "solana"}]})
        result = verify_chain_id(client, "notachain", queries=("USDC",))
        assert result["pairs"] == 0
        assert "not proof the id is wrong" in result["conclusion"]

    def test_a_search_failure_is_reported_rather_than_read_as_absence(self):
        from collectors.dexscreener import DexScreenerError, verify_chain_id

        client = StubDiscovery(search={"USDC": DexScreenerError("429")})
        result = verify_chain_id(client, "somerollup", queries=("USDC",))
        assert result["query_errors"] == ["USDC: 429"]
        assert result["pairs"] == 0

    def test_it_says_when_the_candidate_is_the_id_already_bound(self):
        from collectors.dexscreener import verify_chain_id

        client = StubDiscovery(search={"USDC": [{"chainId": "bsc", "baseToken": {}}]})
        result = verify_chain_id(client, "bsc", queries=("USDC",))
        assert result["canonical_name"] == "bnb"
        assert result["already_bound"] is True


class TestBoundChainsAreCollectedByDefault:
    """Binding an id is the whole decision; there is no second switch to forget."""

    def test_an_unbound_chain_is_not_in_the_defaults(self, monkeypatch):
        monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
        chains.reload_overrides()
        assert "robinhood" not in chains.default_chain_names()

    def test_binding_robinhood_puts_it_in_the_default_run(self, monkeypatch):
        monkeypatch.setenv(chains.CHAIN_ID_ENV, "robinhood=rhchain")
        chains.reload_overrides()
        try:
            assert chains.default_chain_names()[-1] == "robinhood"
            assert chains.resolve_requested(["robinhood"]) == ["robinhood"]
            assert chains.dexscreener_id("robinhood") == "rhchain"
        finally:
            monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
            chains.reload_overrides()

    def test_the_compiled_in_defaults_still_come_first_and_in_order(self, monkeypatch):
        monkeypatch.setenv(chains.CHAIN_ID_ENV, "robinhood=rhchain")
        chains.reload_overrides()
        try:
            names = chains.default_chain_names()
            assert names[: len(chains.DEFAULT_CHAINS)] == list(chains.DEFAULT_CHAINS)
        finally:
            monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
            chains.reload_overrides()

    def test_robinhood_is_evm_even_while_unbound(self, monkeypatch):
        # A property of the chain (an Arbitrum Orbit rollup), not of whether
        # anybody has told this repo its DexScreener id.
        monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
        chains.reload_overrides()
        assert chains.is_evm("robinhood") is True


class TestProxyFilterFollowsTheChain:
    def test_non_evm_chains_pass_without_an_upgradeable_flag(self):
        for chain in ("solana", "sui", "ton"):
            result = check_proxy_risk(FilterInput(chain=chain))
            assert result.outcome is Outcome.PASS, chain

    def test_evm_chains_must_answer(self):
        for chain in ("bnb", "base", "ethereum"):
            result = check_proxy_risk(FilterInput(chain=chain))
            assert result.outcome is Outcome.UNKNOWN, chain

    def test_an_unknown_chain_does_not_inherit_solanas_free_pass(self):
        result = check_proxy_risk(FilterInput(chain="some-new-chain"))
        assert result.outcome is Outcome.UNKNOWN

    def test_an_upgradeable_contract_with_an_unrenounced_admin_is_rejected(self):
        result = check_proxy_risk(
            FilterInput(chain="bnb", upgradeable=True, admin_renounced=False)
        )
        assert result.outcome is Outcome.REJECT


# ---------------------------------------------------------------------------
# The trigger still cannot see the chain
# ---------------------------------------------------------------------------


class TestMultiChainCollection:
    def test_the_same_numbers_fire_identically_on_every_chain(self):
        decisions = {
            chain: evaluate(260_000.0, None)
            for chain in ("solana", "bnb", "base", "ethereum", "some-new-chain")
        }
        assert len(set(decisions.values())) == 1

    def test_one_watcher_collects_several_chains_into_one_table(self):
        with Store() as store:
            watcher = TriggerWatcher(store, source="dexscreener")
            watcher.process(
                [
                    token("solana", "A", mcap_usd=300_000.0, volume_24h_usd=10.0),
                    token("bnb", "0xB", mcap_usd=400_000.0, volume_24h_usd=30.0),
                    token("base", "0xC", mcap_usd=90_000.0, volume_24h_usd=5.0),
                ]
            )
            assert store.chain_breakdown() == {"bnb": 1, "solana": 1}

    def test_the_same_address_on_two_chains_is_two_tokens(self):
        """An address collision across chains must not be deduplicated into one row."""
        with Store() as store:
            watcher = TriggerWatcher(store, source="dexscreener")
            watcher.process(
                [
                    token("bnb", "0xSAME", mcap_usd=300_000.0),
                    token("base", "0xSAME", mcap_usd=300_000.0),
                ]
            )
            assert store.snapshot_count() == 2

    def test_mindshare_is_measured_over_the_whole_poll_not_the_survivors(self):
        """A share of the tokens that fired would be a share of a filtered set.

        The token below that never crosses the trigger still belongs in the
        denominator: it is attention that existed and was competing.
        """
        with Store() as store:
            watcher = TriggerWatcher(store, source="dexscreener")
            watcher.process(
                [
                    token("solana", "A", mcap_usd=300_000.0, txns_24h=50),
                    token("solana", "B", mcap_usd=10_000.0, txns_24h=50),
                ]
            )
            row = store.fetch_by_contract("solana", "A")
            assert row["mindshare_universe_size"] == 2
            assert row["mindshare_share_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# The live endpoint
# ---------------------------------------------------------------------------


class StubFeed:
    def __init__(self, tokens):
        self._tokens = tokens

    def poll(self):
        return self._tokens


def live_tokens():
    return [
        token(
            "solana",
            "A",
            ticker="$ALPHA",
            mcap_usd=800_000.0,
            fdv_usd=900_000.0,
            liquidity_usd=120_000.0,
            volume_24h_usd=2_000_000.0,
            txns_24h=4000,
            boost_total=500.0,
            pair_count=3,
            first_seen_at_ms=TS - 5_400_000,
            declared_telegram=True,
            declared_x=True,
            declared_website=True,
            listings=["dex"],
        ),
        token(
            "bnb",
            "0xB",
            ticker="$BETA",
            mcap_usd=310_000.0,
            fdv_usd=310_000.0,
            liquidity_usd=40_000.0,
            volume_24h_usd=300_000.0,
            txns_24h=600,
            pair_count=1,
            first_seen_at_ms=TS - 86_400_000,
            listings=["dex"],
        ),
        token("base", "0xC", ticker="$GAMMA", mcap_usd=40_000.0, txns_24h=30),
    ]


class StubSafety:
    """A safety source that answers from a dict, with no network."""

    def __init__(self, reports=None, error=None):
        self.reports = reports or {}
        self.error = error

    def fetch(self, tokens):
        if self.error:
            raise RuntimeError(self.error)
        return {key: self.reports[key] for key in tokens if key in self.reports}


def clean_report(chain: str, contract: str) -> SafetyReport:
    """A token that passes every check a safety source can answer."""
    return SafetyReport(
        chain=chain,
        contract=contract,
        source="goplus",
        collected_at_ms=TS,
        honeypot=False,
        sells_failing=False,
        buy_tax_pct=0.0,
        sell_tax_pct=0.0,
        mint_revoked=True,
        freeze_active=False,
        lp_burned=True,
        top10_ex_lp_pct=12.0,
        upgradeable=False,
        admin_renounced=True,
        holder_count=1400,
        deployer_address="0xcreator",
        deployer_prior_rugs=0,
    )


class TestLivePayload:
    def payload(self, **kwargs):
        # with_safety defaults off here so the common case needs no stub; the
        # safety-specific tests below pass one explicitly.
        kwargs.setdefault("with_safety", False)
        return API.build_live_payload(
            ["solana", "bnb", "base"], feed=StubFeed(live_tokens()), **kwargs
        )

    def test_only_tokens_at_or_above_the_trigger_are_listed(self):
        """A table mixing lifecycle points is the comparison the cohort design forbids."""
        payload = self.payload()
        assert payload["live"]["polled"] == 3
        assert payload["live"]["triggered"] == 2
        assert payload["live"]["below_trigger"] == 1
        assert {t["ticker"] for t in payload["tokens"]} == {"$ALPHA", "$BETA"}

    def test_mindshare_is_computed_over_everything_polled(self):
        payload = self.payload()
        alpha = next(t for t in payload["tokens"] if t["ticker"] == "$ALPHA")
        assert alpha["mindshare"]["universe_size"] == 3

    def test_it_says_plainly_that_it_is_not_the_dataset(self):
        payload = self.payload()
        assert payload["mode"] == "live"
        assert "not the collected dataset" in payload["live_notice"]
        assert payload["all_rows_synthetic"] is False
        assert payload["synthetic_notice"] is None

    def test_the_sampling_bias_of_the_universe_is_stated(self):
        assert "bias" in self.payload()["live"]["discovery"]

    def test_paper_mode_and_the_uncalibrated_warning_travel_with_it(self):
        payload = self.payload()
        assert payload["paper_mode"] is True
        assert payload["weights_are_calibrated"] is False
        assert payload["weights_fit"]["weights_version"] == payload["weights_version"]
        assert "too small to establish an edge" in payload["headline_warning"]
        assert "nothing on this page is a prediction" in payload["headline_warning"]

    def test_every_row_carries_its_chain_and_a_display_label(self):
        payload = self.payload()
        by_ticker = {t["ticker"]: t for t in payload["tokens"]}
        assert by_ticker["$BETA"]["chain"] == "bnb"
        assert by_ticker["$BETA"]["chain_label"] == "BNB Chain"

    def test_the_chain_list_covers_what_was_asked_for_including_empties(self):
        """A chain that returned nothing is reported at zero, not omitted.

        Omitting it would make "no tokens on Base" indistinguishable from "Base was
        never polled".
        """
        counts = {c["name"]: c["count"] for c in self.payload()["chains"]}
        assert counts == {"solana": 1, "bnb": 1, "base": 0}

    def test_scores_are_null_while_safety_checks_are_unmeasured(self):
        """Unknown never passes a hard filter, so a live row is excluded, not scored."""
        payload = self.payload()
        alpha = next(t for t in payload["tokens"] if t["ticker"] == "$ALPHA")
        assert alpha["excluded"] is True
        assert alpha["score"] is None
        assert alpha["rejected_by"] == []
        assert "mint_authority" in alpha["indeterminate_on"]

    def test_the_pillar_composite_is_reported_even_though_the_row_is_excluded(self):
        """It is the arithmetic under the score, not a substitute for it."""
        alpha = next(t for t in self.payload()["tokens"] if t["ticker"] == "$ALPHA")
        assert alpha["pillar_composite"] is not None
        assert alpha["score"] is None

    def test_fdv_lets_the_liquidity_depth_filter_answer(self):
        alpha = next(t for t in self.payload()["tokens"] if t["ticker"] == "$ALPHA")
        assert alpha["fdv_usd"] == 900_000.0
        assert "liquidity_depth" not in alpha["indeterminate_on"]

    def test_the_ranking_matches_the_collectors_own_pillar_maths(self):
        """Shared code, checked by re-deriving one row through the library path."""
        payload = self.payload()
        alpha = next(t for t in payload["tokens"] if t["ticker"] == "$ALPHA")
        from collectors.snapshot import build_snapshot
        from scoring.candidate import candidate_from_row

        metrics = live_tokens()[0]
        from collectors import mindshare as mindshare_mod

        shares = mindshare_mod.compute(live_tokens())
        snapshot = build_snapshot(
            metrics,
            evaluate(metrics.mcap_usd, metrics.holder_count),
            source="dexscreener",
            mindshare=shares[("solana", "A")],
        )
        expected = score_candidate(candidate_from_row(snapshot.to_row()))
        assert alpha["pillar_composite"] == pytest.approx(expected.score)

    def test_the_versions_stamped_on_a_live_row_match_the_stored_ones(self):
        from scoring.runner import WEIGHTS_VERSION, prompt_version

        payload = self.payload()
        assert payload["prompt_version"] == prompt_version()
        assert payload["weights_version"] == WEIGHTS_VERSION

    def test_an_unsupported_chain_is_an_error_not_an_empty_list(self):
        with pytest.raises(ValueError, match="robinhood"):
            API.build_live_payload(["robinhood"], feed=StubFeed([]))

    def test_an_empty_poll_produces_an_empty_but_well_formed_payload(self):
        payload = API.build_live_payload(["solana"], feed=StubFeed([]))
        assert payload["tokens"] == []
        assert payload["live"]["polled"] == 0

    def test_the_limit_is_honoured_and_capped(self):
        payload = self.payload(limit=1)
        assert len(payload["tokens"]) == 1

    def test_query_parsing_defaults_and_clamps(self):
        assert API._parse_query("") == (
            list(chains.DEFAULT_CHAINS),
            API.DEFAULT_LIMIT,
            True,
        )
        assert API._parse_query("chains=solana,bsc&limit=5")[0] == ["solana", "bsc"]
        assert API._parse_query("limit=99999")[1] == API.MAX_LIMIT
        assert API._parse_query("limit=nonsense")[1] == API.DEFAULT_LIMIT
        assert API._parse_query("safety=0")[2] is False
        assert API._parse_query("safety=1")[2] is True


class TestLiveSafety:
    """The live view's verdicts have to come from evidence, or say they do not."""

    def with_safety(self, reports=None, error=None):
        return API.build_live_payload(
            ["solana", "bnb", "base"],
            feed=StubFeed(live_tokens()),
            safety_source=StubSafety(reports, error),
            with_safety=True,
        )

    def test_a_clean_report_lets_a_token_actually_score(self):
        """The whole point: with safety answered, a row is judged instead of skipped."""
        payload = self.with_safety({("solana", "A"): clean_report("solana", "A")})
        alpha = next(t for t in payload["tokens"] if t["ticker"] == "$ALPHA")
        assert alpha["excluded"] is False
        assert alpha["score"] is not None
        assert alpha["indeterminate_on"] == []
        assert alpha["rank"] == 1

    def test_a_honeypot_is_rejected_on_evidence_not_excluded_as_unknown(self):
        report = replace(clean_report("solana", "A"), honeypot=True)
        payload = self.with_safety({("solana", "A"): report})
        alpha = next(t for t in payload["tokens"] if t["ticker"] == "$ALPHA")
        assert alpha["rejected_by"] == ["sellability"]
        assert alpha["score"] is None

    def test_a_token_with_no_report_stays_unmeasured(self):
        """Partial coverage must not leak into a pass for the tokens not covered."""
        payload = self.with_safety({("solana", "A"): clean_report("solana", "A")})
        beta = next(t for t in payload["tokens"] if t["ticker"] == "$BETA")
        assert beta["excluded"] is True
        assert beta["safety"] is None
        assert "mint_authority" in beta["indeterminate_on"]

    def test_the_safety_evidence_travels_with_the_row(self):
        """Hard rule 6: a verdict whose inputs are gone cannot be back-tested."""
        payload = self.with_safety({("solana", "A"): clean_report("solana", "A")})
        alpha = next(t for t in payload["tokens"] if t["ticker"] == "$ALPHA")
        assert alpha["safety"]["source"] == "goplus"
        assert alpha["safety"]["mint_revoked"] is True
        assert alpha["safety"]["holder_count"] == 1400

    def test_a_failing_safety_lookup_degrades_toward_fewer_scores(self):
        """A view must not die on a side lookup, and must not pass rows instead."""
        payload = self.with_safety(error="goplus is down")
        assert payload["live"]["safety_error"] is not None
        assert payload["live"]["safety_measured"] == 0
        assert all(t["excluded"] for t in payload["tokens"])
        assert all(t["score"] is None for t in payload["tokens"])

    def test_safety_is_only_looked_up_for_tokens_that_cleared_the_trigger(self):
        asked: list[tuple[str, str]] = []

        class Recording(StubSafety):
            def fetch(self, tokens):
                asked.extend(tokens)
                return {}

        API.build_live_payload(
            ["solana", "bnb", "base"],
            feed=StubFeed(live_tokens()),
            safety_source=Recording(),
            with_safety=True,
        )
        assert ("base", "0xC") not in asked  # below the trigger
        assert ("solana", "A") in asked


class TestSharedLogicIsActuallyShared:
    """A second implementation of any of these would let the two rankings disagree."""

    def test_the_endpoint_uses_the_collectors_trigger_rule_object(self):
        from collectors.trigger_rule import evaluate as rule

        assert API.evaluate is rule

    def test_the_endpoint_uses_the_collectors_snapshot_mapping(self):
        from collectors.snapshot import build_snapshot

        assert API.build_snapshot is build_snapshot

    def test_the_endpoint_uses_the_same_filters_and_pillars(self):
        from filters.hard_filters import apply
        from scoring.candidate import candidate_from_row
        from scoring.pillars import score_candidate as pillars_score

        assert API.apply_filters is apply
        assert API.candidate_from_row is candidate_from_row
        assert API.score_candidate is pillars_score

    def test_the_runner_still_exports_the_names_that_moved(self):
        """The split modules are re-exported, so no existing import site broke."""
        from scoring import runner
        from scoring.candidate import candidate_from_row
        from scoring.pillars import WEIGHTS_VERSION
        from scoring.prompt_meta import prompt_version

        assert runner.candidate_from_row is candidate_from_row
        assert runner.prompt_version is prompt_version
        assert runner.WEIGHTS_VERSION is WEIGHTS_VERSION


class TestTheDeployedPage:
    """The page has to work with the live endpoint missing, and say so when it is."""

    def page(self) -> str:
        return (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_it_asks_for_the_live_endpoint_and_the_static_export(self):
        page = self.page()
        assert "/api/screener" in page
        assert "screener-data.json" in page

    def test_it_falls_back_rather_than_failing_when_the_endpoint_is_missing(self):
        assert "No live endpoint on this deployment." in self.page()

    def test_it_offers_a_chain_filter_and_a_mindshare_column(self):
        page = self.page()
        assert 'id="chain-filter"' in page
        assert "mindshareCell" in page

    def test_the_phase_0_warning_is_still_on_the_page(self):
        assert "Phase 0 — collection only. Not a signal." in self.page()

    def test_vercel_config_ships_the_function_and_the_modules_it_imports(self):
        config = json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))
        include = config["functions"]["api/screener.py"]["includeFiles"]
        for package in ("collectors", "filters", "scoring", "prompts"):
            assert package in include

    def test_the_function_imports_on_stdlib_alone(self):
        """The one deploy failure that builds fine and then 500s on every request.

        api/screener.py runs on Vercel with no requirements.txt and nothing
        installed. Importing duckdb or requests is easy to do by accident -- the
        collector uses both, and half this module's imports come from the same
        packages -- and would not fail here, because both are installed locally.
        So the import is re-run with them blocked.

        Nothing deploys from this repo any more -- Vercel's Git integration
        owns that -- so this suite is the only thing standing between an
        accidental import and a function that 500s on every request.
        """
        import importlib

        class Blocked(ImportError):
            """Distinct from ModuleNotFoundError on purpose -- see below."""

        class Blocker:
            # find_spec, not find_module. Python 3.12 removed the find_module
            # fallback, so a finder defining only find_module is skipped in
            # silence, and this test passed for nothing on the 3.12 both
            # workflows pin.
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {"duckdb", "requests", "numpy", "pandas"}:
                    raise Blocked(f"api/screener.py imports {name}")
                return None

        blocker = Blocker()
        sys.meta_path.insert(0, blocker)
        # Prove the blocker blocks before trusting what it lets through. It has to
        # be a Blocked rather than any ImportError: a plain ImportError is what an
        # absent package raises too, so catching that would pass on a runner where
        # duckdb simply is not installed. Popping the cache matters for the same
        # reason -- import_module returns a cached module without ever consulting
        # meta_path, which is exactly how this check first came back green.
        cached = sys.modules.pop("duckdb", None)
        try:
            with pytest.raises(Blocked):
                importlib.import_module("duckdb")
        finally:
            if cached is not None:
                sys.modules["duckdb"] = cached

        try:
            spec = importlib.util.spec_from_file_location(
                "screener_api_isolated", REPO_ROOT / "api" / "screener.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.meta_path.remove(blocker)

        assert hasattr(module, "handler"), "no `handler` class for the Vercel runtime"
        assert hasattr(module, "build_live_payload")

    def test_nothing_in_the_repo_deploys_any_more(self):
        """Vercel's Git integration is connected and owns deploys. A second
        deployer in the repo would mean every push deploys twice -- the trade-off
        the old deploy.yml header described, and the reason it is gone."""
        import yaml

        directory = REPO_ROOT / ".github" / "workflows"
        assert "deploy.yml" not in {p.name for p in directory.glob("*.yml")}

        # Only what the steps actually RUN counts. Prose about Vercel deploying on
        # push is exactly what these comments should say; a `vercel deploy` in a
        # run block is the thing that would deploy twice.
        commands = []
        for path in directory.glob("*.yml"):
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in (workflow.get("jobs") or {}).values():
                commands += [str(step.get("run", "")) for step in job.get("steps", [])]
        ran = " ".join(commands).lower()
        assert "vercel deploy" not in ran
        assert "vercel@" not in ran

    def test_something_still_asks_whether_the_deployment_answers(self):
        """The Git integration reports whether the BUILD succeeded, which is a
        different question. api/screener.py runs with nothing installed, so an
        accidental `import duckdb` builds green and 500s on every request -- and
        Vercel deploys on push whether or not CI is green, so the test suite
        catching it first is not the same as nobody shipping it."""
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "health.yml").read_text(encoding="utf-8")
        )
        triggers = workflow.get("on", workflow.get(True))
        assert "schedule" in triggers
        body = " ".join(
            str(step.get("run", "")) for step in workflow["jobs"]["check"]["steps"]
        )
        assert "/api/screener" in body
        # Unset URL must be a notice, not a red cross on a repository nobody has
        # pointed at a deployment yet.
        assert "SCREENER_URL" in body

    def test_something_notices_when_the_collector_stops_writing(self):
        """collect.yml failing is not the same as anyone noticing. Its push was
        refused on a file-size limit from 2026-09-20 and every hourly run for two
        days collected, failed and lost its rows. health.yml now fails when the
        manifest every successful run rewrites goes stale."""
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "health.yml").read_text(encoding="utf-8")
        )
        job = workflow["jobs"]["collector"]
        body = " ".join(str(step.get("run", "")) for step in job["steps"])
        assert "state/manifest.json" in body
        assert "updated_at" in body
        assert "exit(1)" in body
        hours = float(job["steps"][0]["env"]["MAX_AGE_HOURS"])
        # Tighter than the six-hour schedule is pointless, looser than a few missed
        # runs is a day of data.
        assert 1 < hours <= 6

    def test_a_workflow_runs_the_tests_on_every_push(self):
        """The suite is only a check if something runs it without being asked.

        Neither other workflow fails on a broken test: collect.yml gathers data
        and health.yml only checks. Without this one a push that broke scoring would
        go green, be committed on top of by the hourly collector, and ship.
        """
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        triggers = workflow.get("on", workflow.get(True))
        assert "push" in triggers
        assert "pull_request" in triggers

        steps = workflow["jobs"]["test"]["steps"]
        commands = " ".join(str(step.get("run", "")) for step in steps)
        assert "ruff check" in commands
        assert "pytest" in commands

    def test_the_deploy_excludes_the_dependency_manifest(self):
        """The function is stdlib-only; an install step could only add failure modes."""
        ignored = (REPO_ROOT / ".vercelignore").read_text(encoding="utf-8").split("\n")
        assert "pyproject.toml" in ignored
        assert "api/" not in ignored


class TestTheCollectorCanActuallyStart:
    """The failure this class exists for: the hourly collector died on

        File "collect.py", line 45, in <module>
          from collectors.social_tg import TelegramCollector, TelegramPreviewClient
        File "collectors/social_tg.py", line 40, in <module>
          import requests
        ModuleNotFoundError: No module named 'requests'

    Nothing could have caught it. The suite runs with every package installed, so
    an import that is missing on the runner passes here. ci.yml is no help for the
    same reason. And collect.yml installed a hand-copied subset of the project's
    dependencies, so the workflow installed what someone remembered rather than
    what the code imports.
    """

    def _third_party_imports(self, entry: Path) -> set[str]:
        """Third-party packages that must exist for ``entry`` to start.

        Two deliberate choices:

        * It follows the repo's own modules rather than reading the entrypoint's
          import list, because the import that broke the collector was three
          files deep -- collect.py -> social_tg -> requests.
        * Only imports at module scope count. An import inside a function or
          behind a try/except runs when that path runs, and scoring/runner.py
          has exactly one: `anthropic`, loaded lazily for the optional narration
          feature and guarded. Requiring that as a hard dependency would make
          every collector run install an SDK it never calls.
        """
        import ast

        local = {p.stem for p in REPO_ROOT.glob("*.py")} | {
            d.name for d in REPO_ROOT.iterdir() if (d / "__init__.py").exists()
        }
        seen_files: set[Path] = set()
        third_party: set[str] = set()
        queue = [entry]
        while queue:
            path = queue.pop()
            if path in seen_files or not path.exists():
                continue
            seen_files.add(path)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:  # module scope only -- see the docstring
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module] if node.level == 0 and node.module else []
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    if root in sys.stdlib_module_names:
                        continue
                    if root not in local:
                        third_party.add(root)
                        continue
                    # A repo module: follow it.
                    parts = name.split(".")
                    queue.append(REPO_ROOT.joinpath(*parts).with_suffix(".py"))
                    queue.append(REPO_ROOT.joinpath(*parts, "__init__.py"))
        return third_party

    def test_every_package_the_collector_imports_is_declared(self):
        import tomllib

        declared = {
            # "requests>=2.31" -> "requests"
            re.split(r"[<>=!\[ ]", dep)[0].strip().replace("-", "_").lower()
            for dep in tomllib.loads(
                (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]["dependencies"]
        }
        needed = {name.lower() for name in self._third_party_imports(REPO_ROOT / "collect.py")}
        missing = needed - declared
        assert not missing, (
            f"collect.py transitively imports {sorted(missing)}, which pyproject does "
            "not declare. The scheduled collector installs the declared dependencies "
            "and will die on startup with ModuleNotFoundError."
        )

    def test_the_collector_workflow_installs_what_the_project_declares(self):
        """A hand-copied package list drifts from the code silently, and the only
        symptom is a scheduled job that stops collecting."""
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        )
        installs = " ".join(
            str(step.get("run", "")) for step in workflow["jobs"]["collect"]["steps"]
        )
        assert "pip install" in installs
        assert "-e ." in installs, (
            "collect.yml installs named packages instead of the project. The list "
            "will drift from the imports again; it already did once."
        )

    def test_a_typed_chain_list_reaches_the_collector_as_one_argument(self):
        """Unquoted, a workflow_dispatch value like "solana, bnb" split at the space
        and argparse rejected the run. The array form keeps it one argument."""
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        )
        script = " ".join(
            str(step.get("run", "")) for step in workflow["jobs"]["collect"]["steps"]
        )
        assert 'CHAIN_ARG=(--chains "$CHAINS")' in script
        assert '"${CHAIN_ARG[@]}"' in script


# ---------------------------------------------------------------------------
# Finding a chain nobody has boosted a token on
# ---------------------------------------------------------------------------


ROBINHOOD_TOKEN = "0x1cDb289BeFDFaC8aF945a288BCdcCc382cB34d32"


def _quiet_chain_pairs(chain_id: str, address: str) -> list[dict]:
    return [
        {
            "chainId": chain_id,
            "baseToken": {"address": address, "symbol": "$RH", "name": "RH Token"},
            "priceUsd": "0.01",
            "marketCap": 900_000,
            "fdv": 1_000_000,
            "liquidity": {"usd": 90_000},
            "volume": {"h24": 400_000, "h6": 120_000, "h1": 30_000},
            "txns": {"h24": {"buys": 900, "sells": 700}, "h1": {"buys": 90, "sells": 40}},
            "priceChange": {"h1": 4.2, "h6": 18.0, "h24": 55.0},
            "pairCreatedAt": 1_789_300_000_000,
        }
    ]


class QuietChainClient:
    """DexScreener with a live pool nobody has boosted or profiled.

    This is the shape of the problem, not a contrivance: the discovery endpoints
    only ever return tokens somebody paid to boost or filled in a profile for, so
    a whole chain can trade all day and never appear in one.
    """

    def __init__(self, chain_id: str = "robinhoodchain", address: str = ROBINHOOD_TOKEN):
        self.chain_id = chain_id
        self.address = address
        self.asked: list[list[str]] = []

    def token_boosts_top(self):
        return [{"chainId": "solana", "tokenAddress": "SoL1"}]

    def token_boosts_latest(self):
        return []

    def token_profiles(self):
        return [{"chainId": "bsc", "tokenAddress": "0xbsc1"}]

    def pairs_for_tokens(self, addresses):
        self.asked.append(list(addresses))
        if self.address in addresses:
            return _quiet_chain_pairs(self.chain_id, self.address)
        return []


class TestWhyAChainProducesNoTokens:
    """Two independent reasons, and the second is the one that surprises."""

    def test_an_unbound_chain_cannot_even_be_asked_about(self, monkeypatch):
        monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
        chains.reload_overrides()
        with pytest.raises(ValueError, match="no DexScreener source"):
            chains.resolve_requested(["robinhood"])

    def test_binding_the_id_is_not_enough_on_a_chain_nobody_boosts(self, monkeypatch):
        """The failure that looks exactly like success.

        With the id bound, the chain resolves, the feed polls, and it collects
        nothing -- because discovery is a paid-for sample and this chain is not in
        it. Indistinguishable, in the counts afterwards, from a quiet chain.
        """
        from collectors.dexscreener import DexScreenerFeed

        monkeypatch.setenv(chains.CHAIN_ID_ENV, "robinhood=robinhoodchain")
        chains.reload_overrides()
        try:
            feed = DexScreenerFeed(
                client=QuietChainClient(), chain_names=("robinhood",)
            )
            assert feed.discover() == {}
            assert feed.poll() == []
        finally:
            monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
            chains.reload_overrides()


class TestResolveTokenChain:
    """A token address you already have is enough to learn a chain's id."""

    def test_it_reports_the_raw_chain_id_the_api_files_the_token_under(self):
        from collectors.dexscreener import resolve_token_chain

        result = resolve_token_chain(QuietChainClient(), ROBINHOOD_TOKEN)
        assert result["chain_ids"] == ["robinhoodchain"]
        assert result["not_bound"] == ["robinhoodchain"]
        assert "robinhoodchain" in result["conclusion"]

    def test_it_accepts_either_envelope_shape(self):
        from collectors.dexscreener import resolve_token_chain

        class DictEnvelope(QuietChainClient):
            def pairs_for_tokens(self, addresses):
                return {"pairs": _quiet_chain_pairs(self.chain_id, self.address)}

        assert resolve_token_chain(DictEnvelope(), ROBINHOOD_TOKEN)["chain_ids"] == [
            "robinhoodchain"
        ]

    def test_an_unindexed_address_is_not_reported_as_a_new_chain(self):
        from collectors.dexscreener import resolve_token_chain

        result = resolve_token_chain(QuietChainClient(), "0xnothing")
        assert result["chain_ids"] == []
        assert "Either the address is wrong" in result["conclusion"]

    def test_a_request_failure_is_reported_rather_than_read_as_absence(self):
        from collectors.dexscreener import DexScreenerError, resolve_token_chain

        class Broken(QuietChainClient):
            def pairs_for_tokens(self, addresses):
                raise DexScreenerError("429")

        result = resolve_token_chain(Broken(), ROBINHOOD_TOKEN)
        assert result["error"] == "429"
        assert result["chain_ids"] == []

    def test_an_already_bound_id_is_not_offered_for_binding(self):
        from collectors.dexscreener import resolve_token_chain

        result = resolve_token_chain(QuietChainClient(chain_id="bsc"), ROBINHOOD_TOKEN)
        assert result["not_bound"] == []
        assert "already bound" in result["conclusion"]


class TestSeededTokens:
    """The other half: collecting a token discovery will never surface."""

    def test_a_seeded_address_is_polled_and_its_chain_comes_from_the_response(
        self, monkeypatch
    ):
        from collectors.dexscreener import DexScreenerFeed

        monkeypatch.setenv(chains.CHAIN_ID_ENV, "robinhood=robinhoodchain")
        chains.reload_overrides()
        try:
            feed = DexScreenerFeed(
                client=QuietChainClient(),
                chain_names=("robinhood",),
                seed_contracts=(ROBINHOOD_TOKEN,),
            )
            polled = feed.poll()
            assert [m.chain for m in polled] == ["robinhood"]
            assert polled[0].mcap_usd == 900_000.0
            assert polled[0].entry_path == "seed"
        finally:
            monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
            chains.reload_overrides()

    def test_a_seeded_token_still_has_to_cross_the_trigger(self, monkeypatch):
        """Seeding says "look at this", never "include this".

        The trigger is the sample's entry rule and it is identical for every
        token; a seed that bypassed it would put a row into the dataset at a
        lifecycle point of its own.
        """
        from collectors.dexscreener import DexScreenerFeed

        class Small(QuietChainClient):
            def pairs_for_tokens(self, addresses):
                pairs = _quiet_chain_pairs(self.chain_id, self.address)
                pairs[0]["marketCap"] = 1_000  # far below $250k
                return pairs

        monkeypatch.setenv(chains.CHAIN_ID_ENV, "robinhood=robinhoodchain")
        chains.reload_overrides()
        try:
            feed = DexScreenerFeed(
                client=Small(), chain_names=("robinhood",), seed_contracts=(ROBINHOOD_TOKEN,)
            )
            polled = feed.poll()
            assert len(polled) == 1
            assert not evaluate(polled[0].mcap_usd, polled[0].holder_count).fired
        finally:
            monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
            chains.reload_overrides()

    def test_a_seed_on_a_chain_nobody_requested_is_dropped(self):
        """Seeding must not widen the run's chain set by a side effect, or the
        chain list stops describing what was actually polled."""
        from collectors.dexscreener import DexScreenerFeed

        feed = DexScreenerFeed(
            client=QuietChainClient(chain_id="bsc"),
            chain_names=("solana",),
            seed_contracts=(ROBINHOOD_TOKEN,),
        )
        assert feed.poll() == []

    def test_a_boosted_token_keeps_its_boost_figures_when_also_seeded(self):
        """A seed placeholder carries no boost data. Letting it win would erase a
        real mindshare input."""
        from collectors.dexscreener import DexScreenerFeed

        class Boosted(QuietChainClient):
            def token_boosts_top(self):
                return [
                    {
                        "chainId": "solana",
                        "tokenAddress": "SoL1",
                        "amount": 100,
                        "totalAmount": 500,
                    }
                ]

        feed = DexScreenerFeed(
            client=Boosted(), chain_names=("solana",), seed_contracts=("SoL1",)
        )
        entry = feed.discover()[("solana", "SoL1")]
        assert entry["discovery"] == "boost_top"
        assert entry["boost_total"] == 500

    def test_a_lowercase_evm_seed_finds_its_checksummed_pool(self, monkeypatch):
        """DexScreener answers an address in any case and returns it checksummed;
        a seed pasted from a URL or an explorer is often lowercase. Compared
        verbatim, it matched no pool and was dropped without a log line."""
        from collectors.dexscreener import DexScreenerFeed

        class CaseBlind(QuietChainClient):
            def pairs_for_tokens(self, addresses):
                if self.address.lower() in {a.lower() for a in addresses}:
                    return _quiet_chain_pairs(self.chain_id, self.address)
                return []

        monkeypatch.setenv(chains.CHAIN_ID_ENV, "robinhood=robinhoodchain")
        chains.reload_overrides()
        try:
            feed = DexScreenerFeed(
                client=CaseBlind(),
                chain_names=("robinhood",),
                seed_contracts=(ROBINHOOD_TOKEN.lower(),),
            )
            polled = feed.poll()
            # Stored under the API's spelling, so it matches every later cycle.
            assert [m.contract for m in polled] == [ROBINHOOD_TOKEN]
            assert polled[0].entry_path == "seed"
        finally:
            monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
            chains.reload_overrides()

    def test_a_seed_on_two_chains_resolves_to_the_requested_one(self, monkeypatch):
        """One EVM address can exist on several chains. Keeping only the last chain
        it came back on dropped a seed that was also on a chain the run asked for."""
        from collectors.dexscreener import DexScreenerFeed

        class TwoChains(QuietChainClient):
            def pairs_for_tokens(self, addresses):
                return _quiet_chain_pairs("robinhoodchain", self.address) + _quiet_chain_pairs(
                    "bsc", self.address
                )

        monkeypatch.setenv(chains.CHAIN_ID_ENV, "robinhood=robinhoodchain")
        chains.reload_overrides()
        try:
            feed = DexScreenerFeed(
                client=TwoChains(), chain_names=("robinhood",), seed_contracts=(ROBINHOOD_TOKEN,)
            )
            assert [m.chain for m in feed.poll()] == ["robinhood"]
        finally:
            monkeypatch.delenv(chains.CHAIN_ID_ENV, raising=False)
            chains.reload_overrides()

    def test_the_env_list_is_parsed_without_inventing_an_empty_address(self):
        from collectors.dexscreener import seed_contracts_from_env

        assert seed_contracts_from_env(" 0xA , ,0xB, 0xA ") == ("0xA", "0xB")
        assert seed_contracts_from_env("") == ()


class TestEntryPathIsRecorded:
    """How a row arrived is a confounder, so it is a column and not a caveat."""

    def test_the_snapshot_carries_how_the_token_was_found(self):
        from collectors.dexscreener import parse_pairs, to_metrics
        from collectors.snapshot import build_snapshot

        aggregate = next(iter(parse_pairs(_quiet_chain_pairs("solana", "Tok1")).values()))
        metrics = to_metrics(aggregate, discovery={"discovery": "seed"})
        snapshot = build_snapshot(metrics, evaluate(metrics.mcap_usd, None))
        assert snapshot.entry_path == "seed"
        assert snapshot.to_row()["entry_path"] == "seed"

    def test_it_does_not_count_toward_data_completeness(self):
        """Bookkeeping about the collector, not a measurement of the token. A row
        must not score better for having been collected."""
        from collectors.schema import Market, Snapshot

        base = Snapshot(
            chain="solana", contract="C", trigger="mcap_250k", source="t",
            market=Market(mcap_usd=300_000.0),
        )
        seeded = Snapshot(
            chain="solana", contract="C", trigger="mcap_250k", source="t",
            market=Market(mcap_usd=300_000.0), entry_path="seed",
        )
        assert base.completeness() == seeded.completeness()

    def test_a_row_written_before_it_was_recorded_is_null_not_a_guess(self):
        from collectors.schema import Snapshot

        assert Snapshot(
            chain="solana", contract="C", trigger="mcap_250k", source="t"
        ).entry_path is None
