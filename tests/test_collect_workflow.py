"""collect.yml's commit step, run for real against throwaway git repositories.

Run 323 (2026-09-25) is why this file exists. It was queued behind run 322, and
actions/checkout gave it the commit that was the tip when it was queued, not when
it started. So it rebuilt from a journal missing run 322's rows and appended to
the same day's shards. The rebase in the commit step stopped on the conflict, the
retries refused to pull over unmerged files, and the final push sent the
half-rebased HEAD -- run 322's commit, already on main. "Everything up-to-date",
exit 0, a green run, and an hour of rows gone with the runner.

Every test here runs the step's own script, parsed out of the workflow, in a
depth-1 clone like the one actions/checkout leaves. What passes here is what runs
in Actions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "collect.yml"

SHARD = "state/scores/2026-09-25.jsonl"
NEW_SHARD = "state/labels/2026-09-25.jsonl"  # created by both runs: an add/add
MANIFEST = "state/manifest.json"
EXPORT = "web/screener-data.json"
KEPT = "collect-unpushed/323-1"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="runs the workflow's shell script against real git repositories",
)

# Git as a runner has it: nothing from this machine's global or system config (a
# signing key, a default branch, a pull strategy) reaches these repositories.
ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")} | {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["collect"]["steps"]


def step(name: str) -> dict:
    (found,) = [s for s in steps() if s.get("name") == name]
    return found


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout


def write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def append(root: Path, path: str, *rows: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.writelines(row + "\n" for row in rows)


def commit_as(clone: Path, who: str, message: str) -> None:
    git(clone, "add", "-A")
    git(clone, "-c", f"user.name={who}", "-c", f"user.email={who}@example.com",
        "commit", "-q", "-m", message)


def on_main(origin: Path, path: str, ref: str = "main") -> list[str]:
    return git(origin, "show", f"{ref}:{path}").splitlines()


def tip(origin: Path, ref: str = "main") -> str:
    return git(origin, "rev-parse", ref).strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """The repository as the runs find it, with the real .gitattributes."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    shutil.copy(REPO_ROOT / ".gitattributes", seed / ".gitattributes")
    append(seed, SHARD, '{"score_id": "base-1"}', '{"score_id": "base-2"}')
    write(seed, MANIFEST, '{"heartbeat": "base"}\n')
    write(seed, EXPORT, '{"export": "base"}\n')
    write(seed, "state/README.txt", "base\n")
    commit_as(seed, "seed", "seed")
    git(seed, "push", "-q", str(bare), "HEAD:refs/heads/main")
    return bare


def checkout(origin: Path, where: Path) -> Path:
    """What actions/checkout leaves: a depth-1 clone of the branch."""
    git(where.parent, "clone", "-q", "--depth", "1", "--branch", "main",
        origin.as_uri(), str(where))
    return where


def run_322_lands(origin: Path, tmp_path: Path) -> str:
    """The run ahead in the queue: collects, commits and pushes first."""
    clone = checkout(origin, tmp_path / "run-322")
    append(clone, SHARD, '{"score_id": "322-a"}', '{"score_id": "322-b"}')
    append(clone, NEW_SHARD, '{"label_id": "322-l"}')
    write(clone, MANIFEST, '{"heartbeat": "322"}\n')
    write(clone, EXPORT, '{"export": "322"}\n')
    commit_as(clone, "run-322", "collect: run 322")
    git(clone, "push", "-q", "origin", "HEAD:main")
    return git(clone, "rev-parse", "HEAD").strip()


def collect(clone: Path) -> None:
    """What run 323's collector leaves in its working tree."""
    append(clone, SHARD, '{"score_id": "323-a"}')
    append(clone, NEW_SHARD, '{"label_id": "323-l"}')
    write(clone, MANIFEST, '{"heartbeat": "323"}\n')
    write(clone, EXPORT, '{"export": "323"}\n')


