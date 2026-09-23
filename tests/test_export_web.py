"""Web export tests.

The export feeds a public page, so the thing worth defending is that the page
cannot overstate what it is showing. Two claims have to survive every future edit:

* Rows replayed from a fixture are labelled as synthetic. The page is a screener
  UI, and a reader who sees tickers, market caps and scores will assume they are
  real unless told otherwise.
* The label is derived from the data, not set by hand -- so it appears without
  anyone remembering to switch it on, and disappears the moment real rows arrive
  without anyone remembering to switch it off.
"""

from __future__ import annotations

import json
from pathlib import Path

from collectors.schema import Market, Snapshot
from collectors.store import Store
from export_web import build_payload

REPO_ROOT = Path(__file__).resolve().parent.parent
T0 = 1788912000000


def store_with(*sources: str) -> Store:
    store = Store()
    for index, source in enumerate(sources):
        store.append_snapshot(
            Snapshot(
                chain="solana",
                contract=f"Tok{index}",
                trigger="mcap_250k",
                source=source,
                ticker=f"$T{index}",
                ts=T0,
                market=Market(mcap_usd=300_000.0, liquidity_usd=30_000.0),
            )
        )
    return store


class TestSyntheticDisclosure:
    def test_fixture_rows_are_declared_synthetic(self):
        with store_with("replay:solana_replay.json") as store:
            payload = build_payload(store)
        assert payload["all_rows_synthetic"] is True
        assert "not real tokens" in payload["synthetic_notice"].lower() or (
            "do not refer to real tokens" in payload["synthetic_notice"]
        )

    def test_live_rows_carry_no_such_notice(self):
        with store_with("bitquery") as store:
            payload = build_payload(store)
        assert payload["all_rows_synthetic"] is False
        assert payload["synthetic_notice"] is None

    def test_one_real_row_is_enough_to_drop_the_notice(self):
        """Mixed data is not synthetic data. The claim has to be true of everything."""
        with store_with("replay:solana_replay.json", "bitquery") as store:
            payload = build_payload(store)
        assert payload["all_rows_synthetic"] is False
        assert payload["synthetic_notice"] is None

    def test_the_backfill_source_counts_as_real(self):
        with store_with("bitquery_backfill") as store:
            payload = build_payload(store)
        assert payload["all_rows_synthetic"] is False

    def test_an_empty_dataset_makes_no_claim_either_way(self):
        with Store() as store:
            payload = build_payload(store)
        assert payload["all_rows_synthetic"] is False
        assert payload["tokens"] == []

    def test_the_sources_are_listed_so_the_claim_is_checkable(self):
        with store_with("replay:solana_replay.json") as store:
            payload = build_payload(store)
        assert payload["data_sources"] == ["replay:solana_replay.json"]


class TestPayloadShape:
    def test_the_headline_warning_survives_regardless_of_data(self):
        with store_with("bitquery") as store:
            payload = build_payload(store)
        assert "uncalibrated priors" in payload["headline_warning"]
        assert payload["paper_mode"] is True
        assert payload["weights_are_calibrated"] is False

    def test_progress_separates_evidence_from_ignorance(self):
        with store_with("bitquery") as store:
            payload = build_payload(store)
        progress = payload["progress"]
        assert "excluded_on_evidence" in progress
        assert "excluded_as_unmeasured" in progress
        assert progress["min_triggered_tokens"] == 300
        assert progress["min_dead_per_survivor"] == 20

    def test_the_published_base_rates_travel_with_the_page(self):
        """The ranking is meaningless without the number it has to beat beside it."""
        with store_with("bitquery") as store:
            payload = build_payload(store)
        rates = payload["published_base_rates"]
        assert rates["all_three_lift"] == 17.4
        assert rates["concordance_benchmark"] == 0.858

    def test_the_payload_is_json_serialisable(self):
        with store_with("replay:x") as store:
            payload = build_payload(store)
        assert json.dumps(payload, default=str)

    def test_no_credential_shaped_key_reaches_the_page(self):
        with store_with("bitquery") as store:
            payload = build_payload(store)
        blob = json.dumps(payload, default=str).lower()
        for word in ("bitquery_token", "bearer", "api_key", "x_bearer", "password"):
            assert word not in blob


