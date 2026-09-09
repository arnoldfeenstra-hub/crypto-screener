"""CLI tests.

The command-line surface is how this actually gets run, and it is the layer where a
mistake is least likely to be noticed: the watcher still exits 0, still prints a
summary, and still writes the right rows while doing something subtly wrong in
between. These cover the argument handling that the rest of the suite cannot see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collectors import trigger_watcher
from collectors.snapshot import main as snapshot_main
from collectors.trigger_watcher import main as watcher_main

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "solana_replay.json"


def _run_watcher(tmp_path, *extra: str) -> dict:
    db = tmp_path / "cli.duckdb"
    args = [
        "--replay",
        str(FIXTURE),
        "--cycles",
        "3",
        "--poll-seconds",
        "0",
        "--db",
        str(db),
        *extra,
    ]
    assert watcher_main(args) == 0
    return {"db": db}


class TestPollInterval:
    def test_poll_seconds_zero_is_honoured(self, tmp_path, monkeypatch):
        """Regression: `args.poll_seconds or config.poll_seconds` swallowed 0.

        Zero is the useful value for a replay, and the bug turned a sub-second run
        into two minutes of sleeping while still producing a correct-looking result.
        """
        slept: list[float] = []
        monkeypatch.setattr(trigger_watcher.time, "sleep", slept.append)
        _run_watcher(tmp_path, "--poll-seconds", "0")
        assert slept and set(slept) == {0}

    def test_an_unspecified_interval_falls_back_to_the_configured_one(
        self, tmp_path, monkeypatch
    ):
        slept: list[float] = []
        monkeypatch.setattr(trigger_watcher.time, "sleep", slept.append)
        monkeypatch.setenv("WATCHER_POLL_SECONDS", "7")
        db = tmp_path / "cli2.duckdb"
        assert (
            watcher_main(
                ["--replay", str(FIXTURE), "--cycles", "2", "--db", str(db)]
            )
            == 0
        )
        assert slept == [7]


class TestWatcherCli:
    def test_replay_run_writes_the_expected_rows(self, tmp_path, capsys):
        _run_watcher(tmp_path)
        summary = json.loads(capsys.readouterr().out)
        assert summary["snapshots_total"] == 6
        assert summary["trigger_breakdown"] == {"mcap_250k": 4, "holders_500": 2}
        assert summary["observed"] == 11
        assert summary["races_lost"] == 0

    def test_once_is_a_single_cycle(self, tmp_path, capsys):
        db = tmp_path / "once.duckdb"
        assert watcher_main(["--replay", str(FIXTURE), "--once", "--db", str(db)]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["cycles"] == 1
        assert summary["snapshots_total"] == 3  # only poll 1's three crossings

    def test_dry_run_writes_no_database(self, tmp_path, capsys):
        db = tmp_path / "dry.duckdb"
        assert (
            watcher_main(
                ["--replay", str(FIXTURE), "--cycles", "3", "--poll-seconds", "0",
                 "--dry-run", "--db", str(db)]
            )
            == 0
        )
        summary = json.loads(capsys.readouterr().out)
        assert summary["fired"] == 6
        assert summary["snapshots_total"] == 0

    def test_a_second_run_over_the_same_data_adds_nothing(self, tmp_path, capsys):
        """Re-running the collector is safe. It is not a way to double the dataset."""
        db = tmp_path / "twice.duckdb"
        args = ["--replay", str(FIXTURE), "--cycles", "3", "--poll-seconds", "0",
                "--db", str(db)]
        watcher_main(args)
        capsys.readouterr()
        watcher_main(args)
        summary = json.loads(capsys.readouterr().out)
        assert summary["snapshots_total"] == 6
        assert summary["fired"] == 0
        # 9 of the 11 observations belong to tokens that already fired and are
        # skipped. The other 2 are the token that never crossed: it keeps being
        # re-evaluated, because "not yet" is not "never".
        assert summary["already_triggered"] == 9
        assert summary["observed"] == 11

    def test_export_parquet_partitions_by_date(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("SCREENER_PARQUET_DIR", str(tmp_path / "parquet"))
        _run_watcher(tmp_path, "--export-parquet")
        summary = json.loads(capsys.readouterr().out)
        export = Path(summary["parquet_export"])
        assert list(export.glob("snapshot_date=*"))

    def test_missing_credentials_fail_loudly_rather_than_collecting_nothing(
        self, tmp_path, monkeypatch, capsys
    ):
        """A silent zero-row run would look like a quiet market, not a missing key."""
        monkeypatch.delenv("BITQUERY_TOKEN", raising=False)
        monkeypatch.setattr(trigger_watcher, "load_dotenv", lambda *a, **k: {}, raising=False)
        monkeypatch.setattr(
            "collectors.config.load_dotenv", lambda *a, **k: {}, raising=False
        )
        code = watcher_main(["--chain", "solana", "--once", "--db", str(tmp_path / "x.duckdb")])
        assert code == 2
        assert "BITQUERY_TOKEN" in capsys.readouterr().err


class TestSnapshotCli:
    def test_stats_reports_the_breakdown(self, tmp_path, capsys):
        run = _run_watcher(tmp_path)
        capsys.readouterr()
        assert snapshot_main(["--db", str(run["db"]), "--stats"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["snapshots"] == 6
        assert payload["dates"] == ["2026-09-09"]

    def test_rows_come_back_in_the_section_4_shape(self, tmp_path, capsys):
        run = _run_watcher(tmp_path)
        capsys.readouterr()
        assert snapshot_main(["--db", str(run["db"]), "--limit", "2"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 2
        assert rows[0]["labels"] == {"filled_at": None}
        assert set(rows[0]["market"]) == {
            "mcap_usd",
            "liquidity_usd",
            "volume_24h_usd",
            "price_usd",
        }

    def test_a_missing_database_is_an_error_not_an_empty_result(self, tmp_path, capsys):
        assert snapshot_main(["--db", str(tmp_path / "nope.duckdb")]) == 2
        assert "cannot open" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--help"]])
def test_help_does_not_crash(argv):
    with pytest.raises(SystemExit) as exc:
        watcher_main(argv)
    assert exc.value.code == 0
