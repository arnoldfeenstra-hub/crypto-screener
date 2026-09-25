"""Neon -- the dataset's system of record, append-only by construction.

Why the dataset moved out of git
--------------------------------
state/ carried the dataset in git (collectors/journal.py): readable, append-only
by file layout, and free to read back on every run. It is also the wrong home for
a dataset that grows ~20 MB a day. Every run's rows become permanent repository
history, the repository only ever grows, and one file crossing GitHub's 100 MiB
limit silently stopped the collector for four days in September 2026.

Neon is Postgres, and the Vercel project integrates with it. With
``SCREENER_DATABASE_URL`` set, the collector keeps the dataset there; without it,
nothing changes and state/ remains the journal.

Layout
------
One table per journal table, ``journal_<table>``:

* ``seq`` -- bigserial: the order rows were recorded in, and the sync watermark;
* ``row_key`` -- the row's own primary key (``snapshot_id``, ``score_id``, ...),
  UNIQUE, so a second copy of a row is an error rather than a silent duplicate;
* ``row`` -- JSONB, the row exactly as the file journal records it;
* ``appended_at`` -- when it reached Neon.

JSONB rather than a typed column per field because the collector's schema grows
(schema version 8 as this is written), and a typed copy would need its shape
changed for every new field. A JSONB row is what was recorded under whatever schema
was current, and ``row->>'ticker'`` still queries it.

**Append-only is enforced by the database.** Triggers refuse row changes, row
removal and table emptying on every journal table (:data:`_APPEND_ONLY_GUARD`).
That stops accidents and code; an owner who removes a trigger on purpose is making
a decision nobody makes by mistake.

The working copy, and why it is cached
--------------------------------------
Collection still runs in DuckDB, which wants the whole dataset at the start of a
run. Downloading ~160 MB from Neon every hour would spend a free plan's monthly
transfer allowance in about two days. So a run keeps a *mirror* -- the same JSONL
shard layout collectors/journal.py reads -- cached between runs (actions/cache in
CI), and pulls only rows above the mirror's watermark. The mirror is a cache and
never the record: one that belongs to another database, or whose row counts
disagree with Neon's, is emptied and rebuilt from Neon.

A run with Neon on::

    ensure_schema -> bootstrap (once: copy state/ into an empty Neon) -> pull
    -> restore DuckDB from the mirror -> collect -> push new rows -> pull
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collectors import journal
from collectors.schema import SCHEMA_VERSION

log = logging.getLogger("neon")

ENV_VAR = "SCREENER_DATABASE_URL"
DEFAULT_MIRROR_DIR = Path(".neon-mirror")
SYNC_STATE = "neon-sync.json"
PAGE_ROWS = 5_000

_META = "journal_meta"


class NeonError(RuntimeError):
    """The Neon journal and its mirror cannot be reconciled."""


def table_name(table: str) -> str:
    """``journal_<table>``, for the journal tables only. Every SQL identifier in this
    module comes from here or is a constant, never from input."""
    if table not in journal.TABLES:
        raise KeyError(f"{table!r} is not a journal table")
    return f"journal_{table}"


_SCHEMA = (
    f"CREATE TABLE IF NOT EXISTS {_META} ("
    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
    "recorded_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    *(
        f"CREATE TABLE IF NOT EXISTS {table_name(table)} ("
        "seq BIGSERIAL PRIMARY KEY, "
        "row_key TEXT NOT NULL UNIQUE, "
        "row JSONB NOT NULL, "
        "appended_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        for table in journal.TABLES
    ),
)

# The only SQL in this module that names a statement able to change a recorded row,
# and it names them to refuse them. tests/test_neon.py scans every other literal in
# this file for such statements, the way tests/test_store_append_only.py scans
# collectors/store.py, and checks that these three do refuse.
_APPEND_ONLY_GUARD = (
    "CREATE OR REPLACE FUNCTION journal_refuse_change() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN "
    "RAISE EXCEPTION 'journal table % is append-only: % refused (CLAUDE.md hard rule 1)', "
    "TG_TABLE_NAME, TG_OP USING ERRCODE = 'insufficient_privilege'; "
    "END; $$",
    "CREATE OR REPLACE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION journal_refuse_change()",
    "CREATE OR REPLACE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
    "FOR EACH STATEMENT EXECUTE FUNCTION journal_refuse_change()",
)


class NeonJournal:
    """The append-only journal, in Postgres. One connection, one writer.

    The collector workflow runs one cycle at a time (collect.yml's concurrency
    group), which is what makes a single ``seq`` watermark per table sufficient.
    The row-count check in :meth:`pull` catches the case where it would not be.
    """

    def __init__(self, connection: Any, url: str | None = None) -> None:
        self.conn = connection
        # Kept only so record() can reconnect after a dropped connection. Never
        # logged: it carries the password.
        self._url = url

    @staticmethod
    def _open(url: str) -> Any:
        # Imported here: only the collector talks to Neon. The Vercel function in
        # api/ is stdlib-only and must never pull a database driver in.
        import psycopg

        # autocommit, with explicit transaction blocks around every write that
        # must land whole. prepare_threshold=None because Neon's pooled endpoint
        # runs PgBouncer in transaction mode, where server-side prepared statements
        # do not survive between transactions.
        return psycopg.connect(
            url, autocommit=True, prepare_threshold=None, connect_timeout=30
        )

    @classmethod
    def connect(cls, url: str) -> NeonJournal:
        return cls(cls._open(url), url)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> NeonJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- schema ---------------------------------------------------------------

    def ensure_schema(self) -> str:
        """Create the tables and the append-only guard if absent. Returns the
        database's dataset id -- minted once, so a mirror can tell which database
        it is a copy of."""
        with self.conn.transaction(), self.conn.cursor() as cur:
            for statement in _SCHEMA:
                cur.execute(statement)
            cur.execute(_APPEND_ONLY_GUARD[0])
            for name in (_META, *(table_name(table) for table in journal.TABLES)):
                for template in _APPEND_ONLY_GUARD[1:]:
                    cur.execute(template.format(table=name))
            cur.execute(
                f"INSERT INTO {_META} (key, value) "
                "SELECT 'dataset_id', gen_random_uuid()::text "
                f"WHERE NOT EXISTS (SELECT 1 FROM {_META} WHERE key = 'dataset_id')"
            )
            cur.execute(f"SELECT value FROM {_META} WHERE key = 'dataset_id'")
            row = cur.fetchone()
        return str(row[0])

    def counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            out: dict[str, int] = {}
            for table in journal.TABLES:
                cur.execute(f"SELECT count(*) FROM {table_name(table)}")
                out[table] = int(cur.fetchone()[0])
        return out

    # --- writing --------------------------------------------------------------

    def _insert(self, table: str, rows: Iterable[dict[str, Any]], key: str) -> int:
        from psycopg.types.json import Jsonb

        count = 0
        with self.conn.cursor() as cur, cur.copy(
            f"COPY {table_name(table)} (row_key, row) FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row((str(row[key]), Jsonb(journal.encode_row(row))))
                count += 1
        return count

    def bootstrap(self, source: Path | str) -> dict[str, int]:
        """Copy a file journal into Neon -- once, and only into an empty Neon.

        This is the migration. It runs in one transaction, so a failure leaves Neon
        empty and the next run tries again, instead of half a dataset that would
        then never bootstrap. Rows keep the order they were recorded in, which
        becomes their ``seq`` order. A key recorded twice in the file journal keeps
        its first row, the same rule restore applies; a row with no key cannot be
        addressed and is left out, with a warning.
        """
        if any(self.counts().values()):
            return {}
        copied: dict[str, int] = {}
        with self.conn.transaction():
            for table, (_, key) in journal.TABLES.items():
                seen: set[str] = set()
                rows: list[dict[str, Any]] = []
                for row in journal.read_rows(table, source):
                    value = row.get(key)
                    if value is None:
                        log.warning("bootstrap: a %s row has no %s; left out", table, key)
                        continue
                    if str(value) in seen:
                        continue
                    seen.add(str(value))
                    rows.append(row)
                count = self._insert(table, rows, key)
                if count:
                    copied[table] = count
        return copied

    def push(self, store: Any, mirror: Path | str) -> dict[str, int]:
        """Record every row the working database holds that Neon does not yet.

        "Does not yet" is judged against the mirror, which :meth:`pull` has just
        made equal to Neon. All tables go in one transaction, so a run's rows land
        together or not at all. A key Neon already holds aborts the lot rather
        than being skipped: it would mean the mirror was wrong, which is a bug to
        find, not to paper over.
        """
        written: dict[str, int] = {}
        with self.conn.transaction():
            for table, (columns, key) in journal.TABLES.items():
                seen = journal.existing_keys(table, mirror)
                fresh = [
                    row
                    for row in store.export_rows(table, [name for name, _ in columns])
                    if str(row.get(key)) not in seen
                ]
                count = self._insert(table, fresh, key)
                if count:
                    written[table] = count
        return written

    def record(
        self,
        store: Any,
        mirror: Path | str,
        dataset_id: str,
        *,
        attempts: int = 3,
        pause_seconds: float = 5.0,
    ) -> dict[str, int]:
        """End of a run: push its new rows and bring the mirror up to Neon.

        A run's rows exist only in the working database until this succeeds, so a
        dropped connection is retried rather than costing the run. Retrying is safe
        because each attempt pulls first: if a previous attempt's commit landed but
        its acknowledgement was lost, those rows are in the mirror before anything
        is sent again, and push skips them.
        """
        import psycopg

        for attempt in range(1, attempts + 1):
            try:
                self.pull(mirror, dataset_id)
                written = self.push(store, mirror)
                self.pull(mirror, dataset_id)
                return written
            except psycopg.OperationalError:
                if attempt == attempts or self._url is None:
                    raise
                log.warning(
                    "Neon connection failed (attempt %d of %d); reconnecting", attempt, attempts
                )
                time.sleep(pause_seconds * attempt)
                try:
                    self.conn.close()
                finally:
                    self.conn = self._open(self._url)
        raise AssertionError("unreachable")  # pragma: no cover

    # --- reading --------------------------------------------------------------

    def pull(self, mirror: Path | str, dataset_id: str) -> dict[str, int]:
        """Bring the mirror up to Neon. Returns the rows fetched per table.

        A mirror of another database, or one whose row counts disagree with Neon's
        afterwards, is emptied and rebuilt from Neon in full. The rebuild is the
        expensive path -- the whole dataset over the wire -- and it is taken only
        when the cheap one cannot be trusted.
        """
        mirror = Path(mirror)
        state = _read_state(mirror)
        if state is None or state.get("dataset_id") != dataset_id:
            _reset_mirror(mirror)
            state = {"dataset_id": dataset_id, "seq": {}}
        pulled = self._pull_into(mirror, state)
        if mirror_counts(mirror) != self.counts():
            log.warning("the mirror disagrees with Neon; rebuilding it from Neon")
            _reset_mirror(mirror)
            state = {"dataset_id": dataset_id, "seq": {}}
            pulled = self._pull_into(mirror, state)
            if mirror_counts(mirror) != self.counts():
                raise NeonError("the mirror still disagrees with Neon after a rebuild")
        return pulled

    def _pull_into(self, mirror: Path, state: dict[str, Any]) -> dict[str, int]:
        pulled: dict[str, int] = {}
        with self.conn.cursor() as cur:
            for table in journal.TABLES:
                after = int(state["seq"].get(table, 0))
                total = 0
                while True:
                    cur.execute(
                        f"SELECT seq, row FROM {table_name(table)} "
                        "WHERE seq > %s ORDER BY seq LIMIT %s",
                        (after, PAGE_ROWS),
                    )
                    page = cur.fetchall()
                    if not page:
                        break
                    journal.append_rows(table, [row for _, row in page], mirror)
                    after = int(page[-1][0])
                    total += len(page)
                    # Recorded after every page, so an interrupted pull resumes
                    # from the last page it wrote rather than from the start.
                    state["seq"][table] = after
                    _write_state(mirror, state)
                if total:
                    pulled[table] = total
        state["synced_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _write_state(mirror, state)
        return pulled


# --- the mirror -----------------------------------------------------------------


def _read_state(mirror: Path) -> dict[str, Any] | None:
    path = mirror / SYNC_STATE
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) and isinstance(state.get("seq"), dict) else None


def _write_state(mirror: Path, state: dict[str, Any]) -> None:
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / SYNC_STATE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _reset_mirror(mirror: Path) -> None:
    """Empty a mirror so it can be rebuilt. Refuses anything that is not a mirror.

    The directory must be absent, empty, or carry this module's sync-state file.
    Pointed at state/ -- the git journal, which never has one -- this raises
    instead of clearing it.
    """
    if not mirror.exists():
        mirror.mkdir(parents=True)
        return
    entries = list(mirror.iterdir())
    if entries and not (mirror / SYNC_STATE).is_file():
        raise NeonError(
            f"{mirror} holds files but is not a Neon mirror (no {SYNC_STATE}); "
            "refusing to clear it"
        )
    for entry in entries:
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def mirror_counts(mirror: Path | str) -> dict[str, int]:
    """Rows per table in a mirror, counted as lines -- no parsing, so it is cheap
    enough to run after every pull."""
    counts: dict[str, int] = {}
    for table in journal.TABLES:
        total = 0
        for path in journal.files_for(table, mirror):
            with path.open(encoding="utf-8") as handle:
                total += sum(1 for line in handle if line.strip())
        counts[table] = total
    return counts


def write_manifest(
    state_dir: Path | str,
    counts: dict[str, int],
    written: dict[str, int],
    dataset_id: str,
) -> Path:
    """The collector's heartbeat, committed beside the frozen git journal.

    .github/workflows/health.yml reads ``updated_at`` from this file to notice a
    collector that has stopped, so it keeps being written every run even though
    the rows now go to Neon.
    """
    path = Path(state_dir) / journal.MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "store": "neon",
        "dataset_id": dataset_id,
        "rows": counts,
        "appended_last_run": written,
        "note": (
            "The dataset lives in Neon, in append-only journal_* tables. The .jsonl "
            "files beside this manifest are the git journal as it stood when the "
            "dataset moved there; they are kept exactly as they were and never "
            "written again. This file is the collector's heartbeat."
        ),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