class TestDeclaredLinks:
    """The page links to a token's community; the link comes from DexScreener,
    which is to say from whoever deployed the token."""

    def test_the_declared_addresses_reach_the_exported_dataset(self):
        with Store() as store:
            store.append_snapshot(
                Snapshot(
                    chain="solana",
                    contract="Tok1",
                    trigger="mcap_250k",
                    source="dexscreener",
                    ticker="$T1",
                    ts=T0,
                    market=Market(mcap_usd=300_000.0),
                    telegram_url="https://t.me/realgroup",
                    x_url="https://x.com/realacct",
                )
            )
            payload = build_payload(store)
        socials = payload["tokens"][0]["socials_declared"]
        assert socials["telegram_url"] == "https://t.me/realgroup"
        assert socials["x_url"] == "https://x.com/realacct"

    def test_a_row_with_no_declared_address_exports_null_not_a_guess(self):
        """Every snapshot taken before schema 5 is this row: the flag was kept and
        the address was dropped. Null is the truth; a link built from the ticker
        would point at a stranger's channel."""
        with store_with("dexscreener") as store:
            payload = build_payload(store)
        socials = payload["tokens"][0]["socials_declared"]
        assert socials["telegram_url"] is None
        assert socials["x_url"] is None

    def test_the_page_refuses_to_render_a_link_it_cannot_vouch_for(self):
        """A javascript: href in a table of memecoin links is exactly the attack
        this page would otherwise hand its reader. The guard is in web/index.html,
        so this asserts the guard is there and that nothing renders an href
        without it."""
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "const safeHref" in page
        assert "u.protocol === 'http:' || u.protocol === 'https:'" in page
        # Every href built from token data goes through it -- the declared social
        # links, and the DexScreener button added alongside them.
        assert 'href="${esc(links[k])}"' in page
        assert "rel=\"noopener noreferrer nofollow\"" in page
        assert "const href = safeHref(t.dexscreener_url)" in page
        assert 'href="${esc(href)}"' in page

    def test_every_href_in_the_row_template_is_a_vouched_variable(self):
        """A stricter version of the above, so a third link cannot slip in raw.

        The check is textual because the guard is: it is easy to add
        `href="${t.some_url}"` to a row template and never notice that it skipped
        safeHref. Only two spellings are allowed, and both are values safeHref
        already returned.
        """
        import re

        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        hrefs = set(re.findall(r'href="\$\{([^}]+)\}"', page))
        assert hrefs == {"esc(links[k])", "esc(href)"}, hrefs


class TestTheDexScreenerLink:
    """A link button per row, built from the registry rather than from the page."""

    def test_the_url_uses_dexscreeners_chain_id_not_the_stored_chain_name(self):
        # /bsc/, not /bnb/. A page that built the URL from the `chain` column
        # would produce a dead link on every BNB row.
        store = Store()
        store.append_snapshot(
            Snapshot(
                chain="bnb",
                contract="0xdead",
                trigger="mcap_250k",
                source="dexscreener",
                ticker="$B",
                ts=T0,
                market=Market(mcap_usd=300_000.0),
            )
        )
        with store:
            payload = build_payload(store)
        assert payload["tokens"][0]["dexscreener_url"] == "https://dexscreener.com/bsc/0xdead"

    def test_a_chain_with_no_bound_id_gets_no_link_rather_than_a_guess(self):
        from collectors import chains

        assert chains.dexscreener_token_url("robinhood", "0xdead") is None

    def test_the_page_renders_a_dash_rather_than_a_dead_button(self):
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "no DexScreener id bound for this chain" in page


