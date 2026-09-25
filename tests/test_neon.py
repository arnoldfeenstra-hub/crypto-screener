"""The dataset in Neon: append-only in the database, and the same dataset as before.

The properties that matter are the ones collectors/journal.py was built around,
now held by Postgres instead of a file layout:

* the database itself refuses to change or remove a recorded row;
* a row is recorded once -- a second copy is an error, not a duplicate;
* the first run moves the git journal into Neon exactly once, whole or not at all;
* the local mirror is only ever a cache: stale, foreign or damaged, it is rebuilt
  from Neon, and it can never be pointed at the git journal and cleared;
* a collector run on Neon is the same run as one on the git journal.

These run against a real, throwaway PostgreSQL cluster started for the session.
Where no server binaries are installed they skip rather than fake it: a mock
Postgres would test the mock.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from collectors import journal, neon
from collectors.neon import NeonError, NeonJournal, mirror_counts

psycopg = pytest.importorskip("psycopg")

NEON_PATH = Path(__file__).resolve().parent.parent / "collectors" / "neon.py"


# --- a throwaway PostgreSQL --------------------------------------------------


def _server_bin() -> Path | None:
    for candidate in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (candidate / "initdb").exists() and (candidate / "pg_ctl").exists():
            return candidate
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _user_exists(name: str) -> bool:
    try:
        import pwd

        pwd.getpwnam(name)
    except (ImportError, KeyError):
        return False
    return True


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory):
    """A PostgreSQL cluster for this session, on a unix socket, UTF-8 like Neon.

    initdb refuses to run as root, so as root the cluster runs as the ``postgres``
    system user in a directory it owns.
    """
    bindir = _server_bin()
    if bindir is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    prefix: list[str] = []
    if os.geteuid() == 0:
        if shutil.which("runuser") is None or not _user_exists("postgres"):
            pytest.skip("running as root with no postgres user to run the server as")
        prefix = ["runuser", "-u", "postgres", "--"]
        base = Path(tempfile.mkdtemp(prefix="pgtest-", dir="/tmp"))
        shutil.chown(base, "postgres", "postgres")
    else:
        base = Path(tempfile.mkdtemp(prefix="pgtest-"))
    data = base / "data"
    port = 50_000 + os.getpid() % 10_000
    subprocess.run(
        [*prefix, str(bindir / "initdb"), "-D", str(data), "-A", "trust",
         "-U", "postgres", "-E", "UTF8", "--no-locale"],
        check=True, capture_output=True,
    )
    options = (
        f"-c listen_addresses='' -c unix_socket_directories='{base}' "
        f"-c port={port} -c fsync=off"
    )
    subprocess.run(
        [*prefix, str(bindir / "pg_ctl"), "-D", str(data), "-o", options,
         "-l", str(base / "log"), "-w", "start"],
        check=True, capture_output=True,
    )
    try:
        yield f"host={base} port={port} user=postgres"
    finally:
        subprocess.run(
            [*prefix, str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            capture_output=True,
        )
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def neon_url(postgres_server):
    """A fresh, empty database per test -- the state Neon is in before the first run."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(f"{postgres_server} dbname=postgres", autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    return f"{postgres_server} dbname={name}"


def journal_with(directory: Path, table: str, rows: list[dict]) -> None:
    journal.append_rows(table, rows, directory)


def snapshot_row(snapshot_id: str, **fields) -> dict:
    return {"snapshot_id": snapshot_id, "chain": "solana", "contract": snapshot_id, **fields}


# --- the schema --------------------------------------------------------------


class TestSchema:
    def test_it_is_idempotent_and_the_dataset_keeps_its_id(self, neon_url):
        with NeonJournal.connect(neon_url) as db:
            first = db.ensure_schema()
            assert db.ensure_schema() == first
            assert db.counts() == dict.fromkeys(journal.TABLES, 0)

    def test_every_journal_table_has_a_table(self, neon_url):
        with NeonJournal.connect(neon_url) as db:
            db.ensure_schema()
            names = {
                row[0]
                for row in db.conn.execute(
                    "SELECT tablename FROM pg_tables WHERE tablename LIKE 'journal_%'"
                ).fetchall()
            }
        assert names == {neon.table_name(t) for t in journal.TABLES} | {"journal_meta"}


