"""Prompt metadata, readable without importing the world.

``prompt_version`` and the SYSTEM block live here rather than in
``scoring/runner.py`` because the runner imports DuckDB through the store, and the
Vercel function in ``api/`` must not. Same reasoning as
``collectors/trigger_rule.py``: one definition, imported by both, so a version
stamped on a live row and a version stamped on a stored row cannot disagree.

The version is read out of the file every time rather than held as a constant, so
the recorded version cannot drift away from the prompt actually in use.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "score.md"

# One calibration record per fitted weights version, named after it. Kept under
# scoring/ rather than calibration/ because the Vercel function ships scoring/ and
# not calibration/ (vercel.json includeFiles), and the live page quotes it too.
WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"


def prompt_version(path: Path | None = None) -> int:
    """Read ``prompt_version`` out of prompts/score.md."""
    target = path or PROMPT_PATH
    text = target.read_text(encoding="utf-8")
    match = re.search(r"prompt_version:\s*(\d+)", text)
    if not match:
        raise ValueError(f"no prompt_version found in {target}")
    return int(match.group(1))


def system_prompt(path: Path | None = None) -> str:
    """Extract the SYSTEM block from prompts/score.md.

    The file is the single source of truth: editing the prompt changes behaviour
    without touching any module, which is the point of versioning it.
    """
    target = path or PROMPT_PATH
    text = target.read_text(encoding="utf-8")
    start = text.find("## SYSTEM")
    if start == -1:
        raise ValueError("no '## SYSTEM' block in prompts/score.md")
    return text[start:].strip()


def weights_record(version: str) -> dict[str, Any] | None:
    """The calibration record a weights version was copied from, or ``None``.

    ``None`` is the answer for every ``priors-*`` version: a prior was never
    fitted, so there is nothing to cite. Written by ``calibration/report.py
    --record``; see that module for what a record must carry.
    """
    path = WEIGHTS_DIR / f"{version}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _outcome_phrase(label: str, threshold: float | None) -> str:
    horizon = label.rsplit("_", 1)[-1]
    if label.startswith("survived_"):
        return f"survival to {horizon}"
    return f"a {threshold:g}x within {horizon}"


def weights_caveat(version: str) -> str:
    """One sentence on what the weights in force are, for every page that shows a
    score. Built from the record so the figures cannot drift from it."""
    record = weights_record(version)
    if record is None:
        return "The scoring weights are uncalibrated priors: guesses."
    fitted = record["out_of_sample"]["fitted"]
    low, high = fitted["auc_ci95"]
    gate = "" if record["phase_0_exit_criteria"]["met"] else ", before Phase 0's gate was met"
    return (
        f"The scoring weights ({version}) were fitted on {record['train']['tokens']} "
        f"tokens{gate}. On the {record['test']['tokens']} that triggered next they "
        f"ranked {_outcome_phrase(record['label'], record['threshold'])} with AUC "
        f"{fitted['auc']:.2f} (95% CI {low:.2f}-{high:.2f}; 0.50 is a coin flip) -- "
        "a sample too small to establish an edge."
    )
