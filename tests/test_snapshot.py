"""Snapshot writer tests -- BUILD_BRIEF.md section 4, and hard rule 3.

The theme here is that the writer copies and does not invent. Most of these tests
are about the difference between ``None`` and ``0``, because that difference is
invisible once it has been written wrong, and it is the difference between "no one
posted about this token" and "we never looked".
"""

from __future__ import annotations

import json
import math

import pytest

from collectors.metrics import TokenMetrics
from collectors.schema import SNAPSHOT_COLUMNS, Snapshot
from collectors.snapshot import build_snapshot
from collectors.store import Store
from collectors.trigger_watcher import evaluate, evaluate_metrics

BASE_MS = 1788912000000  # 2026-09-09T00:00:00Z


def metrics(**overrides) -> TokenMetrics:
    defaults = dict(
        chain="solana",
        contract="Tok1111111111111111111111111111111111111111",
        observed_at_ms=BASE_MS,
        source="bitquery",
        ticker="$TEST",
        first_seen_at_ms=BASE_MS - 7_200_000,
        mcap_usd=260_000.0,
        holder_count=310,
    )
    defaults.update(overrides)
    return TokenMetrics(**defaults)


def build(**overrides) -> Snapshot:
    m = metrics(**overrides)
    return build_snapshot(m, evaluate_metrics(m), source=m.source)


class TestOnlyAtTheTrigger:
    def test_refuses_to_snapshot_a_token_that_did_not_cross(self):
        """A row captured off-trigger breaks the lifecycle match for the whole sample."""
        m = metrics(mcap_usd=1_000.0, holder_count=5)
        with pytest.raises(ValueError, match="did not cross"):
            build_snapshot(m, evaluate_metrics(m))

    def test_records_which_trigger_fired(self):
        assert build(mcap_usd=260_000.0, holder_count=5).trigger == "mcap_250k"
        assert build(mcap_usd=None, holder_count=600).trigger == "holders_500"


class TestNothingIsInvented:
    def test_unmeasured_fields_are_null_not_zero(self):
        """The social groups are structurally empty in Phase 0. Null says so."""
        snap = build()
        assert snap.social_x.mentions_24h is None
        assert snap.social_tg.members is None
        assert snap.trends.google_trends_delta is None
        assert snap.lineage.meta_tag is None
        assert snap.flows.smart_money_entries is None

    def test_a_measured_zero_survives_as_zero(self):
        snap = build(volume_24h_usd=0.0)
        assert snap.market.volume_24h_usd == 0.0
        assert snap.market.volume_24h_usd is not None

    def test_nan_from_upstream_becomes_null(self):
        snap = build(liquidity_usd=float("nan"))
        assert snap.market.liquidity_usd is None
        assert not any(
            isinstance(v, float) and math.isnan(v) for v in snap.to_row().values()
        )

    def test_unknown_age_stays_unknown(self):
        """Zero would claim the token launched the instant it was observed."""
        assert build(first_seen_at_ms=None).age_at_trigger_minutes is None
        assert build().age_at_trigger_minutes == 120

    def test_declared_socials_are_carried_from_block_one(self):
        """The best-evidenced feature in the schema, and free at launch."""
        snap = build(declared_telegram=True, declared_x=True, declared_website=False)
        assert snap.socials_declared.telegram is True
        assert snap.socials_declared.website is False

    def test_undeclared_and_unchecked_are_different(self):
        unchecked = build(declared_telegram=None)
        checked = build(declared_telegram=False)
        assert unchecked.socials_declared.telegram is None
        assert checked.socials_declared.telegram is False


class TestCompleteness:
    def test_completeness_counts_only_feature_fields(self):
        sparse = build(
            mcap_usd=None,
            holder_count=600,
            first_seen_at_ms=None,
            liquidity_usd=None,
            price_usd=None,
        )
        present, expected = sparse.completeness()
        assert 0 < present < expected
        # Feature leaves only, not the identity columns. Grew from 40 to 54 at
        # schema version 2: market.fdv_usd plus the 13-field mindshare group.
        assert expected == 54

    def test_a_richer_row_scores_higher(self):
        sparse = build(mcap_usd=None, holder_count=600)
        rich = build(
            liquidity_usd=68_000.0,
            price_usd=0.00026,
            volume_24h_usd=1_800_000.0,
            top10_ex_lp_pct=22.5,
            mint_revoked=True,
            freeze_active=False,
            deployer_address="Dep1",
            declared_telegram=True,
            declared_x=True,
            declared_website=True,
            listings=["dex"],
        )
        assert rich.completeness()[0] > sparse.completeness()[0]

    def test_the_modifier_is_the_ratio_score_md_expects(self):
        snap = build()
        present, expected = snap.completeness()
        assert snap.to_row()["data_completeness"] == pytest.approx(present / expected)


class TestRoundTrip:
    def test_row_matches_the_declared_columns_exactly(self):
        assert list(build().to_row()) == [name for name, _ in SNAPSHOT_COLUMNS]

    def test_json_export_keeps_the_section_4_shape(self):
        payload = build(listings=["dex"]).to_dict()
        for key in (
            "snapshot_id",
            "ts",
            "trigger",
            "ticker",
            "chain",
            "contract",
            "age_at_trigger_minutes",
            "market",
            "holders",
            "authorities",
            "deployer",
            "launch",
            "flows",
            "social_x",
            "social_tg",
            "socials_declared",
            "trends",
            "lineage",
            "listings",
            "regime",
            "labels",
        ):
            assert key in payload, key
        assert payload["labels"] == {"filled_at": None}
        assert json.dumps(payload)  # must survive serialisation

    def test_it_survives_a_write_and_a_read(self):
        snap = build(listings=["dex", "aggregator"])
        with Store() as store:
            store.append_snapshot(snap)
            row = store.fetch_snapshot(snap.snapshot_id)
        assert row is not None
        assert row["contract"] == snap.contract
        assert row["market_mcap_usd"] == 260_000.0
        assert json.loads(row["listings"]) == ["dex", "aggregator"]
        assert row["social_x_mentions_24h"] is None

    def test_the_partition_key_is_the_utc_date_of_the_observation(self):
        assert build().snapshot_date == "2026-09-09"


class TestLabelsAreNotStoredOnTheSnapshot:
    def test_no_label_column_exists_on_the_snapshot_table(self):
        """Filling a label later would otherwise be an update. Hard rule 1."""
        columns = {name for name, _ in SNAPSHOT_COLUMNS}
        assert not [c for c in columns if c.startswith(("max_multiple", "survived"))]

    def test_the_labels_table_is_keyed_by_snapshot_id(self):
        from collectors.schema import LABEL_COLUMNS

        names = {name for name, _ in LABEL_COLUMNS}
        assert "snapshot_id" in names
        assert {"max_multiple_1h", "max_multiple_7d", "survived_24h"} <= names
        # All five horizons from BUILD_BRIEF.md section 2, decided later, logged now.
        assert len([n for n in names if n.startswith("max_multiple_")]) == 5


def test_evaluate_and_build_agree_on_the_trigger_name():
    for mcap, holders, expected in [
        (260_000.0, 5, "mcap_250k"),
        (None, 600, "holders_500"),
        (400_000.0, 900, "mcap_250k"),
    ]:
        m = metrics(mcap_usd=mcap, holder_count=holders)
        assert build_snapshot(m, evaluate(mcap, holders)).trigger == expected