class TestTheMomentumBlock:
    def test_every_row_carries_the_group_even_when_it_is_all_null(self):
        """Null on every row written before schema 7, which is the truth about
        those rows. Omitting the key instead would make the page's own
        "not measured" rendering indistinguishable from a bug."""
        with store_with("dexscreener") as store:
            payload = build_payload(store)
        momentum = payload["tokens"][0]["momentum"]
        assert set(momentum) == {
            "volume_1h_usd",
            "volume_6h_usd",
            "txns_1h",
            "buys_1h",
            "sells_1h",
            "buys_24h",
            "sells_24h",
            "price_change_5m_pct",
            "price_change_1h_pct",
            "price_change_6h_pct",
            "price_change_24h_pct",
        }
        assert all(value is None for value in momentum.values())

    def test_the_payload_states_the_weight_is_zero(self):
        with store_with("dexscreener") as store:
            payload = build_payload(store)
        assert payload["momentum"]["prior_weight_in_composite"] == 0.0

    def test_the_page_does_not_read_a_missing_sell_count_as_zero_sells(self):
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "if (b == null || s == null) return null;" in page


class TestTheColumnGlossary:
    """The three columns people ask about have to be explained on the page."""

    def test_every_table_header_has_a_glossary_entry(self):
        import re

        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        head = re.search(r"<thead>(.*?)</thead>", page, re.S).group(1)
        headers = re.findall(r"<th[^>]*>([^<]+)</th>", head)
        described = set(re.findall(r"^  \['([^']+)',", page, re.M))
        missing = [h.strip() for h in headers if h.strip() not in described]
        assert not missing, f"columns with no glossary entry: {missing}"

    def test_score_complete_and_pillars_are_distinguished_from_each_other(self):
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "fields_present / fields_expected" in page
        assert "It is <i>not</i> the score" in page
        assert "uncalibrated priors" in page

    def test_the_glossary_is_rendered_on_load(self):
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "function renderGlossary()" in page
        assert "renderGlossary();" in page


class TestTheRefreshButton:
    def test_the_button_exists_and_is_wired(self):
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert 'id="refresh"' in page
        assert "addEventListener('click', doRefresh)" in page

    def test_it_actually_refetches_rather_than_re_rendering(self):
        """A refresh button that only re-sorts what is already in memory is a lie.

        Both sources are re-fetched, and with a cache-busting query, because
        'no-store' is honoured by the browser and not always by a CDN in front of
        a static file.
        """
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "loadCollected(), loadLive()" in page
        assert "const bust =" in page
        assert "bust('screener-data.json')" in page
        assert "bust('/api/screener')" in page

    def test_changing_a_filter_does_not_re_poll_dexscreener(self):
        # render() re-draws from memory; doRefresh() goes to the network. The
        # dropdowns are wired to the former.
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "addEventListener('change', render)" in page
        assert "addEventListener('input', renderRows)" in page

    def test_a_failed_refresh_keeps_the_rows_already_on_screen(self):
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "stale and labelled is better than blank" in page

    def test_a_failed_live_refetch_is_not_announced_as_a_re_poll(self):
        """A failed re-fetch leaves the earlier LIVE body in place, so LIVE being set
        says nothing about whether this refresh answered. The note keys on the
        fetch's own result, and the live panel says when its rows are stale."""
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert "const liveFresh = live.status === 'fulfilled' && live.value === true;" in page
        assert "The live view still shows the earlier fetch" in page
        assert "The last refresh failed" in page

    def test_the_button_is_wired_even_when_nothing_loaded(self):
        # When neither source answers on load, retrying is the one thing left.
        page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assert page.index("addEventListener('click', doRefresh)") < page.index(
            "if (!COLLECTED && !LIVE)"
        )


class TestTheBacktestGate:
    def test_the_backtest_is_given_the_gate_the_progress_block_reports(self, monkeypatch):
        """Phase 0 is done at 300 tokens *with a complete social series*. The
        backtest counted snapshots, so it could call the gate met while the
        progress block beside it, reading the same dataset, said it was not."""
        import export_web

        seen: dict = {}
        real = export_web.run_backtest

        def spy(rows, **kwargs):
            seen.update(kwargs)
            return real(rows, **kwargs)

        monkeypatch.setattr(export_web, "run_backtest", spy)
        with store_with("dexscreener") as store:
            payload = build_payload(store)
        assert seen["complete_social"] == payload["progress"]["complete_social_series"]
