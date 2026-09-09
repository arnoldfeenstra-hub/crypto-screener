"""Append-only tests.

BUILD_BRIEF.md section 6 asks for these alongside the trigger tests: "that snapshots
are append-only".

CLAUDE.md hard rule 1 is the reason. The graveyard is the dataset -- the dead tokens
are the negative class, and prompts/score.md step 5 needs twenty of them per
survivor. Any pruning, dedupe or "fix up that bad row" destroys the thing being
built, and destroys it quietly, because a smaller dataset still trains a model.

Three independent checks below, because a convention that only lives in review will
eventually lose an argument to a deadline:

1. The module contains no mutating SQL (checked by reading its own source).
2. The database rejects a second row for the same token.
3. A rejected write leaves the existing row untouched.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest

from collectors.schema import Market, Snapshot
from collectors.store import AppendOnlyViolation, Store, TriggerEvent

STORE_PATH = Path(__file__).resolve().parent.parent / "collectors" / "store.py"

# Statements that can change or remove a row that already exists. ROLLBACK is not
# here: it abandons an uncommitted write, which is the opposite of the problem.
MUTATING_SQL = [
    r"\bUPDATE\b",
    r"\bDELETE\b",
    r"\bDROP\b",
    r"\bTRUNCATE\b",
    r"\bALTER\b",
    r"\bREPLACE\s+INTO\b",
    r"\bINSERT\s+OR\s+REPLACE\b",
    r"\bON\s+CONFLICT\b",
    r"\bUPSERT\b",
]


def snapshot(contract: str = "Tok1", mcap: float | None = 260_000.0, **kwargs) -> Snapshot:
    return Snapshot(
        chain=kwargs.pop("chain", "solana"),
        contract=contract,
        trigger=kwargs.pop("trigger", "mcap_250k"),
        source="test",
        market=Market(mcap_usd=mcap),
        **kwargs,
    )


def event(contract: str = "Tok1", snapshot_id: str | None = None, **kwargs) -> TriggerEvent:
    return TriggerEvent(
        chain=kwargs.pop("chain", "solana"),
        contract=contract,
        trigger_kind="mcap_250k",
        trigger_mcap_crossed=True,
        trigger_holders_crossed=False,
        source="test",
        snapshot_id=snapshot_id,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. No mutating SQL exists in the module
# ---------------------------------------------------------------------------


def _sql_literals(path: Path) -> list[str]:
    """Every string literal in the module except docstrings.

    Docstrings are excluded so prose about never deleting rows does not fail a test
    about never deleting rows.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


@pytest.mark.parametrize("pattern", MUTATING_SQL)
def test_store_module_contains_no_mutating_sql(pattern):
    """Read the store's own source and fail if it learns to modify a row."""
    offenders = [
        literal
        for literal in _sql_literals(STORE_PATH)
        if re.search(pattern, literal, re.IGNORECASE)
    ]
    assert not offenders, f"{pattern} found in store.py: {offenders}"


def test_the_check_would_actually_catch_something():
    """Guard the guard: a scanner that matches nothing proves nothing."""
    assert any(
        re.search(r"\bUPDATE\b", literal, re.IGNORECASE)
        for literal in ["UPDATE snapshots SET mcap_usd = 1"]
    )
    assert _sql_literals(STORE_PATH), "no literals extracted -- the scan is not running"


# ---------------------------------------------------------------------------
# 2. The database refuses a second row for the same token
# ---------------------------------------------------------------------------