# --- append-only, in the database --------------------------------------------


class TestTheDatabaseRefusesChange:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE journal_snapshots SET row = row",
            "DELETE FROM journal_snapshots",
            "TRUNCATE journal_snapshots",
            "UPDATE journal_meta SET value = 'x'",
            "DELETE FROM journal_meta",
        ],
    )
    def test_a_recorded_row_cannot_be_changed_or_removed(self, neon_url, tmp_path, statement):
        journal_with(tmp_path, "snapshots", [snapshot_row("S1")])
        with NeonJournal.connect(neon_url) as db:
            db.ensure_schema()
            db.bootstrap(tmp_path)
            with pytest.raises(psycopg.Error, match="append-only"):
                db.conn.execute(statement)
            assert db.counts()["snapshots"] == 1

    def test_a_second_copy_of_a_row_is_an_error_not_a_duplicate(self, neon_url):
        from psycopg.types.json import Jsonb

        with NeonJournal.connect(neon_url) as db:
            db.ensure_schema()
            insert = "INSERT INTO journal_snapshots (row_key, row) VALUES (%s, %s)"
            db.conn.execute(insert, ("S1", Jsonb(snapshot_row("S1"))))
            with pytest.raises(psycopg.errors.UniqueViolation):
                db.conn.execute(insert, ("S1", Jsonb(snapshot_row("S1", chain="bnb"))))


MUTATING_SQL = [
    r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b", r"\bTRUNCATE\b", r"\bALTER\b",
    r"\bREPLACE\s+INTO\b", r"\bINSERT\s+OR\s+REPLACE\b", r"\bON\s+CONFLICT\b", r"\bUPSERT\b",
]


def _literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        doc
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and (doc := ast.get_docstring(node, clean=False))
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


@pytest.mark.parametrize("pattern", MUTATING_SQL)
def test_the_module_writes_no_mutating_sql_except_to_refuse_it(pattern):
    """The same scan tests/test_store_append_only.py runs on store.py. The guard's
    three statements are the one exception, and they name those words to refuse
    them -- which the database tests above prove."""
    guard = set(neon._APPEND_ONLY_GUARD)
    offenders = [
        literal
        for literal in _literals(NEON_PATH)
        if literal not in guard and re.search(pattern, literal, re.IGNORECASE)
    ]
    assert not offenders, f"{pattern} in collectors/neon.py: {offenders}"


def test_the_scan_is_running():
    assert _literals(NEON_PATH), "no literals extracted -- the scan is not running"
    assert any("UPDATE" in g for g in neon._APPEND_ONLY_GUARD)


# --- the migration -----------------------------------------------------------


