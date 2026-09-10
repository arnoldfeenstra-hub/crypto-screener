"""Append-only JSONL journal -- the dataset, in a form git can hold.

Phase 0 needs four to six weeks of forward collection and nothing was running it.
A scheduled job is the obvious fix, and every scheduled job needs somewhere to keep
state between runs. This is that somewhere.

Why JSONL and not the DuckDB file
---------------------------------
Committing ``screener.duckdb`` would work and would be wrong in a way that grows.
It is one binary blob rewritten in full on every run, so a job firing twice an hour
adds a fresh multi-megabyte object to git history every time, and the repository
becomes mostly history of a file nobody can read.

A JSONL journal is the same data with the properties this dataset already claims:

* **Append-only in the file format itself.** New rows are lines added at the end.
  Nothing earlier is rewritten -- which is hard rule 1 expressed as a file layout
  rather than as a promise about SQL.
* **Cheap in git.** Appending to a text file is a small delta. Six weeks of
  collection costs a fraction of what six weeks of binary snapshots would.
* **Readable.** A row that looks wrong can be found with ``grep`` and read by a
  human, without DuckDB installed.

The database is rebuilt from the journal at the start of a run and the new rows are
appended back at the end, so the journal is the durable dataset and the database is
a working copy of it.

What "append" means here
------------------------
:func:`sync` reads the primary keys already in the file and writes only rows whose
key is absent. It never rewrites a line and never removes one. A row already
journalled is skipped even if the working database now holds something different
under that key -- and that is the point: if those ever diverge, the journal keeps
what was recorded first, and the divergence is a bug worth finding rather than a
silent overwrite.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from collectors.schema import (
    LABEL_COLUMNS,
    OUTCOME_OBSERVATION_COLUMNS,
    SAFETY_OBSERVATION_COLUMNS,
    SCHEMA_VERSION,
    SCORE_COLUMNS,
    SNAPSHOT_COLUMNS,
    SOCIAL_OBSERVATION_COLUMNS,
    TRIGGER_EVENT_COLUMNS,
)

log = logging.getLogger("journal")

DEFAULT_DIR = Path("state")
MANIFEST_NAME = "manifest.json"

# Table -> (columns, primary key). Every table the collector writes is here; a
# table missing from this list would silently not survive a restart, so the test
# suite checks this covers everything the store creates.
TABLES: dict[str, tuple[list[tuple[str, str]], str]] = {
    "snapshots": (SNAPSHOT_COLUMNS, "snapshot_id"),
    "trigger_events": (TRIGGER_EVENT_COLUMNS, "event_id"),
    "labels": (LABEL_COLUMNS, "label_id"),
    "outcome_observations": (OUTCOME_OBSERVATION_COLUMNS, "observation_id"),
    "scores": (SCORE_COLUMNS, "score_id"),
    "social_observations": (SOCIAL_OBSERVATION_COLUMNS, "observation_id"),
    "safety_observations": (SAFETY_OBSERVATION_COLUMNS, "observation_id"),
}


def _encode(value: Any) -> Any:
    """JSON-safe form of one cell. Dates become ISO strings; everything else is
    already a JSON scalar or a JSON string from the store."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _decode(value: Any, sql_type: str) -> Any:
    if value is None:
        return None
    if sql_type == "DATE" and isinstance(value, str):
        return date.fromisoformat(value)
    return value


def path_for(table: str, directory: Path | str = DEFAULT_DIR) -> Path:
    return Path(directory) / f"{table}.jsonl"


def read_rows(table: str, directory: Path | str = DEFAULT_DIR) -> list[dict[str, Any]]:
    """Every row in a journal file, in the order it was appended.

    A malformed line is skipped with a warning rather than aborting the read: one
    bad line -- a run killed mid-write -- must not cost the other six weeks.
    """
    path = path_for(table, directory)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            log.warning("skipping malformed line %d of %s", number, path)
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def existing_keys(table: str, directory: Path | str = DEFAULT_DIR) -> set[str]:
    _, key = TABLES[table]
    return {str(row.get(key)) for row in read_rows(table, directory) if row.get(key)}


def append_rows(
    table: str, rows: Iterable[dict[str, Any]], directory: Path | str = DEFAULT_DIR
) -> int:
    """Append rows to a journal file. Never rewrites, never truncates."""
    rows = list(rows)
    if not rows:
        return 0
    path = path_for(table, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps({k: _encode(v) for k, v in row.items()}, sort_keys=True) + "\n"
            )
    return len(rows)


def sync(store: Any, directory: Path | str = DEFAULT_DIR) -> dict[str, int]:
    """Append every row the working database holds that the journal does not.

    Returns the count written per table. A row already journalled is left exactly
    as it was recorded.
    """
    written: dict[str, int] = {}
    for table, (columns, key) in TABLES.items():
        seen = existing_keys(table, directory)
        fresh = [
            row
            for row in store.export_rows(table, [name for name, _ in columns])
            if str(row.get(key)) not in seen
        ]
        count = append_rows(table, fresh, directory)
        if count:
            written[table] = count
    write_manifest(store, directory, written)
    return written


def restore(store: Any, directory: Path | str = DEFAULT_DIR) -> dict[str, int]:
    """Load a journal into an empty store. Returns the count loaded per table.

    Order matters: snapshots first, because the outcome table refuses a row whose
    snapshot does not exist.
    """
    loaded: dict[str, int] = {}
    for table, (columns, _) in TABLES.items():
        rows = read_rows(table, directory)
        if not rows:
            continue
        typed = [
            {name: _decode(row.get(name), sql_type) for name, sql_type in columns}
            for row in rows
        ]
        loaded[table] = store.import_rows(table, [name for name, _ in columns], typed)
    return loaded


def write_manifest(
    store: Any, directory: Path | str = DEFAULT_DIR, written: dict[str, int] | None = None
) -> Path:
    """A small human-readable summary beside the journal.

    Not load-bearing -- ``restore`` reads the ``.jsonl`` files, not this -- but a
    scheduled job that commits its own state should say in the commit what the
    state now is, and this is what the commit message reads.
    """
    path = Path(directory) / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {
        table: len(read_rows(table, directory)) for table in TABLES
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rows": counts,
        "appended_last_run": written or {},
        "note": (
            "Append-only journal of the Phase 0 dataset. Rebuilt into DuckDB at the "
            "start of each collector run and appended to at the end. Never edit or "
            "reorder these files: the dead tokens are the dataset."
        ),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def summarise(directory: Path | str = DEFAULT_DIR) -> dict[str, int]:
    return {table: len(read_rows(table, directory)) for table in TABLES}


def restore_into_path(
    db_path: str | Path, directory: Path | str = DEFAULT_DIR
) -> tuple[Any, dict[str, int]]:
    """Open a database at ``db_path`` and load the journal into it."""
    from collectors.store import Store

    store = Store(db_path)
    return store, restore(store, directory)


def _table_columns(table: str) -> Sequence[str]:
    return [name for name, _ in TABLES[table][0]]
