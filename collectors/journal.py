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

Why one file per table per day
------------------------------
GitHub refuses a push that contains any file over 100 MiB. The collector commits
the journal after every run, so the first append that carries a file across that
line takes the collector down -- and silently: each later run still collects, fails
to push, and throws its rows away with the runner. That is what happened on
2026-09-20, when ``state/scores.jsonl`` reached 100.52 MB and forty-odd hourly runs
were lost before anyone looked.

So a table is a directory of shards, ``state/<table>/<YYYY-MM-DD>.jsonl``, one per
UTC day of appending, and a shard that would pass :data:`SHARD_MAX_BYTES` rolls
over to ``<YYYY-MM-DD>.1.jsonl`` and so on. No file can reach the limit however
fast a table grows. The single-file journal written before sharding,
``state/<table>.jsonl``, is still read -- first, so append order holds -- and is
never written again: its rows stay exactly where they were recorded.

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
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
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

# Half GitHub's hard limit, and just under the 50 MiB at which it starts warning.
# A day of scores at the current rate is about 19 MB, so a shard normally holds a
# whole day and the rollover exists for the day that does not fit.
GITHUB_FILE_LIMIT_BYTES = 100 * 1024 * 1024
SHARD_MAX_BYTES = 45 * 1024 * 1024

_SHARD_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.(\d+))?\.jsonl$")

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


def legacy_path(table: str, directory: Path | str = DEFAULT_DIR) -> Path:
    """The single-file journal from before sharding. Read first; never written."""
    return Path(directory) / f"{table}.jsonl"


def shard_dir(table: str, directory: Path | str = DEFAULT_DIR) -> Path:
    return Path(directory) / table


def _shard_key(path: Path) -> tuple[str, int] | None:
    """``(day, rollover index)`` for a shard file name, or None for anything else."""
    match = _SHARD_NAME.match(path.name)
    if match is None:
        return None
    return match.group(1), int(match.group(2) or 0)


def _shard_path(table: str, directory: Path | str, day: str, index: int) -> Path:
    suffix = "" if index == 0 else f".{index}"
    return shard_dir(table, directory) / f"{day}{suffix}.jsonl"


def _utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


def files_for(table: str, directory: Path | str = DEFAULT_DIR) -> list[Path]:
    """Every file holding rows of ``table``, in the order they were appended.

    The pre-shard file first, then the shards by day and rollover index. A file in
    the table's directory that is not named like a shard is not journal and is not
    read.
    """
    files: list[Path] = []
    legacy = legacy_path(table, directory)
    if legacy.is_file():
        files.append(legacy)
    folder = shard_dir(table, directory)
    if folder.is_dir():
        shards = sorted(
            (key, path)
            for path in folder.iterdir()
            if path.is_file() and (key := _shard_key(path)) is not None
        )
        files.extend(path for _, path in shards)
    return files


def path_for(table: str, directory: Path | str = DEFAULT_DIR) -> Path:
    """The shard the next append to ``table`` goes to: today's latest rollover."""
    today = _utc_today()
    folder = shard_dir(table, directory)
    indexes = (
        [
            key[1]
            for path in folder.iterdir()
            if (key := _shard_key(path)) is not None and key[0] == today
        ]
        if folder.is_dir()
        else []
    )
    return _shard_path(table, directory, today, max(indexes, default=0))


def read_rows(table: str, directory: Path | str = DEFAULT_DIR) -> list[dict[str, Any]]:
    """Every row of a table, across all its files, in the order it was appended.

    A malformed line is skipped with a warning rather than aborting the read: one
    bad line -- a run killed mid-write -- must not cost the other six weeks.
    """
    rows: list[dict[str, Any]] = []
    for path in files_for(table, directory):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
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
    """Append rows to the table's current shard. Never rewrites, never truncates.

    A shard that would pass :data:`SHARD_MAX_BYTES` is closed and the next one
    opened, so one call can span shards. A row is never split across files.
    """
    rows = list(rows)
    if not rows:
        return 0
    path = path_for(table, directory)
    key = _shard_key(path)
    assert key is not None  # path_for only ever names shards
    day, index = key
    path.parent.mkdir(parents=True, exist_ok=True)
    size = path.stat().st_size if path.exists() else 0
    handle = path.open("a", encoding="utf-8")
    try:
        for row in rows:
            line = (
                json.dumps({k: _encode(v) for k, v in row.items()}, sort_keys=True)
                + "\n"
            )
            length = len(line.encode("utf-8"))
            if size and size + length > SHARD_MAX_BYTES:
                handle.close()
                index += 1
                path = _shard_path(table, directory, day, index)
                size = path.stat().st_size if path.exists() else 0
                handle = path.open("a", encoding="utf-8")
            handle.write(line)
            size += length
    finally:
        handle.close()
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
    # The number that took the collector down once. Kept in the file the commit
    # message is written from, so the next approach to the limit is visible in the
    # diff of an ordinary run rather than in a rejected push.
    largest = max(
        (path.stat().st_size for table in TABLES for path in files_for(table, directory)),
        default=0,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rows": counts,
        "appended_last_run": written or {},
        "largest_file_mb": round(largest / (1024 * 1024), 2),
        "github_file_limit_mb": GITHUB_FILE_LIMIT_BYTES // (1024 * 1024),
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
