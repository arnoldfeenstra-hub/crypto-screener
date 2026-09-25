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
import os
from pathlib import Path

import pytest

from collect import run_cycle
from collectors import journal
from collectors.metrics import TokenMetrics
from collectors.safety import SafetyReport
from collectors.schema import now_ms
from collectors.store import Store

# Thirty minutes ago, not a fixed date -- and that is a bug fix, not a style
# choice.
#
# This was a constant: 2026-09-09T00:00:00Z. Every snapshot these tests build was
# stamped with it, while run_cycle read the real clock, so the fixtures aged in
# real time. `due_for_repricing` returns False past the 7d horizon, so on
# 2026-09-16 the re-pricing test quietly stopped exercising re-pricing: the cycle
# found nothing due, wrote no observations, and the assertion failed for a reason
# that had nothing to do with the code under test. A test that passes for a week
# and then fails on a calendar boundary is worse than one that never passed.
#
# Anchoring to an age instead of a date fixes the whole class at once: every
# fixture is always half an hour old, which is inside every horizon and every
# re-price interval, whatever day the suite runs on.
TS = now_ms() - 30 * 60_000

# Two days ago, for the one test that needs a snapshot old enough that every
# social offset (t+0, +1h, +6h, +24h) is already overdue. It used to get that for
# free from TS being stale, which is why it broke when TS stopped being stale --
# the requirement was real and invisible. Named, so it stays visible, and inside
# the 7d horizon so the row is still due a re-price.
STALE_TS = now_ms() - 2 * 24 * 60 * 60_000


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


class StubTelegram:
    """Stands in for the t.me preview page."""

    def __init__(self, pages=None, error: Exception | None = None):
        self.pages = pages or {}
        self.error = error
        self.asked: list[str] = []

    def fetch(self, handle: str):
        self.asked.append(handle)
        if self.error:
            raise self.error
        if handle not in self.pages:
            return {"exists": False, "members": None, "online": None}
        return self.pages[handle]


class StubSafety:
    def __init__(self, reports=None):
        self.reports = reports or {}

    def fetch(self, tokens):
        return {key: self.reports[key] for key in tokens if key in self.reports}


