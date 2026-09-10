"""The scheduled collector: the journal, and the cycle around it.

Phase 0 only works if the dataset survives between runs, and the way it survives
is an append-only JSONL journal that git can hold. So the properties under test are
the ones that decide whether six weeks of collection is real or is quietly being
overwritten every half hour:

* a row journalled once is never written again, and never rewritten;
* a restored database is the same dataset, not a similar one;
* a token snapshotted in an earlier run does not fire again in a later one;
* a run that loses its data source still does the rest of its work.

Nothing here opens a socket -- every source is injected, and tests/conftest.py
makes real requests impossible anyway.
"""

from __future__ import annotations

import json

import pytest

from collect import run_cycle
from collectors import journal
from collectors.metrics import TokenMetrics
from collectors.safety import SafetyReport
from collectors.store import Store

TS = 1788912000000


def token(chain: str, contract: str, **fields) -> TokenMetrics:
    base = dict(chain=chain, contract=contract, observed_at_ms=TS, source="dexscreener")
    base.update(fields)
    return TokenMetrics(**base)


def tradeable(chain: str, contract: str, ticker: str, mcap: float) -> TokenMetrics:
    return token(
        chain,
        contract,
        ticker=ticker,
        mcap_usd=mcap,
        fdv_usd=mcap * 1.2,
        liquidity_usd=mcap * 0.15,
        volume_24h_usd=mcap * 2,
        txns_24h=1200,
        first_seen_at_ms=TS - 7_200_000,
        listings=["dex"],
    )


class StubFeed:
    source_name = "dexscreener"

    def __init__(self, tokens, error: Exception | None = None):
        self._tokens = tokens
        self._error = error

    def poll(self):
        if self._error:
            raise self._error
        return self._tokens


class StubPrices:
    def __init__(self, found=None):
        self.found = found or {}

    def fetch(self, tokens):
        return {key: self.found[key] for key in tokens if key in self.found}


class StubSafety:
    def __init__(self, reports=None):
        self.reports = reports or {}

    def fetch(self, tokens):
        return {key: self.reports[key] for key in tokens if key in self.reports}


