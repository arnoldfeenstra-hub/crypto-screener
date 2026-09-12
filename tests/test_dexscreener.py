"""DexScreener source tests.

The split in ``collectors/dexscreener.py`` decides what these can prove. The
parsers are pure and total, so they are tested exhaustively here against
``fixtures/dexscreener_responses.json``. The endpoint paths and the response
shapes are the unverified half -- that fixture was written to the documented
schema, not recorded off the wire -- so the tests below are built to fail
*informatively* if a shape turns out to differ, rather than to pretend the shape
is confirmed.

No test here opens a socket. The client is exercised through a stub opener.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from collectors import chains
from collectors.dexscreener import (
    MAX_ADDRESSES_PER_REQUEST,
    DexScreenerClient,
    DexScreenerError,
    DexScreenerFeed,
    DexScreenerPriceSource,
    _Throttle,
    declared_socials,
    parse_discovery,
    parse_pairs,
    to_metrics,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "dexscreener_responses.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))

ALPHA = ("solana", "AAA1111111111111111111111111111111111111111")
BETA = ("bnb", "0xBBB0000000000000000000000000000000000002")
GAMMA = ("base", "0xCCC0000000000000000000000000000000000003")


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub_opener(routes: dict[str, object], calls: list[str] | None = None):
    """An opener that answers by URL substring and records what was asked for."""

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


def client_with(routes, calls=None) -> DexScreenerClient:
    # sleep is stubbed out: a retry test that actually waits is a slow test.
    return DexScreenerClient(
        opener=stub_opener(routes, calls),
        throttle=_Throttle(sleep=lambda _s: None, clock=lambda: 0.0),
        sleep=lambda _s: None,
    )


# ---------------------------------------------------------------------------
# Parsers: total, and honest about what they did not see
# ---------------------------------------------------------------------------


class TestParsePairs:
    def test_a_tokens_pools_are_aggregated_into_one_record(self):
        by_token = parse_pairs(DATA["pairs_response"])
        alpha = by_token[ALPHA]
        assert alpha.ticker == "ALPHA"
        assert alpha.pair_count == 2

    def test_additive_facts_are_summed_and_valuations_are_not(self):
        """Two pools hold two lots of liquidity, but one market cap reported twice.

        Summing market cap would multiply a token's valuation by its number of
        pools, which is the kind of error that looks like a great find.
        """
        alpha = parse_pairs(DATA["pairs_response"])[ALPHA]
        assert alpha.liquidity_usd == pytest.approx(128000.25 + 31000.0)
        assert alpha.volume_24h_usd == pytest.approx(1850000.5 + 210000.0)
        assert alpha.txns_24h == 2400 + 1900 + 300 + 250
        # From the deepest pool, not the sum and not the mean.
        assert alpha.mcap_usd == 806000
        assert alpha.fdv_usd == 924000
        assert alpha.price_usd == pytest.approx(0.0008241)

    def test_age_comes_from_the_earliest_pool(self):
        """A second pool is a later listing, not a later launch."""
        alpha = parse_pairs(DATA["pairs_response"])[ALPHA]
        assert alpha.first_pair_created_at_ms == 1788905000000

    def test_numbers_sent_as_strings_are_parsed(self):
        alpha = parse_pairs(DATA["pairs_response"])[ALPHA]
        assert isinstance(alpha.price_usd, float)

    def test_the_chain_id_is_normalised_to_the_registry_name(self):
        """DexScreener says `bsc`; the column says `bnb`. One name, or the filter lies."""
        by_token = parse_pairs(DATA["pairs_response"])
        assert BETA in by_token
        assert not any(key[0] == "bsc" for key in by_token)

    def test_a_pair_with_no_base_address_is_skipped_not_guessed(self):
        by_token = parse_pairs(DATA["pairs_response"])
        assert not any(agg.ticker == "NOPE" for agg in by_token.values())

    def test_a_missing_info_block_leaves_socials_unknown_not_false(self):
        """The socials triple is the best-evidenced feature in the schema.

        A guessed False on a token whose profile was simply not read would corrupt
        exactly the column most likely to carry signal (17.4x graduation lift).
        """
        gamma = parse_pairs(DATA["pairs_response"])[GAMMA]
        assert gamma.declared_telegram is None
        assert gamma.declared_x is None
        assert gamma.declared_website is None

    def test_a_profile_on_any_pool_answers_for_the_token(self):
        alpha = parse_pairs(DATA["pairs_response"])[ALPHA]
        assert alpha.declared_telegram is True
        assert alpha.declared_x is True
        assert alpha.declared_website is True

    def test_a_token_with_one_profiled_pool_reports_what_that_profile_said(self):
        beta = parse_pairs(DATA["pairs_response"])[BETA]
        assert beta.declared_x is True
        assert beta.declared_telegram is False  # the info block was read and had none

    @pytest.mark.parametrize("payload", DATA["malformed_responses"])
    def test_malformed_responses_produce_no_rows_rather_than_an_exception(self, payload):
        assert parse_pairs(payload) == {}

    def test_a_non_positive_market_cap_is_missing_not_small(self):
        """Zero is what a broken response looks like, and it is a denominator."""
        payload = {
            "pairs": [
                {
                    "chainId": "solana",
                    "baseToken": {"address": "X", "symbol": "X"},
                    "marketCap": 0,
                    "fdv": -5,
                    "liquidity": {"usd": 100.0},
                }
            ]
        }
        aggregate = parse_pairs(payload)[("solana", "X")]
        assert aggregate.mcap_usd is None
        assert aggregate.fdv_usd is None


class TestParseDiscovery:
    def test_boosts_carry_their_spend(self):
        entries = parse_discovery(DATA["token_boosts_top"], kind="boost_top")
        alpha = next(e for e in entries if e["contract"].startswith("AAA"))
        assert alpha["boost_amount"] == 500
        assert alpha["boost_total"] == 1500

    def test_a_measured_zero_boost_is_zero_not_missing(self):
        """`totalAmount: 0` is a fact; an absent key is not. They must not merge."""
        entries = parse_discovery(DATA["token_boosts_top"], kind="boost_top")
        beta = next(e for e in entries if e["chain"] == "bnb")
        assert beta["boost_total"] == 0
        assert beta["boost_total"] is not None

    def test_an_entry_with_no_link_list_leaves_the_triple_unknown(self):
        entries = parse_discovery(DATA["token_profiles_latest"], kind="profile")
        gamma = next(e for e in entries if e["chain"] == "base")
        assert gamma["declared_telegram"] is None
        assert gamma["declared_x"] is None

    def test_an_unknown_chain_is_kept_under_its_own_name(self):
        """A token on a chain the registry has not met is still a real token."""
        entries = parse_discovery(DATA["token_boosts_top"], kind="boost_top")
        assert any(e["chain"] == "hyperliquid" for e in entries)

    def test_an_entry_with_no_address_is_dropped(self):
        entries = parse_discovery(DATA["token_boosts_top"], kind="boost_top")
        assert all(e["contract"] for e in entries)
        assert len(entries) == 3

    @pytest.mark.parametrize("payload", [None, {}, "nope", [None, 1, "x"]])
    def test_malformed_discovery_payloads_produce_nothing(self, payload):
        assert parse_discovery(payload, kind="boost_top") == []


def test_declared_socials_reads_both_websites_and_socials():
    pair = {
        "info": {
            "websites": [{"url": "https://site.invalid"}],
            "socials": [{"type": "telegram", "url": "https://t.me/x"}],
        }
    }
    assert declared_socials(pair) == (True, False, True)


# ---------------------------------------------------------------------------
# Mapping onto the repo's source-neutral record
# ---------------------------------------------------------------------------


class TestToMetrics:
    def test_fields_dexscreener_cannot_answer_stay_none(self):
        """Holders, authorities and deployer are unknown here, and must read as unknown.

        This is what keeps data_completeness an honest description of the row, and
        what keeps the holders half of the trigger from ever firing off this source.
        """
        metrics = to_metrics(parse_pairs(DATA["pairs_response"])[ALPHA])
        assert metrics.holder_count is None
        assert metrics.mint_revoked is None
        assert metrics.freeze_active is None
        assert metrics.deployer_address is None
        assert metrics.bundled_supply_pct is None

    def test_discovery_fills_gaps_but_never_overwrites_a_measurement(self):
        aggregate = parse_pairs(DATA["pairs_response"])[GAMMA]
        metrics = to_metrics(
            aggregate,
            discovery={"boost_total": 42.0, "declared_telegram": True, "discovery": "profile"},
        )
        assert metrics.boost_total == 42.0
        assert metrics.declared_telegram is True  # the pool profile had nothing to say

        alpha = parse_pairs(DATA["pairs_response"])[ALPHA]
        overridden = to_metrics(alpha, discovery={"declared_website": False})
        assert overridden.declared_website is True  # the pool profile wins

    def test_age_is_computed_from_the_earliest_pool(self):
        aggregate = parse_pairs(DATA["pairs_response"])[ALPHA]
        metrics = to_metrics(aggregate, observed_at_ms=1788905000000 + 3_600_000)
        assert metrics.age_minutes() == 60


# ---------------------------------------------------------------------------
# Client behaviour
# ---------------------------------------------------------------------------


class TestClient:
    def test_a_retryable_status_is_retried_and_a_client_error_is_not(self):
        attempts: list[str] = []

        def flaky(request, timeout=None):
            attempts.append(request.full_url)
            if len(attempts) < 3:
                raise urllib.error.HTTPError(request.full_url, 429, "slow down", {}, None)
            return _Response([])

        client = DexScreenerClient(
            opener=flaky,
            throttle=_Throttle(sleep=lambda _s: None, clock=lambda: 0.0),
            sleep=lambda _s: None,
        )
        assert client.token_boosts_top() == []
        assert len(attempts) == 3

    def test_a_404_is_not_retried(self):
        attempts: list[str] = []

        def gone(request, timeout=None):
            attempts.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 404, "gone", {}, None)

        client = DexScreenerClient(
            opener=gone,
            throttle=_Throttle(sleep=lambda _s: None, clock=lambda: 0.0),
            sleep=lambda _s: None,
        )
        with pytest.raises(DexScreenerError):
            client.token_boosts_top()
        assert len(attempts) == 1

    def test_addresses_are_batched_at_the_documented_ceiling(self):
        calls: list[str] = []
        client = client_with({"/latest/dex/tokens/": {"pairs": []}}, calls)
        client.pairs_for_tokens([f"A{i}" for i in range(50)])
        assert len(calls) == 1
        assert calls[0].count("%2C") + calls[0].count(",") == MAX_ADDRESSES_PER_REQUEST - 1

    def test_the_throttle_spaces_requests_by_the_documented_limit(self):
        slept: list[float] = []
        now = [0.0]
        throttle = _Throttle(sleep=slept.append, clock=lambda: now[0])
        throttle.wait("token-boosts")
        throttle.wait("token-boosts")
        assert slept and slept[0] == pytest.approx(1.0)  # 60/min -> one second apart


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------


class TestFeed:
    def routes(self):
        return {
            "/token-boosts/top/v1": DATA["token_boosts_top"],
            "/token-boosts/latest/v1": [],
            "/token-profiles/latest/v1": DATA["token_profiles_latest"],
            "/latest/dex/tokens/": DATA["pairs_response"],
        }

    def test_it_returns_one_record_per_discovered_token_with_market_data(self):
        feed = DexScreenerFeed.for_chains(
            ["solana", "bnb", "base"], client=client_with(self.routes())
        )
        found = {(m.chain, m.contract) for m in feed.poll()}
        assert found == {ALPHA, BETA, GAMMA}

    def test_chains_not_asked_for_are_left_out(self):
        feed = DexScreenerFeed.for_chains(["solana"], client=client_with(self.routes()))
        assert {m.chain for m in feed.poll()} == {"solana"}

    def test_a_failing_discovery_endpoint_does_not_lose_the_others(self):
        """A short poll delays a snapshot. A crashed poll skips one."""
        routes = self.routes()
        routes["/token-boosts/top/v1"] = urllib.error.HTTPError(
            "u", 500, "boom", {}, None
        )
        feed = DexScreenerFeed.for_chains(
            ["solana", "base"], client=client_with(routes)
        )
        # Alpha and Gamma both appear in the profiles list, so they survive.
        assert {m.contract for m in feed.poll()} >= {GAMMA[1]}

    def test_a_discovered_token_with_no_pool_yet_is_simply_absent(self):
        routes = self.routes()
        routes["/latest/dex/tokens/"] = {"pairs": []}
        feed = DexScreenerFeed.for_chains(["solana"], client=client_with(routes))
        assert feed.poll() == []

    def test_the_same_token_in_two_discovery_lists_is_merged_once(self):
        feed = DexScreenerFeed.for_chains(["solana"], client=client_with(self.routes()))
        polled = feed.poll()
        assert len([m for m in polled if m.contract == ALPHA[1]]) == 1
        assert polled[0].boost_total == 1500

    def test_an_unsourced_chain_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="robinhood"):
            DexScreenerFeed.for_chains(["robinhood"])

    def test_the_poll_cap_does_not_prefer_loud_tokens(self):
        """Capping by boost size would bias the sample on the axis mindshare measures."""
        feed = DexScreenerFeed.for_chains(
            ["solana", "bnb", "base"],
            client=client_with(self.routes()),
            max_tokens_per_poll=2,
        )
        polled = feed.poll()
        assert len(polled) <= 2
        # Deterministic: the same cap twice gives the same tokens.
        again = DexScreenerFeed.for_chains(
            ["solana", "bnb", "base"],
            client=client_with(self.routes()),
            max_tokens_per_poll=2,
        ).poll()
        assert [m.contract for m in polled] == [m.contract for m in again]


class TestPriceSource:
    def test_a_token_the_source_cannot_find_is_absent_not_zero(self):
        """A missing pool is an unknown price. A zero would manufacture a total loss."""
        source = DexScreenerPriceSource(client_with({"/latest/dex/tokens/": {"pairs": []}}))
        assert source.fetch([ALPHA]) == {}

    def test_it_returns_repriceable_metrics_for_what_it_found(self):
        source = DexScreenerPriceSource(
            client_with({"/latest/dex/tokens/": DATA["pairs_response"]})
        )
        found = source.fetch([ALPHA, BETA])
        assert set(found) == {ALPHA, BETA}
        assert found[ALPHA].mcap_usd == 806000

    def test_a_batch_failure_loses_that_batch_and_no_more(self):
        source = DexScreenerPriceSource(
            client_with(
                {"/latest/dex/tokens/": urllib.error.HTTPError("u", 500, "x", {}, None)}
            )
        )
        assert source.fetch([ALPHA]) == {}


def test_every_chain_in_the_fixture_resolves_through_the_registry():
    """Guard the guard: a fixture full of chains the registry drops proves nothing."""
    ids = {
        entry["chainId"]
        for entry in DATA["token_boosts_top"] + DATA["token_profiles_latest"]
        if "chainId" in entry
    }
    assert all(chains.canonical(chain_id) for chain_id in ids)