def cycle(tmp_path, tokens, **kwargs):
    kwargs.setdefault("price_source", StubPrices())
    kwargs.setdefault("with_safety", False)
    # Off unless a test asks for it, so the default path builds no live preview
    # client. tests/conftest.py would block the request anyway; not making it is
    # better than having it blocked.
    kwargs.setdefault("with_social", False)
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


    # -- Shards ----------------------------------------------------------------
    #
    # On 2026-09-20 state/scores.jsonl reached 100.52 MB. GitHub refused every
    # push after that, and each hourly run collected, failed to commit, and lost
    # its rows with the runner. These pin the layout that makes that impossible.

    def test_no_append_grows_a_shard_past_the_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(journal, "SHARD_MAX_BYTES", 400)
        rows = [{"snapshot_id": f"s{i:03d}", "ticker": "$T" * 10} for i in range(60)]
        journal.append_rows("snapshots", rows[:25], tmp_path)
        journal.append_rows("snapshots", rows[25:], tmp_path)
        files = journal.files_for("snapshots", tmp_path)
        assert len(files) > 1
        assert all(path.stat().st_size <= 400 for path in files)
        assert [r["snapshot_id"] for r in journal.read_rows("snapshots", tmp_path)] == [
            r["snapshot_id"] for r in rows
        ]

    def test_the_pre_shard_file_is_read_first_and_never_written_again(self, tmp_path):
        legacy = journal.legacy_path("snapshots", tmp_path)
        legacy.write_text(json.dumps({"snapshot_id": "old"}) + "\n", encoding="utf-8")
        before = legacy.read_bytes()
        journal.append_rows("snapshots", [{"snapshot_id": "new"}], tmp_path)
        assert legacy.read_bytes() == before
        assert [r["snapshot_id"] for r in journal.read_rows("snapshots", tmp_path)] == [
            "old",
            "new",
        ]

    def test_days_and_rollovers_read_back_in_the_order_they_were_written(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(journal, "SHARD_MAX_BYTES", 60)
        monkeypatch.setattr(journal, "_utc_today", lambda: "2026-09-22")
        journal.append_rows("snapshots", [{"snapshot_id": f"a{i}"} for i in range(3)], tmp_path)
        monkeypatch.setattr(journal, "_utc_today", lambda: "2026-09-23")
        journal.append_rows("snapshots", [{"snapshot_id": "b0"}], tmp_path)
        names = [path.name for path in journal.files_for("snapshots", tmp_path)]
        assert names[0].startswith("2026-09-22") and names[-1] == "2026-09-23.jsonl"
        assert [r["snapshot_id"] for r in journal.read_rows("snapshots", tmp_path)] == [
            "a0",
            "a1",
            "a2",
            "b0",
        ]

    def test_rollovers_sort_by_number_not_by_text(self, tmp_path):
        folder = journal.shard_dir("snapshots", tmp_path)
        folder.mkdir(parents=True)
        for name, snapshot_id in (
            ("2026-09-22.10.jsonl", "c"),
            ("2026-09-22.9.jsonl", "b"),
            ("2026-09-22.jsonl", "a"),
        ):
            (folder / name).write_text(
                json.dumps({"snapshot_id": snapshot_id}) + "\n", encoding="utf-8"
            )
        assert [r["snapshot_id"] for r in journal.read_rows("snapshots", tmp_path)] == [
            "a",
            "b",
            "c",
        ]

    def test_a_file_that_is_not_named_like_a_shard_is_not_read(self, tmp_path):
        folder = journal.shard_dir("snapshots", tmp_path)
        folder.mkdir(parents=True)
        (folder / "notes.txt").write_text("not journal\n", encoding="utf-8")
        (folder / "2026-09-22.jsonl.bak").write_text(
            json.dumps({"snapshot_id": "x"}) + "\n", encoding="utf-8"
        )
        assert journal.read_rows("snapshots", tmp_path) == []

    def test_a_row_in_the_pre_shard_file_is_not_appended_again(self, tmp_path):
        """The first run after sharding must not copy the old journal into shards."""
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
            journal.sync(store, tmp_path)
            # Put what was just written where it lived before sharding existed.
            [shard] = journal.files_for("snapshots", tmp_path)
            shard.rename(journal.legacy_path("snapshots", tmp_path))
            again = journal.sync(store, tmp_path)
        assert "snapshots" not in again
        assert journal.summarise(tmp_path)["snapshots"] == 1

    def test_the_manifest_says_how_close_the_largest_file_is_to_the_limit(self, tmp_path):
        from collectors.schema import Snapshot

        with Store() as store:
            store.append_snapshot(
                Snapshot(chain="solana", contract="A", trigger="mcap_250k", source="t")
            )
            journal.sync(store, tmp_path)
        manifest = json.loads((tmp_path / journal.MANIFEST_NAME).read_text(encoding="utf-8"))
        assert manifest["github_file_limit_mb"] == 100
        assert 0 <= manifest["largest_file_mb"] < 1

    # -- Restore -------------------------------------------------------------
    #
    # In DuckDB a failed statement aborts the transaction, so the old restore's
    # "catch the duplicate and carry on" raised on the next row -- and when the
    # duplicate was the last row, COMMIT rolled the whole table back while the
    # restore reported every row written.

    @staticmethod
    def _journal_with_a_second_row_for_token_a(tmp_path, *, last: bool):
        from collectors.schema import Market, Snapshot

        with Store() as store:
            for contract in ("A", "B", "C"):
                store.append_snapshot(
                    Snapshot(
                        chain="solana",
                        contract=contract,
                        trigger="mcap_250k",
                        source="test",
                        market=Market(mcap_usd=300_000.0),
                    )
                )
            journal.sync(store, tmp_path)
        rows = journal.read_rows("snapshots", tmp_path)
        first_a = next(r for r in rows if r["contract"] == "A")
        second_a = {**first_a, "snapshot_id": "a-second-id", "market_mcap_usd": 1.0}
        others = [r for r in rows if r is not first_a]
        ordered = [first_a, *others, second_a] if last else [first_a, second_a, *others]
        for path in journal.files_for("snapshots", tmp_path):
            path.unlink()
        journal.append_rows("snapshots", ordered, tmp_path)
        return first_a

    @pytest.mark.parametrize("last", [False, True])
    def test_a_duplicate_token_in_the_journal_keeps_the_first_row_and_every_other(
        self, tmp_path, last
    ):
        first_a = self._journal_with_a_second_row_for_token_a(tmp_path, last=last)
        with Store() as restored:
            loaded = journal.restore(restored, tmp_path)
            assert loaded["snapshots"] == 3
            assert restored.snapshot_count() == 3
            ids = {row["snapshot_id"] for row in restored.recent_snapshots(10)}
        assert first_a["snapshot_id"] in ids and "a-second-id" not in ids

    def test_a_failed_bulk_load_still_restores_row_by_row(self, tmp_path, monkeypatch):
        import duckdb

        from collectors.schema import Snapshot

        with Store() as store:
            store.append_snapshot(
                Snapshot(chain="solana", contract="A", trigger="mcap_250k", source="t")
            )
            journal.sync(store, tmp_path)

        def refuse(*args, **kwargs):
            raise duckdb.IOException("simulated: the temporary file could not be read")

        monkeypatch.setattr(Store, "_bulk_load", refuse)
        with Store() as restored:
            assert journal.restore(restored, tmp_path)["snapshots"] == 1
            assert restored.snapshot_count() == 1

    def test_restoring_onto_a_partial_database_adds_only_the_missing_rows(self, tmp_path):
        from collectors.schema import Snapshot

        with Store() as store:
            for contract in ("A", "B"):
                store.append_snapshot(
                    Snapshot(chain="solana", contract=contract, trigger="mcap_250k", source="t")
                )
            journal.sync(store, tmp_path)
        with Store() as partial:
            partial.append_snapshot(
                Snapshot(chain="solana", contract="A", trigger="mcap_250k", source="t")
            )
            loaded = journal.restore(partial, tmp_path)
            assert loaded["snapshots"] == 1  # B; the journal's A clashes on (chain, contract)
            assert partial.snapshot_count() == 2

    def test_every_file_in_the_committed_state_is_under_githubs_limit(self):
        """The check that would have caught the outage before it happened.

        Legacy single-file journals are frozen by sharding, so this can only fail
        if something starts writing an unsharded file again.
        """
        state = Path(__file__).resolve().parent.parent / "state"
        if not state.is_dir():
            pytest.skip("no state/ directory in this checkout")
        oversized = [
            (str(path.relative_to(state)), path.stat().st_size)
            for path in state.rglob("*")
            if path.is_file() and path.stat().st_size >= journal.GITHUB_FILE_LIMIT_BYTES
        ]
        assert oversized == []


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
        assert summary["scored"]["safety_fetched"] == 1
        assert summary["totals"]["safety_observations"] == 1
        assert summary["scored"]["excluded_total"] == 0

    def test_a_second_cycle_reuses_the_verdict_instead_of_asking_again(self, tmp_path):
        """Re-asking a keyless rate-limited API the same question every half hour
        is both slow and rude. A stored verdict is reused until it is stale, and a
        reused verdict is not written to the table a second time."""
        # Collected just now, so the second cycle finds it fresh. A verdict has a
        # shelf life -- see the stale case below.
        report = SafetyReport(
            chain="solana",
            contract="A",
            source="goplus",
            collected_at_ms=now_ms(),
            honeypot=False,
            mint_revoked=True,
        )

        asked: list[list] = []

        class Counting(StubSafety):
            def fetch(self, tokens):
                asked.append(list(tokens))
                return super().fetch(tokens)

        tokens = [tradeable("solana", "A", "$A", 300_000.0)]
        source = Counting({("solana", "A"): report})
        first = cycle(tmp_path, tokens, with_safety=True, safety_source=source)
        second = cycle(tmp_path, tokens, with_safety=True, safety_source=source)

        assert first["scored"]["safety_fetched"] == 1
        assert second["scored"]["safety_fetched"] == 0
        assert second["scored"]["safety_known"] == 1
        # One row, not two: nothing new was established the second time.
        assert second["totals"]["safety_observations"] == 1
        # Not "asked for an empty list" -- the second cycle does not reach the
        # source at all, so a rate-limited API sees no request.
        assert len(asked) == 1

    def test_a_stale_verdict_is_looked_up_again(self, tmp_path):
        """Safety is not static: a mint authority gets revoked, an LP gets pulled.

        A verdict older than the refresh window is re-asked, and the new answer is
        appended beside the old one rather than replacing it -- the change over
        time is itself the observation.
        """
        from scoring.runner import SAFETY_MAX_AGE_MS

        old_report = SafetyReport(
            chain="solana",
            contract="A",
            source="goplus",
            collected_at_ms=now_ms() - SAFETY_MAX_AGE_MS - 60_000,
            mint_revoked=True,
        )
        fresh_report = SafetyReport(
            chain="solana",
            contract="A",
            source="goplus",
            collected_at_ms=now_ms(),
            mint_revoked=False,  # the authority came back
        )
        tokens = [tradeable("solana", "A", "$A", 300_000.0)]
        cycle(
            tmp_path,
            tokens,
            with_safety=True,
            safety_source=StubSafety({("solana", "A"): old_report}),
        )
        second = cycle(
            tmp_path,
            tokens,
            with_safety=True,
            safety_source=StubSafety({("solana", "A"): fresh_report}),
        )
        assert second["scored"]["safety_fetched"] == 1
        assert second["totals"]["safety_observations"] == 2
        # A new reading is a new input, so the re-score is a new row.
        assert second["scored"]["written"] == 1
        assert second["totals"]["scores"] == 2

    def test_a_cycle_that_changes_nothing_stores_no_score_and_still_ranks(
        self, tmp_path
    ):
        """An unchanged re-score is not stored a second time; the row that
        records it stays the token's current score, and the page ranks from it."""
        report = SafetyReport(
            chain="solana",
            contract="A",
            source="goplus",
            collected_at_ms=now_ms(),  # fresh, so the second cycle reuses it
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
        page = tmp_path / "screener-data.json"
        runs = [
            cycle(
                tmp_path,
                [tradeable("solana", "A", "$A", 300_000.0)],
                with_safety=True,
                safety_source=StubSafety({("solana", "A"): report}),
                export_to=page,
            )
            for _ in range(2)
        ]
        assert [run["scored"]["rows"] for run in runs] == [1, 1]
        assert [run["scored"]["written"] for run in runs] == [1, 0]
        assert runs[1]["totals"]["scores"] == 1
        (token,) = json.loads(page.read_text(encoding="utf-8"))["tokens"]
        assert token["excluded"] is False
        assert token["score"] is not None
        assert token["rank"] == 1

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


# ---------------------------------------------------------------------------
# The forward social series
# ---------------------------------------------------------------------------


class TestSocialCollection:
    """The one part of the dataset that cannot be reconstructed later.

    On-chain history is backfillable; a Telegram member count at a past timestamp
    is archived nowhere (BUILD_BRIEF.md section 1). So the properties that matter
    are that a declared channel is actually polled, and that an undeclared one
    never gets a guessed handle.
    """

    def social_token(
        self, contract: str, ticker: str, telegram: str | None, *, observed_at_ms=None
    ):
        return token(
            "solana",
            contract,
            # The snapshot's ts is the observation time, and the offsets are
            # measured from it. Default to now so these exercise the steady state:
            # a token that just fired is due for t+0 and nothing else.
            observed_at_ms=observed_at_ms or now_ms(),
            ticker=ticker,
            mcap_usd=400_000.0,
            fdv_usd=480_000.0,
            liquidity_usd=60_000.0,
            volume_24h_usd=800_000.0,
            txns_24h=1500,
            first_seen_at_ms=now_ms() - 3_600_000,
            declared_telegram=telegram is not None,
            telegram_url=telegram,
            listings=["dex"],
        )

    def test_a_declared_channel_is_polled_and_its_count_stored(self, tmp_path):
        client = StubTelegram({"realgroup": {"exists": True, "members": 8412, "online": 91}})
        summary = cycle(
            tmp_path,
            [self.social_token("SoL1", "AAA", "https://t.me/realgroup")],
            with_social=True,
            telegram_client=client,
        )
        assert client.asked == ["realgroup"]
        assert summary["social_tg"]["with_counts"] == 1

        rows = journal.read_rows("social_observations", tmp_path / "state")
        assert [r["members"] for r in rows] == [8412]
        assert [r["handle"] for r in rows] == ["realgroup"]
        assert rows[0]["error"] is None

    def test_a_token_with_no_telegram_never_gets_a_handle_invented(self, tmp_path):
        """`extract_handle` turns the ticker "PAIRZ" into the handle "PAIRZ", and
        t.me/PAIRZ is a real page belonging to somebody else. Polling it would
        write a stranger's member count into this token's row."""
        client = StubTelegram()
        summary = cycle(
            tmp_path,
            [self.social_token("SoL2", "PAIRZ", None)],
            with_social=True,
            telegram_client=client,
        )
        assert client.asked == []
        assert summary["social_tg"]["no_handle"] == 1

        rows = journal.read_rows("social_observations", tmp_path / "state")
        assert rows and all(r["handle"] is None for r in rows)
        assert all(r["members"] is None for r in rows)
        assert all("no telegram handle" in r["error"] for r in rows)

    def test_the_declared_address_reaches_the_snapshot(self, tmp_path):
        cycle(
            tmp_path,
            [self.social_token("SoL3", "BBB", "https://t.me/realgroup")],
            with_social=True,
            telegram_client=StubTelegram(),
        )
        snapshots = journal.read_rows("snapshots", tmp_path / "state")
        assert [s["telegram_url"] for s in snapshots] == ["https://t.me/realgroup"]

    def test_a_failed_preview_is_a_row_with_an_error_not_a_zero(self, tmp_path):
        """A count of zero and a failed lookup are different facts. Storing the
        second as the first is the imputation hard rule 3 forbids."""
        from collectors.social_tg import TelegramError

        cycle(
            tmp_path,
            [self.social_token("SoL4", "CCC", "https://t.me/realgroup")],
            with_social=True,
            telegram_client=StubTelegram(error=TelegramError("HTTP 429")),
        )
        rows = journal.read_rows("social_observations", tmp_path / "state")
        assert rows and all(r["members"] is None for r in rows)
        assert all("429" in r["error"] for r in rows)

    def test_an_observation_records_the_age_it_was_really_taken_at(self, tmp_path):
        """A missed offset stays due, so switching this on fills t+0 for tokens
        that are already old. The row has to say so, or a reader compares a
        day-old reading against a genuine t+0 one."""
        cycle(
            tmp_path,
            [self.social_token("SoL5", "DDD", "https://t.me/realgroup")],
            with_social=True,
            telegram_client=StubTelegram(
                {"realgroup": {"exists": True, "members": 12, "online": 1}}
            ),
        )
        rows = journal.read_rows("social_observations", tmp_path / "state")
        assert rows
        for row in rows:
            assert row["age_minutes"] is not None
            assert row["age_minutes"] >= row["offset_minutes"]

    def test_switching_it_on_late_fills_every_missed_offset_and_says_so(self, tmp_path):
        """A missed offset stays due, which is right -- a late count beats a hole.

        But four readings taken in the same minute are not a t+0/+1h/+6h/+24h
        series, and nothing downstream should mistake them for one. They are
        filed under the offsets they were scheduled for and counted as late.
        """
        client = StubTelegram(
            {"realgroup": {"exists": True, "members": 5000, "online": 40}}
        )
        summary = cycle(
            tmp_path,
            [
                self.social_token(
                    "SoL7", "FFF", "https://t.me/realgroup", observed_at_ms=STALE_TS
                )
            ],
            with_social=True,
            telegram_client=client,
        )
        assert summary["social_tg"]["observations"] == 4
        assert summary["social_tg"]["late"] == 4

        rows = journal.read_rows("social_observations", tmp_path / "state")
        assert sorted(r["offset_minutes"] for r in rows) == [0, 60, 360, 1440]
        # Every one of them was actually taken at the same, much later, age.
        assert all(r["age_minutes"] > 1440 for r in rows)

    def test_the_collector_skips_the_whole_step_when_asked(self, tmp_path):
        client = StubTelegram()
        summary = cycle(
            tmp_path,
            [self.social_token("SoL6", "EEE", "https://t.me/realgroup")],
            with_social=False,
            telegram_client=client,
        )
        assert "social_tg" not in summary
        assert client.asked == []


# ---------------------------------------------------------------------------
# X, the one metered source
# ---------------------------------------------------------------------------


class StubXClient:
    """Stands in for X API v2 recent search, and counts what it was asked."""

    def __init__(self, mentions: int = 12):
        self.mentions = mentions
        self.calls = 0

    def search_recent(self, query, minutes, max_results=100):
        self.calls += 1
        return {
            "data": [
                {"id": str(i), "author_id": f"a{i % 3}", "text": query, "public_metrics": {}}
                for i in range(self.mentions)
            ],
            "includes": {
                "users": [
                    {"id": f"a{i}", "public_metrics": {"followers_count": 500}}
                    for i in range(3)
                ]
            },
            "meta": {"result_count": self.mentions},
        }


class TestXCollection:
    """X runs on the same schedule as everything else, and only with a credential.

    Every other source in this repo is keyless and free. X search is metered per
    post read, so the two properties that matter are that it is off by default and
    that a cycle cannot spend an unbounded amount of somebody's quota.
    """

    def test_it_is_off_when_no_credential_is_configured(self, tmp_path, monkeypatch):
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
        summary = cycle(tmp_path, [tradeable("solana", "A" * 32, "$A", 400_000.0)],
                        with_social=True, telegram_client=StubTelegram())
        assert summary["social_x"] == {"skipped": "X_BEARER_TOKEN is not set"}

    def test_the_absence_is_stated_rather_than_silent(self, tmp_path, monkeypatch):
        # A social series nobody is collecting and a social series of zeroes look
        # identical in a row count afterwards. The summary has to tell them apart.
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
        summary = cycle(tmp_path, [tradeable("solana", "A" * 32, "$A", 400_000.0)],
                        with_social=True, telegram_client=StubTelegram())
        assert "skipped" in summary["social_x"]

    def test_an_injected_client_collects_without_any_credential(self, tmp_path, monkeypatch):
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
        client = StubXClient()
        summary = cycle(tmp_path, [tradeable("solana", "A" * 32, "$A", 400_000.0)],
                        with_social=True, telegram_client=StubTelegram(), x_client=client)
        assert client.calls >= 1
        assert summary["social_x"]["observations"] >= 1
        assert summary["social_x"]["with_counts"] >= 1

    def test_the_budget_caps_the_calls_and_reports_what_it_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
        client = StubXClient()
        summary = cycle(
            tmp_path,
            [tradeable("solana", f"C{i}" * 11, f"$T{i}", 400_000.0) for i in range(6)],
            with_social=True,
            telegram_client=StubTelegram(),
            x_client=client,
            x_max_searches=2,
        )
        assert client.calls == 2
        assert summary["social_x"]["skipped_for_budget"] >= 1
        assert summary["social_x"]["budget"] == 2

    def test_a_skipped_poll_is_not_written_as_an_error_row(self, tmp_path, monkeypatch):
        """A budget is a fact about the collector, not about the token.

        An error row means "we asked and it failed". Writing one for a poll that
        was never attempted would put this repo's own rate limit into the dataset
        as though it were an observation about the token.
        """
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
        client = StubXClient()
        summary = cycle(
            tmp_path,
            [tradeable("solana", f"C{i}" * 11, f"$T{i}", 400_000.0) for i in range(6)],
            with_social=True,
            telegram_client=StubTelegram(),
            x_client=client,
            x_max_searches=2,
        )
        assert summary["social_x"]["with_errors"] == 0
        assert summary["social_x"]["observations"] == 2

    def test_a_failing_client_does_not_end_the_cycle(self, tmp_path, monkeypatch):
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)

        class Broken:
            def search_recent(self, *a, **k):
                raise RuntimeError("boom")

        summary = cycle(tmp_path, [tradeable("solana", "A" * 32, "$A", 400_000.0)],
                        with_social=True, telegram_client=StubTelegram(), x_client=Broken())
        # The cycle still scored and journalled; the failure is a row, not a crash.
        assert "scored" in summary
        assert summary["social_x"]["with_errors"] >= 1

    def test_a_zero_budget_makes_no_calls(self, tmp_path, monkeypatch):
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
        client = StubXClient()
        summary = cycle(tmp_path, [tradeable("solana", "A" * 32, "$A", 400_000.0)],
                        with_social=True, telegram_client=StubTelegram(), x_client=client,
                        x_max_searches=0)
        assert client.calls == 0
        assert summary["social_x"]["skipped_for_budget"] >= 1


class TestTheBudgetReachesTheCycle:
    """collect.main resolves X_MAX_SEARCHES_PER_CYCLE; these pin how."""

    @staticmethod
    def _main(monkeypatch, *, dotenv: dict[str, str] | None = None) -> tuple[int, dict]:
        import collect

        seen: dict = {}

        def fake_load_config():
            # What load_dotenv does: fill in what the real environment left unset.
            for key, value in (dotenv or {}).items():
                if key not in os.environ:
                    monkeypatch.setenv(key, value)

        monkeypatch.setattr(collect, "load_config", fake_load_config)
        monkeypatch.setattr(collect, "run_cycle", lambda **kw: seen.update(kw) or {})
        return collect.main([]), seen

    def test_a_blank_variable_does_not_take_the_collector_down(self, monkeypatch):
        """collect.yml passes an undefined repository variable as an empty string.
        int("") at argparse time crashed every scheduled run before it restored."""
        monkeypatch.setenv("X_MAX_SEARCHES_PER_CYCLE", "")
        code, seen = self._main(monkeypatch)
        assert code == 0
        assert seen["x_max_searches"] is None

    def test_a_budget_in_dotenv_is_honoured_like_the_token_beside_it(self, monkeypatch):
        """The token in .env switched X on; the budget in .env was read before .env
        was loaded, so the metered source ran uncapped."""
        monkeypatch.delenv("X_MAX_SEARCHES_PER_CYCLE", raising=False)
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
        _, seen = self._main(
            monkeypatch, dotenv={"X_BEARER_TOKEN": "t", "X_MAX_SEARCHES_PER_CYCLE": "40"}
        )
        assert seen["x_max_searches"] == 40

    def test_a_malformed_budget_stops_the_run_by_name(self, monkeypatch, capsys):
        monkeypatch.setenv("X_MAX_SEARCHES_PER_CYCLE", "forty")
        code, seen = self._main(monkeypatch)
        assert code == 2
        assert seen == {}
        assert "X_MAX_SEARCHES_PER_CYCLE" in capsys.readouterr().err
