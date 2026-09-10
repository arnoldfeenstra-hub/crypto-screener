"""Append-only DuckDB store.

CLAUDE.md hard rule 1: never delete or overwrite a snapshot row. The graveyard is
the dataset; losers are the signal. This module is the enforcement point, and it
enforces in three independent ways:

1. **No mutating SQL exists here.** This module contains no statement that can
   change or remove a row. ``tests/test_store_append_only.py`` parses this file's
   AST, pulls out every string literal, and fails the build if a mutating
   statement appears in any of them. That check cannot be satisfied by convention
   alone -- it reads the code.
2. **The database refuses duplicates.** ``snapshot_id`` is a primary key and
   ``(chain, contract)`` is unique, so a token can be snapshotted exactly once.
   A second attempt raises :class:`AppendOnlyViolation` instead of replacing
   anything. The uniqueness is a constraint, not a convention.
3. **Corrections are new rows.** A snapshot found to be wrong is superseded by a
   later row carrying ``supersedes``; the original stays.

Exports follow the same discipline. Each parquet export is written to a fresh,
timestamped directory partitioned by ``snapshot_date``, so no export ever
overwrites an earlier one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from collectors.schema import (
    LABEL_COLUMNS,
    OUTCOME_OBSERVATION_COLUMNS,
    SAFETY_OBSERVATION_COLUMNS,
    SCHEMA_VERSION,
    SCORE_COLUMNS,
    SNAPSHOT_COLUMNS,
    SOCIAL_OBSERVATION_COLUMNS,
    TRIGGER_EVENT_COLUMNS,
    Snapshot,
    now_ms,
)


class AppendOnlyViolation(RuntimeError):
    """Raised when a write would change or duplicate an existing row."""


class SchemaMismatch(RuntimeError):
    """The file on disk was written by an older schema than this code expects.

    Raised on open rather than on the first insert, so the failure names the cause
    instead of surfacing as a column error halfway through a poll. Nothing is
    migrated in place: this module has no statement that can change a table, by
    design, so a widened schema means a new file. The old file keeps every row it
    had -- the graveyard is the dataset, and it is still there.
    """


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """One token crossing the trigger, once, forever.

    Doubles as the "has this token already fired" index, which is why the watcher
    needs no mutable state of its own to survive a restart.
    """

    chain: str
    contract: str
    trigger_kind: str
    trigger_mcap_crossed: bool
    trigger_holders_crossed: bool
    source: str
    mcap_usd_at_trigger: float | None = None
    holder_count_at_trigger: int | None = None
    snapshot_id: str | None = None
    ts: int | None = None
    event_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id or str(uuid.uuid4()),
            "ts": self.ts if self.ts is not None else now_ms(),
            "chain": self.chain,
            "contract": self.contract,
            "trigger_kind": self.trigger_kind,
            "trigger_mcap_crossed": self.trigger_mcap_crossed,
            "trigger_holders_crossed": self.trigger_holders_crossed,
            "mcap_usd_at_trigger": self.mcap_usd_at_trigger,
            "holder_count_at_trigger": self.holder_count_at_trigger,
            "snapshot_id": self.snapshot_id,
            "source": self.source,
        }


def _ddl(table: str, columns: Sequence[tuple[str, str]], extra: Sequence[str] = ()) -> str:
    body = [f"    {name} {sql_type}" for name, sql_type in columns]
    body.extend(f"    {clause}" for clause in extra)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(body) + "\n)"


SNAPSHOTS_DDL = _ddl(
    "snapshots",
    SNAPSHOT_COLUMNS,
    (
        "PRIMARY KEY (snapshot_id)",
        # One snapshot per token, at first crossing. Enforced by the database so
        # that a watcher restart or a double poll cannot produce a second row.
        "UNIQUE (chain, contract)",
    ),
)

TRIGGER_EVENTS_DDL = _ddl(
    "trigger_events",
    TRIGGER_EVENT_COLUMNS,
    ("PRIMARY KEY (event_id)", "UNIQUE (chain, contract)"),
)

# Labels are appended by collectors/outcomes.py (Phase 0 item 4, not built yet).
# They live apart from snapshots because filling them later would otherwise mean
# editing a snapshot row.
LABELS_DDL = _ddl("labels", LABEL_COLUMNS, ("PRIMARY KEY (label_id)",))

# Every re-price. Append-only, so the price path behind any label stays available
# and a label can be recomputed after a formula change.
OUTCOME_OBSERVATIONS_DDL = _ddl(
    "outcome_observations",
    OUTCOME_OBSERVATION_COLUMNS,
    ("PRIMARY KEY (observation_id)",),
)

# Scored rows. Append-only like everything else: rescoring under new weights writes
# a new row, so the score history under each prompt version stays intact.
SCORES_DDL = _ddl("scores", SCORE_COLUMNS, ("PRIMARY KEY (score_id)",))

# Raw social counts. One row per (snapshot_id, platform, offset); a second
# collection for the same slot is refused rather than replacing the first.
SOCIAL_OBSERVATIONS_DDL = _ddl(
    "social_observations",
    SOCIAL_OBSERVATION_COLUMNS,
    (
        "PRIMARY KEY (observation_id)",
        "UNIQUE (snapshot_id, platform, offset_minutes)",
    ),
)


# Safety lookups. Append-only: re-checking a token writes another row and both
# stay, because the change over time is itself the observation.
SAFETY_OBSERVATIONS_DDL = _ddl(
    "safety_observations",
    SAFETY_OBSERVATION_COLUMNS,
    ("PRIMARY KEY (observation_id)",),
)


def _insert_sql(table: str, columns: Sequence[str]) -> str:
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    return f"INSERT INTO {table} ({names}) VALUES ({placeholders})"


class Store:
    """Append-only handle on the dataset.

    Use as a context manager, or call :meth:`close`. Opening a path whose parent
    does not exist creates the parent; opening ``:memory:`` gives an ephemeral
    database, which is what the tests use.
    """

    def __init__(self, db_path: str | Path = ":memory:", *, read_only: bool = False) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(self.db_path, read_only=read_only)
        if not read_only:
            self._migrate()
        self._verify_schema()

    def _verify_schema(self) -> None:
        """Fail loudly if an existing file predates a column this code writes."""
        existing = {
            str(row[0])
            for row in self._con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'snapshots'"
            ).fetchall()
        }
        if not existing:
            return  # read-only handle on an empty file; nothing to check yet
        missing = [name for name, _ in SNAPSHOT_COLUMNS if name not in existing]
        if missing:
            raise SchemaMismatch(
                f"{self.db_path} was written under an older schema and has no "
                f"{', '.join(missing)} column(s). This module never rewrites a table, "
                f"so point --db at a new file (schema version {SCHEMA_VERSION}); the "
                "existing file keeps all of its rows."
            )

    def _migrate(self) -> None:
        for statement in (
            SNAPSHOTS_DDL,
            TRIGGER_EVENTS_DDL,
            LABELS_DDL,
            OUTCOME_OBSERVATIONS_DDL,
            SCORES_DDL,
            SOCIAL_OBSERVATIONS_DDL,
            SAFETY_OBSERVATIONS_DDL,
        ):
            self._con.execute(statement)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        self._con.execute("BEGIN TRANSACTION")
        try:
            yield self._con
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")

    # -- writes ------------------------------------------------------------

    def append_snapshot(self, snapshot: Snapshot) -> str:
        """Insert one snapshot. Never overwrites; raises if the token already has one."""
        with self._transaction() as con:
            self._insert_snapshot(con, snapshot)
        return snapshot.snapshot_id

    def append_trigger_event(self, event: TriggerEvent) -> str:
        with self._transaction() as con:
            return self._insert_trigger_event(con, event)

    def append_trigger(self, snapshot: Snapshot, event: TriggerEvent) -> str:
        """Insert the snapshot and its trigger event atomically.

        The two must land together. If only the snapshot survived, the next poll
        would see an untriggered token and try again; if only the event survived,
        the token would be marked done with nothing recorded.
        """
        with self._transaction() as con:
            self._insert_snapshot(con, snapshot)
            self._insert_trigger_event(con, event)
        return snapshot.snapshot_id

    def _insert_snapshot(self, con: duckdb.DuckDBPyConnection, snapshot: Snapshot) -> None:
        row = snapshot.to_row()
        row["snapshot_date"] = date.fromisoformat(row["snapshot_date"])
        columns = [name for name, _ in SNAPSHOT_COLUMNS]
        values = [row[name] for name in columns]
        try:
            con.execute(_insert_sql("snapshots", columns), values)
        except duckdb.ConstraintException as exc:
            raise AppendOnlyViolation(
                f"{snapshot.chain}:{snapshot.contract} already has a snapshot. "
                "A token is snapshotted once, at first trigger crossing "
                "(BUILD_BRIEF.md section 3). To correct a bad row, append a new one "
                "with supersedes set."
            ) from exc

    def _insert_trigger_event(
        self, con: duckdb.DuckDBPyConnection, event: TriggerEvent
    ) -> str:
        row = event.to_row()
        columns = [name for name, _ in TRIGGER_EVENT_COLUMNS]
        try:
            con.execute(_insert_sql("trigger_events", columns), [row[c] for c in columns])
        except duckdb.ConstraintException as exc:
            raise AppendOnlyViolation(
                f"{event.chain}:{event.contract} has already fired its trigger."
            ) from exc
        return str(row["event_id"])

    def append_outcome_observations(self, observations: Iterable[Any]) -> int:
        """Append re-price rows. Each is a new row; none replaces an earlier one."""
        columns = [name for name, _ in OUTCOME_OBSERVATION_COLUMNS]
        written = 0
        with self._transaction() as con:
            for observation in observations:
                snapshot_ts = self._snapshot_ts(observation.snapshot_id)
                if snapshot_ts is None:
                    raise AppendOnlyViolation(
                        f"no snapshot {observation.snapshot_id}; an outcome row must "
                        "point at a snapshot that exists"
                    )
                row = observation.to_row(snapshot_ts)
                try:
                    con.execute(
                        _insert_sql("outcome_observations", columns),
                        [row[name] for name in columns],
                    )
                except duckdb.ConstraintException as exc:
                    raise AppendOnlyViolation(
                        f"outcome observation {row['observation_id']} already recorded"
                    ) from exc
                written += 1
        return written

    def append_labels(self, labels: Any) -> str:
        """Append one label row.

        Recomputing at a later horizon appends another row rather than editing this
        one, so the history of what was believed and when stays intact.
        """
        columns = [name for name, _ in LABEL_COLUMNS]
        row = labels.to_row()
        with self._transaction() as con:
            try:
                con.execute(
                    _insert_sql("labels", columns), [row[name] for name in columns]
                )
            except duckdb.ConstraintException as exc:
                raise AppendOnlyViolation(f"label {row['label_id']} already written") from exc
        return str(row["label_id"])

    def append_scores(self, rows: Iterable[dict[str, Any]]) -> int:
        """Append scored rows. Rescoring writes new rows; it never edits old ones."""
        columns = [name for name, _ in SCORE_COLUMNS]
        written = 0
        with self._transaction() as con:
            for row in rows:
                try:
                    con.execute(
                        _insert_sql("scores", columns),
                        [row.get(name) for name in columns],
                    )
                except duckdb.ConstraintException as exc:
                    raise AppendOnlyViolation(
                        f"score {row.get('score_id')} already written"
                    ) from exc
                written += 1
        return written

    def append_safety_observations(
        self, reports: Iterable[Any], snapshot_ids: dict[tuple[str, str], str] | None = None
    ) -> int:
        """Append safety lookups. Each is a new row; none replaces an earlier one."""
        columns = [name for name, _ in SAFETY_OBSERVATION_COLUMNS]
        ids = snapshot_ids or {}
        written = 0
        with self._transaction() as con:
            for report in reports:
                row = report.to_row(ids.get((report.chain, report.contract)))
                row.setdefault("observation_id", str(uuid.uuid4()))
                try:
                    con.execute(
                        _insert_sql("safety_observations", columns),
                        [row.get(name) for name in columns],
                    )
                except duckdb.ConstraintException as exc:
                    raise AppendOnlyViolation(
                        f"safety observation {row['observation_id']} already recorded"
                    ) from exc
                written += 1
        return written

    def latest_safety(self, snapshot_id: str) -> dict[str, Any] | None:
        """The most recent safety row for a snapshot. Earlier rows stay in place."""
        columns = [name for name, _ in SAFETY_OBSERVATION_COLUMNS]
        row = self._con.execute(
            f"SELECT {', '.join(columns)} FROM safety_observations "
            "WHERE snapshot_id = ? ORDER BY ts DESC LIMIT 1",
            [snapshot_id],
        ).fetchone()
        return dict(zip(columns, row, strict=True)) if row else None

    def safety_observation_count(self) -> int:
        row = self._con.execute("SELECT count(*) FROM safety_observations").fetchone()
        return int(row[0]) if row else 0

    def snapshots_with_safety(self) -> int:
        row = self._con.execute(
            "SELECT count(DISTINCT snapshot_id) FROM safety_observations "
            "WHERE snapshot_id IS NOT NULL"
        ).fetchone()
        return int(row[0]) if row else 0

    def append_social_observations(self, observations: Iterable[Any]) -> int:
        """Append raw social counts. One row per (snapshot, platform, offset)."""
        columns = [name for name, _ in SOCIAL_OBSERVATION_COLUMNS]
        written = 0
        with self._transaction() as con:
            for observation in observations:
                row = observation.to_row()
                try:
                    con.execute(
                        _insert_sql("social_observations", columns),
                        [row[name] for name in columns],
                    )
                except duckdb.ConstraintException as exc:
                    raise AppendOnlyViolation(
                        f"{row['platform']} offset {row['offset_minutes']} already "
                        f"collected for snapshot {row['snapshot_id']}"
                    ) from exc
                written += 1
        return written

    # -- reads -------------------------------------------------------------

    def social_offsets_collected(self, snapshot_id: str, platform: str) -> set[int]:
        rows = self._con.execute(
            "SELECT offset_minutes FROM social_observations "
            "WHERE snapshot_id = ? AND platform = ?",
            [snapshot_id, platform],
        ).fetchall()
        return {int(row[0]) for row in rows}

    def social_observations(self, snapshot_id: str) -> list[Any]:
        from collectors.social_base import SocialObservation

        columns = [name for name, _ in SOCIAL_OBSERVATION_COLUMNS]
        rows = self._con.execute(
            f"SELECT {', '.join(columns)} FROM social_observations "
            "WHERE snapshot_id = ? ORDER BY platform, offset_minutes",
            [snapshot_id],
        ).fetchall()
        return [
            SocialObservation(**dict(zip(columns, row, strict=True))) for row in rows
        ]

    def social_observation_count(self) -> int:
        row = self._con.execute("SELECT count(*) FROM social_observations").fetchone()
        return int(row[0]) if row else 0

    def snapshots_with_complete_social(self) -> int:
        """Snapshots carrying a count at every scheduled offset on both platforms.

        This is what Phase 0's exit criterion means by "complete social series", and
        rows whose only social entries are recorded failures do not count.
        """
        from collectors.social_base import OFFSETS_MINUTES

        row = self._con.execute(
            "SELECT count(*) FROM ("
            "  SELECT snapshot_id FROM social_observations "
            "  WHERE error IS NULL GROUP BY snapshot_id "
            "  HAVING count(DISTINCT platform || ':' || offset_minutes) >= ?"
            ")",
            [len(OFFSETS_MINUTES) * 2],
        ).fetchone()
        return int(row[0]) if row else 0

    def _snapshot_ts(self, snapshot_id: str) -> int | None:
        row = self._con.execute(
            "SELECT ts FROM snapshots WHERE snapshot_id = ?", [snapshot_id]
        ).fetchone()
        return int(row[0]) if row else None

    def snapshots_for_labelling(self) -> list[dict[str, Any]]:
        """Snapshots with the baseline the label maths needs."""
        rows = self._con.execute(
            "SELECT snapshot_id, chain, contract, ticker, ts, market_mcap_usd "
            "FROM snapshots ORDER BY ts"
        ).fetchall()
        keys = ("snapshot_id", "chain", "contract", "ticker", "ts", "market_mcap_usd")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def outcome_observations(self, snapshot_id: str) -> list[Any]:
        from collectors.outcomes import PriceObservation

        rows = self._con.execute(
            "SELECT observation_id, snapshot_id, ts, mcap_usd, price_usd, liquidity_usd, "
            "volume_24h_usd, holder_count, source FROM outcome_observations "
            "WHERE snapshot_id = ? ORDER BY ts",
            [snapshot_id],
        ).fetchall()
        return [
            PriceObservation(
                observation_id=row[0],
                snapshot_id=row[1],
                ts=int(row[2]),
                mcap_usd=row[3],
                price_usd=row[4],
                liquidity_usd=row[5],
                volume_24h_usd=row[6],
                holder_count=row[7],
                source=row[8],
            )
            for row in rows
        ]

    def last_observation_ts(self, snapshot_id: str) -> int | None:
        row = self._con.execute(
            "SELECT max(ts) FROM outcome_observations WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def latest_labels(self, snapshot_id: str) -> dict[str, Any] | None:
        """The most recent label row for a snapshot. Older rows stay in place."""
        columns = [name for name, _ in LABEL_COLUMNS]
        row = self._con.execute(
            f"SELECT {', '.join(columns)} FROM labels WHERE snapshot_id = ? "
            "ORDER BY filled_at_ms DESC LIMIT 1",
            [snapshot_id],
        ).fetchone()
        return dict(zip(columns, row, strict=True)) if row else None

    def latest_scores(self, limit: int = 100) -> list[dict[str, Any]]:
        """The most recent scoring run's rows, best first.

        Only the newest ``scored_at_ms`` is returned: older rows stay in the table
        as history, but a ranking built from several runs at once would be mixing
        prompt versions.
        """
        columns = [name for name, _ in SCORE_COLUMNS]
        row = self._con.execute(
            "SELECT run_id FROM scores ORDER BY scored_at_ms DESC, run_id LIMIT 1"
        ).fetchone()
        if not row or row[0] is None:
            return []
        rows = self._con.execute(
            f"SELECT {', '.join(columns)} FROM scores WHERE run_id = ? "
            "ORDER BY score DESC NULLS LAST LIMIT ?",
            [row[0], int(limit)],
        ).fetchall()
        return [dict(zip(columns, r, strict=True)) for r in rows]

    def all_scores(self) -> list[dict[str, Any]]:
        """Every scored row, oldest first. Used to build calibration training rows."""
        columns = [name for name, _ in SCORE_COLUMNS]
        rows = self._con.execute(
            f"SELECT {', '.join(columns)} FROM scores ORDER BY scored_at_ms"
        ).fetchall()
        return [dict(zip(columns, r, strict=True)) for r in rows]

    def score_count(self) -> int:
        row = self._con.execute("SELECT count(*) FROM scores").fetchone()
        return int(row[0]) if row else 0

    def labelled_snapshot_count(self) -> int:
        row = self._con.execute("SELECT count(DISTINCT snapshot_id) FROM labels").fetchone()
        return int(row[0]) if row else 0

    def outcome_observation_count(self) -> int:
        row = self._con.execute("SELECT count(*) FROM outcome_observations").fetchone()
        return int(row[0]) if row else 0

    def survivor_counts(self, horizon: str = "7d") -> tuple[int, int]:
        """``(survivors, dead)`` at a horizon, over the most recent label per snapshot.

        Feeds the Phase 0 exit criteria. Snapshots whose label has not resolved are
        in neither count -- unresolved is not dead.
        """
        column = f"survived_{horizon}"
        rows = self._con.execute(
            f"SELECT l.{column} FROM labels l JOIN ("
            "  SELECT snapshot_id, max(filled_at_ms) AS latest FROM labels GROUP BY snapshot_id"
            ") m ON l.snapshot_id = m.snapshot_id AND l.filled_at_ms = m.latest"
        ).fetchall()
        survivors = sum(1 for row in rows if row[0] is True)
        dead = sum(1 for row in rows if row[0] is False)
        return survivors, dead

    def has_triggered(self, chain: str, contract: str) -> bool:
        result = self._con.execute(
            "SELECT 1 FROM trigger_events WHERE chain = ? AND contract = ? LIMIT 1",
            [chain, contract],
        ).fetchone()
        return result is not None

    def triggered_contracts(self, chain: str | None = None) -> set[str]:
        """Contracts that have already fired. The watcher loads this once per cycle."""
        if chain is None:
            rows = self._con.execute("SELECT contract FROM trigger_events").fetchall()
        else:
            rows = self._con.execute(
                "SELECT contract FROM trigger_events WHERE chain = ?", [chain]
            ).fetchall()
        return {row[0] for row in rows}

    def snapshot_count(self) -> int:
        row = self._con.execute("SELECT count(*) FROM snapshots").fetchone()
        return int(row[0]) if row else 0

    def fetch_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        columns = [name for name, _ in SNAPSHOT_COLUMNS]
        row = self._con.execute(
            f"SELECT {', '.join(columns)} FROM snapshots WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchone()
        return dict(zip(columns, row, strict=True)) if row else None

    def fetch_by_contract(self, chain: str, contract: str) -> dict[str, Any] | None:
        columns = [name for name, _ in SNAPSHOT_COLUMNS]
        row = self._con.execute(
            f"SELECT {', '.join(columns)} FROM snapshots WHERE chain = ? AND contract = ?",
            [chain, contract],
        ).fetchone()
        return dict(zip(columns, row, strict=True)) if row else None

    def recent_snapshots(self, limit: int = 5) -> list[dict[str, Any]]:
        """Most recent rows, newest first. Read-only; used by the inspection CLI."""
        columns = [name for name, _ in SNAPSHOT_COLUMNS]
        rows = self._con.execute(
            f"SELECT {', '.join(columns)} FROM snapshots ORDER BY ts DESC LIMIT ?",
            [int(limit)],
        ).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def snapshot_dates(self) -> list[str]:
        rows = self._con.execute(
            "SELECT DISTINCT snapshot_date FROM snapshots ORDER BY snapshot_date"
        ).fetchall()
        return [row[0].isoformat() for row in rows]

    def chain_breakdown(self) -> dict[str, int]:
        """Snapshots per chain, most first. Feeds the web page's chain filter."""
        rows = self._con.execute(
            "SELECT chain, count(*) AS n FROM snapshots GROUP BY chain "
            "ORDER BY n DESC, chain"
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def trigger_breakdown(self) -> dict[str, int]:
        rows = self._con.execute(
            "SELECT trigger_kind, count(*) FROM snapshots GROUP BY trigger_kind"
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    # -- journal round trip ------------------------------------------------
    #
    # Two generic accessors, used only by collectors/journal.py, which keeps the
    # dataset in append-only JSONL so a scheduled collector can carry state across
    # runs in git. Both are read-or-insert: there is no statement here that can
    # change or remove an existing row, same as everywhere else in this module.

    def export_rows(self, table: str, columns: Sequence[str]) -> list[dict[str, Any]]:
        """Every row of one table, oldest first where the table has an order."""
        rows = self._con.execute(
            f"SELECT {', '.join(columns)} FROM {table}"
        ).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def import_rows(
        self, table: str, columns: Sequence[str], rows: Iterable[dict[str, Any]]
    ) -> int:
        """Insert journalled rows into an empty table.

        A row the table already holds is skipped rather than raising: restoring a
        journal onto a database that already has some of it is a resumed run, not
        an error. Nothing existing is touched either way.
        """
        written = 0
        with self._transaction() as con:
            for row in rows:
                try:
                    con.execute(
                        _insert_sql(table, columns), [row.get(name) for name in columns]
                    )
                except duckdb.ConstraintException:
                    continue
                written += 1
        return written

    # -- export ------------------------------------------------------------

    def export_parquet(self, out_dir: str | Path) -> Path:
        """Write the whole dataset to a fresh timestamped directory.

        Partitioned by ``snapshot_date`` (CLAUDE.md conventions). Each export goes
        to its own directory rather than merging into a shared one, so an export
        never overwrites files an earlier export wrote.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = Path(out_dir) / f"export_{stamp}"
        target = base
        # Two exports in the same second still get their own directory rather than
        # one landing on top of the other.
        suffix = 1
        while target.exists():
            target = base.with_name(f"{base.name}_{suffix}")
            suffix += 1
        target.mkdir(parents=True, exist_ok=False)
        self._con.execute(
            "COPY (SELECT * FROM snapshots) TO ? "
            "(FORMAT PARQUET, PARTITION_BY (snapshot_date))",
            [str(target)],
        )
        return target
