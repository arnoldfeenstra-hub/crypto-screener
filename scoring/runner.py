"""Phase 1 -- the scoring runner. Paper mode.

BUILD_BRIEF.md section 3: "Wire prompts/score.md to the API. Run in paper mode:
score, log, never surface as a recommendation."

Pipeline, in the order prompts/score.md specifies:

    snapshot -> hard filters (step 1) -> pillar maths (step 2) -> narrative -> stored row

Hard filters run *first* and a failing token is scored ``null`` and excluded
regardless of how strong anything else looks. The pillar maths
(``scoring/pillars.py``) is deterministic, because Phase 2 fits coefficients
against it. The narrative -- thesis, bear case, falsifier -- is the part that needs
a model, and it is the only part that calls the API.

Three rules this module exists to keep
--------------------------------------
**Paper mode is the default and the only mode.** CLAUDE.md: the weights are
uncalibrated priors, so output here is a data collector that happens to emit
numbers, not a signal generator. ``paper_mode=False`` is not implemented and
raises; there is nothing for it to switch on.

**No execution path, ever** (hard rule 4). No exchange keys, no signing, no order
placement. The output is a ranked list a human reads.

**Safety is looked up per batch, not per row, and never before the trigger.**
``collectors/safety.py`` answers six of the eight hard filters. It runs here rather
than in the watcher because a safety API that reports holder counts on some chains
and not others would, if it reached the trigger, make entry to the dataset
chain-dependent -- and a pooled cross-chain sample with a chain-dependent entry rule
cannot be interpreted. By the time this module runs, entry has already been decided.

**Every scored row carries its inputs** (hard rule 6). ``prompt_version``,
``weights_version``, the model id and the entire candidate packet are stored in the
same row as the score. prompts/score.md will be edited many times before Phase 2,
and a score whose inputs are gone cannot be back-tested.

Running without an API key is the normal case: the pillar scores and the filters
are computed locally, and the narrative fields stay null with
``narrative_source='none'``. The API is additive, never load-bearing.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from collectors.config import load_config
from collectors.safety import SafetyReport, SafetySource
from collectors.safety import from_row as safety_from_row
from collectors.schema import now_ms
from collectors.store import Store
from filters.hard_filters import Verdict, apply
from scoring.candidate import candidate_from_row, filter_input_from_candidate
from scoring.pillars import WEIGHTS, WEIGHTS_VERSION, PillarResult, score_candidate
from scoring.prompt_meta import PROMPT_PATH, prompt_version, system_prompt

log = logging.getLogger("scoring")

PHASE = "1"
PAPER_MODE_ONLY = True
DEFAULT_MODEL = "claude-opus-5"
NO_EDGE_THRESHOLD = 55.0

# How long a stored safety verdict is reused before it is looked up again.
#
# Both numbers in this trade-off are real. Safety is not static -- a mint authority
# gets revoked, an LP gets pulled -- so a verdict has a shelf life. But the
# collector re-scores its recent snapshots every cycle, and re-asking a keyless,
# rate-limited, free API the same question every half hour is both slow and rude:
# at 30 requests a minute, 200 tokens is minutes of throttled traffic per run,
# repeated forever.
#
# Six hours keeps the answers fresh enough to catch a rug that happened since, and
# turns a per-cycle sweep into a per-token one. Every refresh still appends a new
# row, so the history of what changed and when is kept in full.
SAFETY_MAX_AGE_MS = 6 * 60 * 60 * 1000

# WEIGHTS_VERSION, PROMPT_PATH, prompt_version and system_prompt are imported above
# and re-exported here. They moved to scoring/pillars.py and scoring/prompt_meta.py
# so api/screener.py can stamp the same versions on a live row without importing
# DuckDB through the store; this module was their home, and every existing import
# site still works.
__all__ = [
    "PROMPT_PATH",
    "WEIGHTS_VERSION",
    "Narrative",
    "NarrativeClient",
    "ScoredBatch",
    "ScoringRunner",
    "candidate_from_row",
    "filter_input_from_candidate",
    "main",
    "prompt_version",
    "system_prompt",
]

# prompts/score.md step 3.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string"},
        "bear_case": {"type": "string"},
        "falsifier": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["thesis", "bear_case", "falsifier", "confidence", "data_gaps"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Narrative:
    thesis: str | None = None
    bear_case: str | None = None
    falsifier: str | None = None
    confidence: str | None = None
    data_gaps: tuple[str, ...] = ()
    source: str = "none"


class NarrativeClient:
    """Wraps the Anthropic API for the qualitative half of a score.

    Optional by design. Without a key the screener still filters, still computes
    pillars and still stores rows -- the narrative fields are simply null, which is
    a smaller loss than a fabricated bear case would be.
    """

    def __init__(self, model: str = DEFAULT_MODEL, client: Any | None = None) -> None:
        self.model = model
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on install
                raise RuntimeError(
                    "the anthropic package is not installed; run "
                    "`pip install anthropic` or score without --narrate"
                ) from exc
            self._client = anthropic.Anthropic()
        return self._client

    def narrate(
        self, candidate: dict[str, Any], pillars: PillarResult, regime: str | None
    ) -> Narrative:
        payload = {
            "candidate": candidate,
            "computed_pillar_scores": pillars.pillar_scores(),
            "composite_score": pillars.score,
            "modifiers_applied": list(pillars.modifiers),
            "batch_regime": regime,
            "automated_notes": pillars.notes(),
        }
        message = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system_prompt(),
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Pillar scores and the composite have already been computed "
                        "deterministically and are given below; do not recompute them. "
                        "Return the thesis, the strongest bear case, the falsifier, a "
                        "confidence level and the data gaps for this one candidate.\n\n"
                        + json.dumps(payload, indent=2, default=str)
                    ),
                }
            ],
        )
        if getattr(message, "stop_reason", None) == "refusal":
            log.warning("narrative refused for %s", candidate.get("ticker"))
            return Narrative(source="refused")
        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            return Narrative(source="empty")
        data = json.loads(text)
        return Narrative(
            thesis=data.get("thesis"),
            bear_case=data.get("bear_case"),
            falsifier=data.get("falsifier"),
            confidence=data.get("confidence"),
            data_gaps=tuple(data.get("data_gaps") or ()),
            source=self.model,
        )


@dataclass
class ScoredBatch:
    regime: str | None
    rows: list[dict[str, Any]]

    @property
    def ranked(self) -> list[dict[str, Any]]:
        """Surviving candidates in rank order, best first."""
        return sorted(
            (r for r in self.rows if not r["excluded"]),
            key=lambda r: (r["rank"] is None, r["rank"] or 0),
        )

    @property
    def rejected(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["excluded"]]

    def verdict(self) -> str:
        """prompts/score.md step 3: say plainly when there is no edge in the batch."""
        best = max((r["score"] or 0.0) for r in self.ranked) if self.ranked else 0.0
        if not self.ranked:
            return "no candidate cleared the hard filters"
        if best < NO_EDGE_THRESHOLD:
            return (
                f"no edge in this batch (best score {best:.1f} < {NO_EDGE_THRESHOLD:.0f}); "
                "weights are uncalibrated priors, so this is a logged observation, "
                "not a recommendation"
            )
        return (
            f"best score {best:.1f}, but weights are uncalibrated priors -- "
            "Phase 2 has not run, so no edge has been measured"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_regime": self.regime,
            "ranked": [
                {
                    "rank": r["rank"],
                    "ticker": r["ticker"],
                    "chain": r["chain"],
                    "score": r["score"],
                    "pillar_scores": json.loads(r["pillar_scores"]),
                    "modifiers_applied": json.loads(r["modifiers_applied"]),
                    "thesis": r["thesis"],
                    "bear_case": r["bear_case"],
                    "falsifier": r["falsifier"],
                    "confidence": r["confidence"],
                    "data_gaps": json.loads(r["data_gaps"]),
                }
                for r in self.ranked
            ],
            "rejected": [
                {
                    "ticker": r["ticker"],
                    # score.md step 3 has one flat list, so both kinds go in it. The
                    # split is kept beside it: "failed a check" and "the check was
                    # never run" exclude alike but mean very different things.
                    "rejected_by": json.loads(r["rejected_by"] or "[]")
                    + json.loads(r["indeterminate_on"] or "[]"),
                    "hard_rejected_by": json.loads(r["rejected_by"] or "[]"),
                    "indeterminate_on": json.loads(r["indeterminate_on"] or "[]"),
                }
                for r in self.rejected
            ],
            "batch_verdict": self.verdict(),
        }

    def exclusion_summary(self) -> dict[str, int]:
        """How many rows failed a check versus how many were never checked."""
        hard = sum(1 for r in self.rejected if json.loads(r["rejected_by"] or "[]"))
        return {
            "excluded_total": len(self.rejected),
            "excluded_on_evidence": hard,
            "excluded_as_unmeasured": len(self.rejected) - hard,
        }


class ScoringRunner:
    def __init__(
        self,
        store: Store | None = None,
        *,
        paper_mode: bool = True,
        narrator: NarrativeClient | None = None,
        model: str = DEFAULT_MODEL,
        safety_source: SafetySource | None = None,
        safety_max_age_ms: int = SAFETY_MAX_AGE_MS,
    ) -> None:
        if not paper_mode:
            raise NotImplementedError(
                "paper mode is the only mode. The weights are uncalibrated priors "
                "(CLAUDE.md), so there is no live mode to switch on until Phase 2 "
                "shows the top decile beating the base rate out of sample."
            )
        self.store = store
        self.paper_mode = True
        self.narrator = narrator
        self.model = model
        # Optional, and additive like the narrator: without it the filters answer
        # "unknown" exactly as they did before, which excludes rather than passes.
        self.safety_source = safety_source
        self.safety_max_age_ms = safety_max_age_ms
        # Everything used for scoring this batch, reused and fresh together...
        self.last_safety_reports: dict[tuple[str, str], SafetyReport] = {}
        # ...and only the part that was actually looked up, which is the part that
        # gets appended. Writing back a reused verdict every cycle would grow the
        # table without recording anything new.
        self.last_safety_fetched: dict[tuple[str, str], SafetyReport] = {}
        self.prompt_version = prompt_version()

    def score_one(
        self,
        candidate: dict[str, Any],
        *,
        regime: str | None = None,
        safety: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        safety = safety or {}
        if safety:
            # Hard rule 6: the score and the inputs that produced it live in the
            # same row. Safety fields decide six of the eight hard filters, so a
            # score stored without them could never be re-derived -- the row would
            # say "excluded" with no record of what excluded it.
            candidate = {**candidate, "safety": safety}
        verdict: Verdict = apply(filter_input_from_candidate(candidate, **safety))
        excluded = verdict.excluded

        pillars = score_candidate(candidate, regime=regime)
        narrative = Narrative()
        # Only spend a call on a candidate that survived the filters. A rejected
        # token's bear case is already known: it failed a hard check.
        if self.narrator is not None and not excluded:
            try:
                narrative = self.narrator.narrate(candidate, pillars, regime)
            except Exception:
                log.exception("narrative failed for %s", candidate.get("ticker"))
                narrative = Narrative(source="error")

        gaps = list(narrative.data_gaps) or [
            p.name for p in pillars.pillars if not p.resolved
        ]
        return {
            "score_id": str(uuid.uuid4()),
            "run_id": run_id or str(uuid.uuid4()),
            "snapshot_id": candidate.get("snapshot_id"),
            "scored_at_ms": now_ms(),
            "prompt_version": self.prompt_version,
            "weights_version": WEIGHTS_VERSION,
            "model": self.model if self.narrator else "deterministic-only",
            "paper_mode": True,
            "batch_regime": regime,
            "score": None if excluded else pillars.score,
            "raw_score": None if excluded else pillars.raw_score,
            "rank": None,
            "excluded": excluded,
            "rejected_by": json.dumps(verdict.rejected_by),
            "indeterminate_on": json.dumps(verdict.indeterminate_on),
            "pillar_scores": json.dumps(pillars.pillar_scores()),
            "pillar_components": json.dumps(
                {p.name: p.components for p in pillars.pillars}, default=str
            ),
            "modifiers_applied": json.dumps(list(pillars.modifiers)),
            "data_completeness": candidate.get("data_completeness"),
            "thesis": narrative.thesis,
            "bear_case": narrative.bear_case,
            "falsifier": narrative.falsifier,
            "confidence": narrative.confidence,
            "data_gaps": json.dumps(gaps),
            "narrative_source": narrative.source,
            # Hard rule 6: the inputs travel with the score.
            "input_snapshot": json.dumps(candidate, default=str),
            "ticker": candidate.get("ticker"),
            "chain": candidate.get("chain"),
        }

    def fetch_safety(
        self,
        candidates: Sequence[dict[str, Any]],
        known: dict[tuple[str, str], SafetyReport] | None = None,
        *,
        as_of_ms: int | None = None,
    ) -> dict[tuple[str, str], SafetyReport]:
        """Safety for the batch: what is already known, plus what needs asking.

        ``known`` is what earlier runs established, read back from the store. A
        verdict younger than :data:`SAFETY_MAX_AGE_MS` is reused; anything older or
        missing is looked up again. ``self.last_safety_fetched`` holds only the
        fresh ones, so a reused verdict is not written to the table a second time.

        A failure here is not fatal and must not be: unknown excludes, so a
        degraded safety fetch makes the screener more conservative, never less.
        """
        known = dict(known or {})
        self.last_safety_fetched = {}
        if self.safety_source is None:
            return known

        now = as_of_ms if as_of_ms is not None else now_ms()
        stale: list[tuple[str, str]] = []
        for candidate in candidates:
            key = (candidate.get("chain"), candidate.get("contract"))
            if not key[0] or not key[1]:
                continue
            existing = known.get(key)
            if existing is None or now - existing.collected_at_ms >= self.safety_max_age_ms:
                stale.append(key)

        if not stale:
            return known
        try:
            fresh = self.safety_source.fetch(stale)
        except Exception:
            log.exception("safety lookup failed; those filters stay unmeasured")
            return known

        self.last_safety_fetched = fresh
        known.update(fresh)
        return known

    def score_batch(
        self,
        candidates: Sequence[dict[str, Any]],
        *,
        regime: str | None = None,
        safety: dict[str, Any] | None = None,
        known_safety: dict[tuple[str, str], SafetyReport] | None = None,
    ) -> ScoredBatch:
        # One id for the whole batch: a ranking is read from a run, and two runs can
        # land in the same millisecond.
        run_id = str(uuid.uuid4())
        reports = self.fetch_safety(candidates, known_safety)
        self.last_safety_reports = reports
        rows = []
        for candidate in candidates:
            report = reports.get((candidate.get("chain"), candidate.get("contract")))
            per_token = dict(safety or {})
            if report is not None:
                per_token.update(report.to_filter_fields())
            rows.append(
                self.score_one(
                    candidate, regime=regime, safety=per_token or None, run_id=run_id
                )
            )
        ranked = sorted(
            (r for r in rows if not r["excluded"]),
            key=lambda r: (r["score"] is None, -(r["score"] or 0.0)),
        )
        for position, row in enumerate(ranked, start=1):
            row["rank"] = position
        return ScoredBatch(regime=regime, rows=rows)

    def run(self, *, limit: int = 50, regime: str | None = None) -> ScoredBatch:
        if self.store is None:
            raise ValueError("a Store is required to score stored snapshots")
        rows = self.store.recent_snapshots(limit)
        candidates = [candidate_from_row(row) for row in rows]
        known_safety = {
            key: safety_from_row(row)
            for key, row in self.store.latest_safety_by_token().items()
        }
        batch = self.score_batch(candidates, regime=regime, known_safety=known_safety)
        self.store.append_scores(batch.rows)
        if self.last_safety_fetched:
            # Stored as its own append-only observation, never written back onto the
            # snapshot: the lookup happened after the snapshot was taken, and the
            # change over time is itself worth keeping.
            snapshot_ids = {
                (row["chain"], row["contract"]): row["snapshot_id"] for row in rows
            }
            self.store.append_safety_observations(
                self.last_safety_fetched.values(), snapshot_ids
            )
        return batch


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scoring",
        description=(
            "Phase 1 scorer, paper mode. Scores stored snapshots and logs the result. "
            "Output is a ranked list a human reads; nothing here can place an order."
        ),
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--limit", type=int, default=50, help="snapshots to score")
    parser.add_argument("--regime", choices=["hot", "neutral", "cold"])
    parser.add_argument(
        "--narrate",
        action="store_true",
        help="also call the API for thesis / bear case / falsifier (needs ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--safety",
        action="store_true",
        help=(
            "look up GoPlus and RugCheck for each candidate (no API key). Without "
            "this, six of the eight hard filters can only answer 'unknown' and "
            "nearly every row is excluded as unmeasured rather than judged."
        ),
    )
    parser.add_argument(
        "--no-rugcheck",
        action="store_true",
        help="with --safety, use GoPlus only (RugCheck is one request per token)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    config = load_config()
    db_path = args.db or str(config.db_path)

    narrator = NarrativeClient(model=args.model) if args.narrate else None
    safety_source = (
        SafetySource(use_rugcheck=not args.no_rugcheck) if args.safety else None
    )
    with Store(db_path) as store:
        runner = ScoringRunner(
            store, narrator=narrator, model=args.model, safety_source=safety_source
        )
        batch = runner.run(limit=args.limit, regime=args.regime)
        payload = batch.to_dict()
        payload["_meta"] = {
            "paper_mode": True,
            "prompt_version": runner.prompt_version,
            "weights_version": WEIGHTS_VERSION,
            "weights": WEIGHTS,
            "scored": len(batch.rows),
            "safety_measured": len(runner.last_safety_reports),
            **batch.exclusion_summary(),
            "warning": (
                "Weights are uncalibrated priors. Phase 2 has not run, so no edge "
                "has been measured. Do not read these as predictions."
            ),
        }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
