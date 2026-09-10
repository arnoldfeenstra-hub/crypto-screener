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


class TestChainDiscovery:
    """The probe that finds an id, so binding one is not guesswork either."""

    def test_it_reports_every_chain_id_seen_and_whether_it_is_bound(self):
        from collectors.dexscreener import discover_chain_ids

        class StubClient:
            def token_boosts_top(self):
                return [
                    {"chainId": "solana", "tokenAddress": "A"},
                    {"chainId": "robinhood-chain", "tokenAddress": "B"},
                ]

            def token_boosts_latest(self):
                return [{"chainId": "robinhood-chain", "tokenAddress": "C"}]

            def token_profiles(self):
                return []

        found = discover_chain_ids(StubClient())
        assert found["solana"]["bound_to_a_source"] is True
        assert found["robinhood-chain"]["tokens_seen"] == 2
        assert found["robinhood-chain"]["canonical_name"] == "robinhood"
        assert found["robinhood-chain"]["bound_to_a_source"] is False

    def test_a_failing_endpoint_does_not_lose_the_others(self):
        from collectors.dexscreener import DexScreenerError, discover_chain_ids

        class StubClient:
            def token_boosts_top(self):
                raise DexScreenerError("down")

            def token_boosts_latest(self):
                return [{"chainId": "base", "tokenAddress": "A"}]

            def token_profiles(self):
                return []

        assert "base" in discover_chain_ids(StubClient())


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
        assert "uncalibrated priors" in payload["headline_warning"]

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

        .github/workflows/deploy.yml runs the same check before deploying.
        """
        import importlib.util
        import sys

        class Blocker:
            def find_module(self, name, path=None):
                if name.split(".")[0] in {"duckdb", "requests", "numpy", "pandas"}:
                    raise ImportError(f"api/screener.py imports {name}")
                return None

        blocker = Blocker()
        sys.meta_path.insert(0, blocker)
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

    def test_a_workflow_deploys_what_github_receives(self):
        """"Push to GitHub, deploy to Vercel" is a file in the repo, not a memory."""
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        )
        # PyYAML reads the bare `on:` key as the boolean True.
        triggers = workflow.get("on", workflow.get(True))
        assert "push" in triggers
        assert "workflow_dispatch" in triggers
        # The collector commits with GITHUB_TOKEN, and such a push never starts
        # another workflow, so without a schedule the exported dataset would never
        # reach the deployed page.
        assert "schedule" in triggers

    def test_the_deploy_excludes_the_dependency_manifest(self):
        """The function is stdlib-only; an install step could only add failure modes."""
        ignored = (REPO_ROOT / ".vercelignore").read_text(encoding="utf-8").split("\n")
        assert "pyproject.toml" in ignored
        assert "api/" not in ignored