def cycle(tmp_path, tokens, **kwargs):
    kwargs.setdefault("price_source", StubPrices())
    kwargs.setdefault("with_safety", False)
    return run_cycle(
        chain_names=kwargs.pop("chain_names", ["solana", "bnb"]),
        state_dir=tmp_path / "state",
        db_path=tmp_path / "work.duckdb",
        regime=kwargs.pop("regime", "neutral"),
        feed=tokens if isinstance(tokens, StubFeed) else StubFeed(tokens),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------


class TestJournal:
    def test_every_table_the_store_creates_is_journalled(self):
        """A table missing here would silently not survive a restart.

        The failure would look like a collector that works and a dataset that is
        mysteriously thinner than the run summaries claim.
        """
        with Store() as store:
            tables = {
                row[0]
                for row in store._con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main'"
                ).fetchall()
            }
        assert tables == set(journal.TABLES)

    def test_a_row_is_written_once_and_a_second_sync_adds_nothing(self, tmp_path):
        from collectors.schema import Market, Snapshot

        with Store() as store:
            store.append_snapshot(
                Snapshot(
                    chain="solana",
                    contract="A",
                    trigger="mcap_250k",
                    source="test",
                    market=Market(mcap_usd=300_000.0),
                )
            )
            first = journal.sync(store, tmp_path)
            second = journal.sync(store, tmp_path)
        assert first["snapshots"] == 1
        assert "snapshots" not in second

    def test_appending_never_rewrites_an_earlier_line(self, tmp_path):
        """Append-only as a file layout, not as a promise about SQL."""
        journal.append_rows("snapshots", [{"snapshot_id": "one", "ticker": "$A"}], tmp_path)
        before = journal.path_for("snapshots", tmp_path).read_text(encoding="utf-8")
        journal.append_rows("snapshots", [{"snapshot_id": "two", "ticker": "$B"}], tmp_path)
        after = journal.path_for("snapshots", tmp_path).read_text(encoding="utf-8")
        assert after.startswith(before)

    def test_a_malformed_line_costs_that_line_and_no_others(self, tmp_path):
        """A run killed mid-write must not cost the other six weeks."""
        path = journal.path_for("snapshots", tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"snapshot_id": "one"})
            + "\n{ this is not json\n"
            + json.dumps({"snapshot_id": "two"})
            + "\n",
            encoding="utf-8",
        )
        rows = journal.read_rows("snapshots", tmp_path)
        assert [r["snapshot_id"] for r in rows] == ["one", "two"]

    def test_a_restored_database_holds_the_same_rows(self, tmp_path):
        from collectors.schema import Market, Snapshot

        with Store() as store:
            for i in range(3):
                store.append_snapshot(
                    Snapshot(
                        chain="solana",
                        contract=f"C{i}",
                        trigger="mcap_250k",
                        source="test",
                        market=Market(mcap_usd=300_000.0 + i),
                    )
                )
            journal.sync(store, tmp_path)
            original = {
                row["contract"]: row["market_mcap_usd"]
                for row in store.recent_snapshots(10)
            }

        with Store() as restored:
            journal.restore(restored, tmp_path)
            assert {
                row["contract"]: row["market_mcap_usd"]
                for row in restored.recent_snapshots(10)
            } == original

    def test_the_partition_date_survives_the_round_trip(self, tmp_path):
        """snapshot_date is a DATE column and JSON has no date type."""
        from collectors.schema import Snapshot

        snapshot = Snapshot(
            chain="solana", contract="A", trigger="mcap_250k", source="test", ts=TS
        )
        with Store() as store:
            store.append_snapshot(snapshot)
            journal.sync(store, tmp_path)
        with Store() as restored:
            journal.restore(restored, tmp_path)
            assert restored.snapshot_dates() == [snapshot.snapshot_date]

    def test_restoring_twice_does_not_duplicate(self, tmp_path):
        """A resumed run is not an error, and must not double the dataset."""
        from collectors.schema import Snapshot

        with Store() as store:
            store.append_snapshot(
                Snapshot(chain="solana", contract="A", trigger="mcap_250k", source="t")
            )
            journal.sync(store, tmp_path)
        with Store() as restored:
            journal.restore(restored, tmp_path)
            journal.restore(restored, tmp_path)
            assert restored.snapshot_count() == 1

    def test_the_manifest_reports_what_the_journal_holds(self, tmp_path):
        from collectors.schema import Snapshot

        with Store() as store:
            store.append_snapshot(
                Snapshot(chain="solana", contract="A", trigger="mcap_250k", source="t")
            )
            journal.sync(store, tmp_path)
        manifest = json.loads(
            (tmp_path / journal.MANIFEST_NAME).read_text(encoding="utf-8")
        )
        assert manifest["rows"]["snapshots"] == 1
        assert manifest["schema_version"] >= 3


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------