class TestBootstrap:
    def test_it_copies_the_git_journal_once(self, neon_url, tmp_path):
        journal_with(tmp_path, "snapshots", [snapshot_row("S1"), snapshot_row("S2")])
        journal_with(tmp_path, "labels", [{"label_id": "L1", "snapshot_id": "S1"}])
        with NeonJournal.connect(neon_url) as db:
            db.ensure_schema()
            assert db.bootstrap(tmp_path) == {"snapshots": 2, "labels": 1}
            assert db.bootstrap(tmp_path) == {}
            assert db.counts()["snapshots"] == 2

    def test_it_keeps_the_order_rows_were_recorded_in(self, neon_url, tmp_path):
        journal_with(tmp_path, "snapshots", [snapshot_row(f"S{i}") for i in range(5)])
        with NeonJournal.connect(neon_url) as db:
            db.ensure_schema()
            db.bootstrap(tmp_path)
            keys = [
                r[0]
                for r in db.conn.execute(
                    "SELECT row_key FROM journal_snapshots ORDER BY seq"
                ).fetchall()
            ]
        assert keys == [f"S{i}" for i in range(5)]

    def test_a_key_recorded_twice_keeps_its_first_row(self, neon_url, tmp_path):
        first, second = snapshot_row("S1", chain="solana"), snapshot_row("S1", chain="bnb")
        journal_with(tmp_path, "snapshots", [first, second])
        with NeonJournal.connect(neon_url) as db:
            db.ensure_schema()
            assert db.bootstrap(tmp_path) == {"snapshots": 1}
            row = db.conn.execute("SELECT row FROM journal_snapshots").fetchone()[0]
        assert row["chain"] == "solana"

    def test_a_failed_bootstrap_leaves_neon_empty_to_try_again(
        self, neon_url, tmp_path, monkeypatch
    ):
        journal_with(tmp_path, "snapshots", [snapshot_row("S1")])
        journal_with(tmp_path, "labels", [{"label_id": "L1", "snapshot_id": "S1"}])
        real_insert = NeonJournal._insert

        def failing(self, table, rows, key):
            if table == "labels":
                raise psycopg.errors.QueryCanceled("connection lost mid-copy")
            return real_insert(self, table, rows, key)

        with NeonJournal.connect(neon_url) as db:
            db.ensure_schema()
            monkeypatch.setattr(NeonJournal, "_insert", failing)
            with pytest.raises(psycopg.Error):
                db.bootstrap(tmp_path)
            monkeypatch.setattr(NeonJournal, "_insert", real_insert)
            assert db.counts()["snapshots"] == 0
            assert db.bootstrap(tmp_path) == {"snapshots": 1, "labels": 1}


# --- the mirror ----------------------------------------------------------------


class TestTheMirror:
    def _seeded(self, db, source: Path, count: int = 3) -> str:
        journal_with(source, "snapshots", [snapshot_row(f"S{i}") for i in range(count)])
        dataset_id = db.ensure_schema()
        db.bootstrap(source)
        return dataset_id

    def test_a_pull_fetches_only_what_is_new(self, neon_url, tmp_path):
        mirror = tmp_path / "mirror"
        with NeonJournal.connect(neon_url) as db:
            dataset_id = self._seeded(db, tmp_path / "state")
            assert db.pull(mirror, dataset_id) == {"snapshots": 3}
            assert db.pull(mirror, dataset_id) == {}
            assert mirror_counts(mirror) == db.counts()

    def test_a_mirror_of_another_database_is_rebuilt(self, neon_url, tmp_path):
        mirror = tmp_path / "mirror"
        with NeonJournal.connect(neon_url) as db:
            dataset_id = self._seeded(db, tmp_path / "state")
            db.pull(mirror, dataset_id)
            journal.append_rows("snapshots", [snapshot_row("FOREIGN")], mirror)
            state = neon._read_state(mirror)
            state["dataset_id"] = "some-other-database"
            neon._write_state(mirror, state)
            assert db.pull(mirror, dataset_id) == {"snapshots": 3}
            keys = {r["snapshot_id"] for r in journal.read_rows("snapshots", mirror)}
        assert keys == {"S0", "S1", "S2"}

    def test_a_damaged_mirror_is_rebuilt(self, neon_url, tmp_path):
        mirror = tmp_path / "mirror"
        with NeonJournal.connect(neon_url) as db:
            dataset_id = self._seeded(db, tmp_path / "state")
            db.pull(mirror, dataset_id)
            (shard,) = journal.files_for("snapshots", mirror)
            first_line = shard.read_text(encoding="utf-8").splitlines(True)[0]
            shard.write_text(first_line, encoding="utf-8")
            db.pull(mirror, dataset_id)
            assert mirror_counts(mirror) == db.counts()

    def test_the_git_journal_can_never_be_cleared_as_a_mirror(self, neon_url, tmp_path):
        """Pointed at state/ by mistake, a rebuild must refuse, not empty it."""
        state = tmp_path / "state"
        with NeonJournal.connect(neon_url) as db:
            dataset_id = self._seeded(db, state)
            before = {p: p.read_bytes() for p in state.rglob("*.jsonl")}
            with pytest.raises(NeonError, match="not a Neon mirror"):
                db.pull(state, dataset_id)
        assert {p: p.read_bytes() for p in state.rglob("*.jsonl")} == before


