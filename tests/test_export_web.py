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

from collectors.schema import Market, Snapshot
from collectors.store import Store
from export_web import build_payload

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