class TestCycle:
    def test_a_run_collects_scores_and_journals(self, tmp_path):
        summary = cycle(
            tmp_path,
            [
                tradeable("solana", "A", "$A", 300_000.0),
                token("bnb", "0xB", mcap_usd=40_000.0, txns_24h=10),
            ],
        )
        assert summary["poll"] == {"observed": 2, "fired": 1}
        assert summary["totals"]["snapshots"] == 1
        assert summary["journalled"]["snapshots"] == 1
        assert summary["scored"]["rows"] == 1

    def test_a_token_snapshotted_in_an_earlier_run_does_not_fire_again(self, tmp_path):
        """The whole point of persisting state: one snapshot per token, forever."""
        tokens = [tradeable("solana", "A", "$A", 300_000.0)]
        cycle(tmp_path, tokens)
        second = cycle(tmp_path, [*tokens, tradeable("bnb", "0xB", "$B", 500_000.0)])
        assert second["restored"]["snapshots"] == 1
        assert second["poll"]["fired"] == 1
        assert second["totals"]["snapshots"] == 2

    def test_the_journal_grows_and_never_shrinks(self, tmp_path):
        cycle(tmp_path, [tradeable("solana", "A", "$A", 300_000.0)])
        first = journal.summarise(tmp_path / "state")
        cycle(tmp_path, [tradeable("solana", "B", "$B", 400_000.0)])
        second = journal.summarise(tmp_path / "state")
        assert all(second[table] >= first[table] for table in first)
        assert second["snapshots"] == 2

    def test_a_dead_source_still_leaves_the_rest_of_the_run_done(self, tmp_path):
        """A source outage delays a snapshot; it must not lose the cycle."""
        cycle(tmp_path, [tradeable("solana", "A", "$A", 300_000.0)])
        summary = cycle(tmp_path, StubFeed([], error=RuntimeError("dexscreener down")))
        assert summary["poll"] == {"observed": 0, "fired": 0}
        assert summary["scored"]["rows"] == 1  # the earlier token was still re-scored
        assert summary["totals"]["snapshots"] == 1

    def test_a_stale_working_database_is_rebuilt_from_the_journal(self, tmp_path):
        """The journal is the dataset; a leftover DuckDB file is never consulted."""
        cycle(tmp_path, [tradeable("solana", "A", "$A", 300_000.0)])
        (tmp_path / "work.duckdb").write_bytes(b"not a database")
        summary = cycle(tmp_path, [tradeable("solana", "B", "$B", 400_000.0)])
        assert summary["totals"]["snapshots"] == 2

    def test_safety_reports_are_stored_and_make_the_filters_answer(self, tmp_path):
        report = SafetyReport(
            chain="solana",
            contract="A",
            source="goplus",
            collected_at_ms=TS,
            honeypot=False,
            sells_failing=False,
            buy_tax_pct=0.0,
            sell_tax_pct=0.0,
            mint_revoked=True,
            freeze_active=False,
            lp_burned=True,
            top10_ex_lp_pct=11.0,
            deployer_prior_rugs=0,
        )
        summary = cycle(
            tmp_path,
            [tradeable("solana", "A", "$A", 300_000.0)],
            with_safety=True,
            safety_source=StubSafety({("solana", "A"): report}),
        )
        assert summary["scored"]["safety_measured"] == 1
        assert summary["totals"]["safety_observations"] == 1
        assert summary["scored"]["excluded_total"] == 0

    def test_the_web_export_is_written_when_asked(self, tmp_path):
        out = tmp_path / "screener-data.json"
        summary = cycle(
            tmp_path, [tradeable("solana", "A", "$A", 300_000.0)], export_to=out
        )
        assert summary["exported"]["tokens"] == 1
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["tokens"][0]["chain"] == "solana"

    def test_an_unsourced_chain_stops_the_run_rather_than_collecting_half_of_it(
        self, tmp_path
    ):
        with pytest.raises(ValueError, match="robinhood"):
            cycle(tmp_path, [], chain_names=["solana", "robinhood"])

    def test_re_pricing_appends_observations_rather_than_editing(self, tmp_path):
        later = token(
            "solana", "A", observed_at_ms=TS + 600_000, mcap_usd=900_000.0, price_usd=1.0
        )
        cycle(
            tmp_path,
            [tradeable("solana", "A", "$A", 300_000.0)],
            price_source=StubPrices({("solana", "A"): later}),
        )
        with Store(tmp_path / "read.duckdb") as store:
            journal.restore(store, tmp_path / "state")
            snapshot_id = store.recent_snapshots(1)[0]["snapshot_id"]
            observations = store.outcome_observations(snapshot_id)
        assert observations
        assert all(o.mcap_usd == 900_000.0 for o in observations)