class TestOneRowPerToken:
    def test_second_snapshot_for_the_same_token_is_refused(self):
        with Store() as store:
            store.append_snapshot(snapshot())
            with pytest.raises(AppendOnlyViolation):
                store.append_snapshot(snapshot(mcap=999_000.0))
            assert store.snapshot_count() == 1

    def test_refusal_leaves_the_original_row_exactly_as_it_was(self):
        with Store() as store:
            first_id = store.append_snapshot(snapshot(mcap=260_000.0))
            before = store.fetch_snapshot(first_id)
            with pytest.raises(AppendOnlyViolation):
                store.append_snapshot(snapshot(mcap=1.0, trigger="holders_500"))
            assert store.fetch_snapshot(first_id) == before

    def test_a_refused_write_leaves_the_other_rows_alone(self):
        with Store() as store:
            store.append_snapshot(snapshot("TokA"))
            store.append_snapshot(snapshot("TokB"))
            with pytest.raises(AppendOnlyViolation):
                store.append_snapshot(snapshot("TokA"))
            assert store.snapshot_count() == 2

    def test_row_count_only_ever_goes_up(self):
        with Store() as store:
            counts = [store.snapshot_count()]
            for i in range(10):
                store.append_snapshot(snapshot(f"Tok{i}"))
                counts.append(store.snapshot_count())
            with pytest.raises(AppendOnlyViolation):
                store.append_snapshot(snapshot("Tok3"))
            counts.append(store.snapshot_count())
            assert counts == sorted(counts)
            assert counts[-1] == 10

    def test_the_same_token_on_another_chain_is_a_different_token(self):
        with Store() as store:
            store.append_snapshot(snapshot("Tok1", chain="solana"))
            store.append_snapshot(snapshot("Tok1", chain="bnb"))
            assert store.snapshot_count() == 2

    def test_trigger_event_and_snapshot_land_together_or_not_at_all(self):
        """A half-written trigger is worse than none: it silently drops a token."""
        with Store() as store:
            store.append_trigger(snapshot("TokA"), event("TokA"))
            with pytest.raises(AppendOnlyViolation):
                store.append_trigger(snapshot("TokA"), event("TokA"))
            assert store.snapshot_count() == 1
            assert store.triggered_contracts() == {"TokA"}

    def test_a_failed_event_write_does_not_leave_an_orphan_snapshot(self):
        with Store() as store:
            store.append_trigger_event(event("TokA"))
            with pytest.raises(AppendOnlyViolation):
                store.append_trigger(snapshot("TokA"), event("TokA"))
            # The snapshot insert succeeded inside the transaction, the event did
            # not, so the whole thing is gone.
            assert store.snapshot_count() == 0


# ---------------------------------------------------------------------------
# 3. Rows are immutable in memory too
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_snapshot_objects_are_frozen(self):
        snap = snapshot()
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.ticker = "$CHANGED"  # type: ignore[misc]

    def test_nested_groups_are_frozen_too(self):
        snap = snapshot()
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.market.mcap_usd = 1.0  # type: ignore[misc]

    def test_a_correction_is_a_new_row_pointing_at_the_old_one(self):
        """The supported way to fix a bad row, per .claude/rules/data-integrity.md."""
        with Store() as store:
            original_id = store.append_snapshot(snapshot("TokA", mcap=260_000.0))
            correction = snapshot(
                "TokA", chain="solana-corrected", mcap=261_500.0, supersedes=original_id
            )
            store.append_snapshot(correction)
            assert store.snapshot_count() == 2
            original = store.fetch_snapshot(original_id)
            assert original is not None
            assert original["market_mcap_usd"] == 260_000.0  # untouched
            assert store.fetch_snapshot(correction.snapshot_id)["supersedes"] == original_id


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_export_is_partitioned_by_snapshot_date(self, tmp_path):
        with Store() as store:
            store.append_snapshot(snapshot("TokA"))
            target = store.export_parquet(tmp_path)
        partitions = list(target.glob("snapshot_date=*"))
        assert partitions, f"no date partitions under {target}"

    def test_a_second_export_never_overwrites_the_first(self, tmp_path):
        with Store() as store:
            store.append_snapshot(snapshot("TokA"))
            first = store.export_parquet(tmp_path)
            store.append_snapshot(snapshot("TokB"))
            second = store.export_parquet(tmp_path)
        assert first != second
        assert first.exists() and second.exists()