# --- the collector, end to end -------------------------------------------------


class TestTheCycleOnNeon:
    """The collector-loop tests' own harness, pointed at Neon."""

    def _cycle(self, tmp_path, tokens, neon_url=None, **kwargs):
        from tests.test_collector_loop import cycle

        if neon_url is not None:
            kwargs.update(database_url=neon_url, mirror_dir=tmp_path / "mirror")
        return cycle(tmp_path, tokens, **kwargs)

    def test_a_run_records_to_neon_and_leaves_state_to_the_heartbeat(self, neon_url, tmp_path):
        from tests.test_collector_loop import tradeable

        summary = self._cycle(tmp_path, [tradeable("solana", "A", "$A", 300_000.0)], neon_url)
        assert summary["store"] == "neon"
        assert summary["journalled"]["snapshots"] == 1
        with NeonJournal.connect(neon_url) as db:
            assert db.counts()["snapshots"] == 1
        written = sorted(p.name for p in (tmp_path / "state").rglob("*") if p.is_file())
        assert written == ["manifest.json"]
        assert '"store": "neon"' in (tmp_path / "state" / "manifest.json").read_text()

    def test_the_first_run_on_neon_carries_the_git_journal_over(self, neon_url, tmp_path):
        from tests.test_collector_loop import tradeable

        token_a = tradeable("solana", "A", "$A", 300_000.0)
        self._cycle(tmp_path, [token_a])  # the git journal, as before
        jsonl = {p: p.read_bytes() for p in (tmp_path / "state").rglob("*.jsonl")}
        second = self._cycle(
            tmp_path, [token_a, tradeable("bnb", "0xB", "$B", 500_000.0)], neon_url
        )
        assert second["neon"]["bootstrapped"]["snapshots"] == 1
        assert second["restored"]["snapshots"] == 1
        assert second["poll"]["fired"] == 1  # A was already in the dataset
        with NeonJournal.connect(neon_url) as db:
            assert db.counts()["snapshots"] == 2
        # The git journal is read once and never written again.
        assert {p: p.read_bytes() for p in (tmp_path / "state").rglob("*.jsonl")} == jsonl

    def test_a_lost_mirror_costs_a_download_not_the_dataset(self, neon_url, tmp_path):
        from tests.test_collector_loop import tradeable

        self._cycle(tmp_path, [tradeable("solana", "A", "$A", 300_000.0)], neon_url)
        shutil.rmtree(tmp_path / "mirror")
        second = self._cycle(tmp_path, [tradeable("bnb", "0xB", "$B", 500_000.0)], neon_url)
        assert second["neon"]["pulled"]["snapshots"] == 1
        assert second["totals"]["snapshots"] == 2

    def test_the_mirror_may_not_be_the_git_journal(self, neon_url, tmp_path):
        from collect import run_cycle

        with pytest.raises(ValueError, match="different directories"):
            run_cycle(
                chain_names=["solana"],
                state_dir=tmp_path / "state",
                mirror_dir=tmp_path / "state",
                database_url=neon_url,
                db_path=tmp_path / "work.duckdb",
            )

    def test_a_dropped_connection_at_the_end_is_retried_not_lost(
        self, neon_url, tmp_path, monkeypatch
    ):
        from tests.test_collector_loop import tradeable

        real_push = NeonJournal.push
        calls = {"n": 0}

        def flaky(self, store, mirror):
            calls["n"] += 1
            if calls["n"] == 1:
                raise psycopg.OperationalError("server closed the connection unexpectedly")
            return real_push(self, store, mirror)

        monkeypatch.setattr(NeonJournal, "push", flaky)
        monkeypatch.setattr(neon.time, "sleep", lambda seconds: None)
        summary = self._cycle(tmp_path, [tradeable("solana", "A", "$A", 300_000.0)], neon_url)
        assert calls["n"] == 2
        assert summary["journalled"]["snapshots"] == 1
        with NeonJournal.connect(neon_url) as db:
            assert db.counts()["snapshots"] == 1
