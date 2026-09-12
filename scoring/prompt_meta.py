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

import re
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "score.md"


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