def commit_step(clone: Path) -> subprocess.CompletedProcess:
    script = clone.parent / f"{clone.name}-commit.sh"
    script.write_text(step("Commit the journal")["run"], encoding="utf-8")
    env = ENV | {
        # The step's own env block, filled in as Actions would.
        "SUMMARY": "observed=41 fired=1",
        "RETRY_SECONDS": "0",
        # Set by Actions on every job.
        "GITHUB_REF_NAME": "main",
        "GITHUB_RUN_ID": "323",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    # A `run:` with no `shell:` runs as `bash -e {0}` on a Linux runner.
    return subprocess.run(
        ["bash", "-e", str(script)], cwd=clone, env=env, capture_output=True, text=True
    )


class TestTheCommitStep:
    def test_run_323s_race_lands_both_runs_rows(self, origin, tmp_path):
        stale = checkout(origin, tmp_path / "run-323")  # queued before 322 pushed
        landed = run_322_lands(origin, tmp_path)
        collect(stale)

        result = commit_step(stale)

        assert result.returncode == 0, result.stdout + result.stderr
        # Every row, once each: the rows already on main, then this run's.
        assert on_main(origin, SHARD) == [
            '{"score_id": "base-1"}',
            '{"score_id": "base-2"}',
            '{"score_id": "322-a"}',
            '{"score_id": "322-b"}',
            '{"score_id": "323-a"}',
        ]
        assert on_main(origin, NEW_SHARD) == ['{"label_id": "322-l"}', '{"label_id": "323-l"}']
        # Rewritten whole by every run: the later run's copy.
        assert on_main(origin, MANIFEST) == ['{"heartbeat": "323"}']
        assert on_main(origin, EXPORT) == ['{"export": "323"}']
        # Linear history: this run's commit directly on top of the one it raced.
        assert tip(origin, "main^") == landed
        assert git(origin, "log", "-1", "--format=%s", "main").strip() == (
            "collect: observed=41 fired=1"
        )
        assert not git(origin, "branch", "--list", "collect-unpushed/*").strip()

    def test_with_the_branch_to_itself_it_just_pushes(self, origin, tmp_path):
        clone = checkout(origin, tmp_path / "run-323")
        base = tip(origin)
        collect(clone)

        result = commit_step(clone)

        assert result.returncode == 0, result.stdout + result.stderr
        assert tip(origin, "main^") == base
        assert on_main(origin, SHARD)[-1] == '{"score_id": "323-a"}'

    def test_nothing_new_commits_nothing(self, origin, tmp_path):
        clone = checkout(origin, tmp_path / "run-323")
        base = tip(origin)

        result = commit_step(clone)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "nothing new this cycle" in result.stdout
        assert tip(origin) == base

    def test_a_conflict_it_cannot_merge_fails_the_run_and_keeps_the_rows(
        self, origin, tmp_path
    ):
        """Only shards, the manifest and the export have a known right answer."""
        stale = checkout(origin, tmp_path / "run-323")
        other = checkout(origin, tmp_path / "someone")
        write(other, "state/README.txt", "theirs\n")
        commit_as(other, "someone", "edit state/README.txt")
        git(other, "push", "-q", "origin", "HEAD:main")
        before = tip(origin)
        collect(stale)
        write(stale, "state/README.txt", "ours\n")

        result = commit_step(stale)

        assert result.returncode != 0
        assert "::error::state/README.txt conflicts with main" in result.stdout
        assert tip(origin) == before  # nothing half-pushed
        assert '{"score_id": "323-a"}' in on_main(origin, SHARD, ref=KEPT)
        assert '+{"score_id": "323-a"}' in (stale / "unpushed.patch").read_text()
        assert not (stale / ".git" / "rebase-merge").exists()

    def test_a_push_refused_every_time_fails_the_run_and_keeps_the_rows(
        self, origin, tmp_path
    ):
        """The old loop's last push decided the step's exit code, whatever it
        pushed. Five refusals must be a red run, not a green one."""
        hook = origin / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "while read old new ref; do\n"
            '  if [ "$ref" = refs/heads/main ]; then echo "main is locked" >&2; exit 1; fi\n'
            "done\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        clone = checkout(origin, tmp_path / "run-323")
        before = tip(origin)
        collect(clone)

        result = commit_step(clone)

        assert result.returncode != 0
        assert "refused the push five times" in result.stdout
        assert tip(origin) == before
        assert '{"score_id": "323-a"}' in on_main(origin, SHARD, ref=KEPT)


class TestTheWorkflowAroundIt:
    def test_a_queued_run_starts_from_the_branch_tip(self):
        """Without `ref`, checkout takes the commit the event carried -- for a run
        queued behind another, a journal missing everything that run pushed."""
        (checkout_step,) = [
            s for s in steps() if str(s.get("uses", "")).startswith("actions/checkout@")
        ]
        assert (checkout_step.get("with") or {}).get("ref") == "${{ github.ref }}"

    def test_the_script_reads_its_inputs_from_env(self):
        """An expression inside `run:` is pasted into the script before bash sees
        it, so the tests above could not run the script as written."""
        commit = step("Commit the journal")
        assert "${{" not in commit["run"]
        assert set(commit["env"]) == {"SUMMARY", "RETRY_SECONDS"}

    def test_shards_merge_as_a_union_and_nothing_else_does(self, tmp_path):
        repo = tmp_path / "attrs"
        git(tmp_path, "init", "-q", str(repo))
        shutil.copy(REPO_ROOT / ".gitattributes", repo / ".gitattributes")
        paths = [
            SHARD,
            "state/scores/2026-09-25.1.jsonl",  # a rollover
            "state/scores.jsonl",  # the pre-shard journal
            MANIFEST,
            EXPORT,
        ]
        merge = {
            line.split(": ")[0]: line.split(": ")[2]
            for line in git(repo, "check-attr", "merge", "--", *paths).splitlines()
        }
        assert merge == {
            SHARD: "union",
            "state/scores/2026-09-25.1.jsonl": "union",
            "state/scores.jsonl": "union",
            MANIFEST: "unspecified",
            EXPORT: "unspecified",
        }

    def test_a_failed_runs_rows_are_uploaded(self):
        keep = step("Keep the unpushed rows")
        assert keep["if"] == "failure()"
        assert keep["with"]["path"] == "unpushed.patch"
