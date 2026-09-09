"""Phase 2 -- calibration report.

The report answers one question: **does the top decile beat the base rate out of
sample?** BUILD_BRIEF.md section 3 gates Phase 3 on that answer and is blunt about
the alternative -- if it does not, say so plainly and go back to Phase 0, because a
screener with no measured edge is worse than no screener: it launders a coin flip
as a decision.

So :func:`render` is written to make the negative answer as easy to say as the
positive one. ``verdict`` has three values and two of them are "no".
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from calibration.fit import (
    MIN_DEAD_PER_SURVIVOR,
    MIN_TRIGGERED_TOKENS,
    PUBLISHED_CONCORDANCE_BENCHMARK,
    ExitCriteria,
    FitResult,
    check_exit_criteria,
    fit,
    rows_from_store,
)
from collectors.config import load_config
from collectors.store import Store

PHASE = "2"

REQUIRED_SECTIONS = (
    "base_rate_by_mcap_band",
    "out_of_sample_auc",
    "concordance_vs_benchmark",
    "top_decile_lift_over_base_rate",
    "results_by_regime",
    "confidence_intervals",
)

VERDICT_NOT_READY = "not_ready"
VERDICT_NO_EDGE = "no_measured_edge"
VERDICT_EDGE = "measured_edge"


def verdict(result: FitResult | None, criteria: ExitCriteria) -> tuple[str, str]:
    """``(verdict, plain English)``. Two of the three outcomes are negative."""
    if not criteria.met:
        return (
            VERDICT_NOT_READY,
            "Phase 0 has not finished collecting, so there is nothing to calibrate. "
            f"{criteria.explain()}. Until this is met, every score the screener "
            "emits is an uncalibrated prior and must not be read as a prediction.",
        )
    if result is None:
        return (VERDICT_NOT_READY, "No fit was produced.")
    if result.positives_test == 0:
        # Distinct from "no edge": nothing was measured at all. A chronological
        # split can land every positive event in train when outcomes cluster in
        # time, and reporting that as a failed model would be wrong twice over.
        return (
            VERDICT_NOT_READY,
            f"The held-out set of {result.test_size} rows contains no positive "
            "events, so nothing can be measured on it. This usually means outcomes "
            "are clustered in time; collect a longer span before splitting again.",
        )
    if not result.has_measured_edge:
        lift = result.top_decile_lift
        lift_text = "undefined" if lift is None else f"{lift:.2f}x"
        return (
            VERDICT_NO_EDGE,
            f"The top decile scores {lift_text} the base rate out of sample, so no "
            "edge has been measured. Per BUILD_BRIEF.md section 3 the honest step is "
            "to go back to Phase 0 with better features, not to ship the ranking: a "
            "screener with no measured edge launders a coin flip as a decision.",
        )
    concordance_text = (
        f"{result.concordance:.3f}" if result.concordance is not None else "undefined"
    )
    benchmark_note = (
        "above" if result.beats_benchmark else "below"
    )
    return (
        VERDICT_EDGE,
        f"The top decile beats the base rate by {result.top_decile_lift:.2f}x out of "
        f"sample, on {result.positives_test} positive events in a test set of "
        f"{result.test_size}. Concordance {concordance_text} is {benchmark_note} the "
        f"published {PUBLISHED_CONCORDANCE_BENCHMARK} benchmark.",
    )


def base_rate_by_mcap_band(store: Any, label: str = "survived_7d") -> dict[str, Any]:
    """Outcome rate per market-cap band.

    stats.md: a 2% base rate makes accuracy meaningless, so every result is
    reported next to the base rate for the same band.
    """
    bands = (
        ("<500k", 0.0, 500_000.0),
        ("500k-2m", 500_000.0, 2_000_000.0),
        ("2m-10m", 2_000_000.0, 10_000_000.0),
        (">10m", 10_000_000.0, float("inf")),
    )
    out: dict[str, Any] = {}
    for name, low, high in bands:
        total = 0
        positives = 0
        for snapshot in store.snapshots_for_labelling():
            mcap = snapshot.get("market_mcap_usd")
            if mcap is None or not (low <= mcap < high):
                continue
            labels = store.latest_labels(snapshot["snapshot_id"])
            if not labels or labels.get(label) is None:
                continue
            total += 1
            positives += 1 if labels[label] else 0
        out[name] = {
            "n": total,
            "positives": positives,
            "base_rate": positives / total if total else None,
        }
    return out


def render(
    store: Any,
    *,
    label: str = "survived_7d",
    force: bool = False,
) -> dict[str, Any]:
    """Produce the full calibration report. Every figure is out of sample."""
    triggered = store.snapshot_count()
    survivors, dead = store.survivor_counts("7d")
    complete_social = store.snapshots_with_complete_social()
    criteria = check_exit_criteria(triggered, survivors, dead, complete_social)

    rows = rows_from_store(store, label=label)
    result: FitResult | None = None
    error: str | None = None
    if rows:
        try:
            result = fit(rows, label=label, exit_criteria=criteria, force=force)
        except RuntimeError as exc:
            error = str(exc)

    code, explanation = verdict(result, criteria)
    report: dict[str, Any] = {
        "verdict": code,
        "explanation": explanation,
        "phase_0_exit_criteria": criteria.to_dict(),
        "labelled_rows_available": len(rows),
        "base_rate_by_mcap_band": base_rate_by_mcap_band(store, label),
        "concordance_benchmark": PUBLISHED_CONCORDANCE_BENCHMARK,
        "thresholds": {
            "min_triggered_tokens": MIN_TRIGGERED_TOKENS,
            "min_dead_per_survivor": MIN_DEAD_PER_SURVIVOR,
        },
    }
    if error:
        report["fit_refused"] = error
    if result:
        report["fit"] = result.to_dict()
        report["results_by_regime"] = result.by_regime
        if result.forced:
            report["warning"] = (
                "This fit was forced past Phase 0's exit criteria. Treat every "
                "figure below as an illustration of the pipeline, not a measurement."
            )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibration-report",
        description="Phase 2. Report whether the top decile beats the base rate.",
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--label", default="survived_7d")
    parser.add_argument(
        "--force",
        action="store_true",
        help="fit even though Phase 0 has not exited; the report will say so",
    )
    args = parser.parse_args(argv)

    config = load_config()
    with Store(args.db or str(config.db_path), read_only=True) as store:
        report = render(store, label=args.label, force=args.force)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
