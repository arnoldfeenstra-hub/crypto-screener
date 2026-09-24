"""Phase 2 -- calibration report, and the record an adopted weight vector cites.

The report answers one question: **does the top decile beat the base rate out of
sample?** BUILD_BRIEF.md section 3 gates Phase 3 on that answer and is blunt about
the alternative -- if it does not, say so plainly and go back to Phase 0, because a
screener with no measured edge is worse than no screener: it launders a coin flip
as a decision.

So :func:`render` is written to make the negative answer as easy to say as the
positive one. ``verdict`` has three values and two of them are "no".

``--record`` writes the *calibration record*: the JSON file under
``scoring/weights/`` that a weight vector in ``scoring/pillars.py`` is copied
from. It carries what .claude/rules/stats.md asks to be reported -- out-of-sample
AUC and top-decile lift with intervals, the base rate, the regimes the held-out
window saw -- for the vector being adopted *and* the one it replaces, measured on
the same rows, plus every check by name. A weight vector with no record behind it
is a prior.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from calibration.backtest import DEFAULT_LABEL, DEFAULT_THRESHOLD
from calibration.fit import (
    CALIBRATION_LABELS,
    MIN_DEAD_PER_SURVIVOR,
    MIN_TRIGGERED_TOKENS,
    PUBLISHED_CONCORDANCE_BENCHMARK,
    ExitCriteria,
    FitResult,
    Row,
    auc,
    auc_interval,
    base_rate,
    check_exit_criteria,
    fit,
    outcome,
    prior_score,
    rows_from_store,
    time_split,
    top_decile_lift,
    top_decile_lift_interval,
)
from collectors.config import load_config
from collectors.store import Store
from scoring.pillars import WEIGHTS, WEIGHTS_VERSION, score_candidate

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
            "Phase 0 has not finished collecting, so no edge can be claimed. "
            f"{criteria.explain()}. Until this is met, every score the screener "
            "emits -- whatever weights are in force -- is a logged observation and "
            "must not be read as a prediction.",
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


def base_rate_by_mcap_band(
    store: Any, label: str = DEFAULT_LABEL, threshold: float | None = DEFAULT_THRESHOLD
) -> dict[str, Any]:
    """Outcome rate per market-cap band.

    stats.md: a 2% base rate makes accuracy meaningless, so every result is
    reported next to the base rate for the same band. A multiple is read against
    ``threshold`` exactly as the fit reads it -- a band's rate and a fit's rate
    that counted different things as a yes could not be compared.
    """
    bands = (
        ("<500k", 0.0, 500_000.0),
        ("500k-2m", 500_000.0, 2_000_000.0),
        ("2m-10m", 2_000_000.0, 10_000_000.0),
        (">10m", 10_000_000.0, float("inf")),
    )
    resolved: list[tuple[float, int]] = []
    for snapshot in store.snapshots_for_labelling():
        mcap = snapshot.get("market_mcap_usd")
        result = outcome(store.latest_labels(snapshot["snapshot_id"]), label, threshold)
        if mcap is not None and result is not None:
            resolved.append((mcap, result))
    out: dict[str, Any] = {}
    for name, low, high in bands:
        in_band = [result for mcap, result in resolved if low <= mcap < high]
        out[name] = {
            "n": len(in_band),
            "positives": sum(in_band),
            "base_rate": sum(in_band) / len(in_band) if in_band else None,
        }
    return out


def _pair(interval: tuple[float, float] | None) -> list[float] | None:
    return list(interval) if interval else None


def _ranking(labels: Sequence[int], scores: Sequence[float]) -> dict[str, Any]:
    return {
        "auc": auc(labels, scores),
        "auc_ci95": _pair(auc_interval(labels, scores)),
        "top_decile_lift": top_decile_lift(labels, scores),
        "top_decile_lift_ci95": _pair(top_decile_lift_interval(labels, scores)),
    }


def held_out_by_label(store: Any, result: FitResult) -> list[dict[str, Any]]:
    """The fitted vector against the vector in force on every section 2 label.

    Only tokens triggered after the fit's training window count, so every row is
    one the fit never saw. That also means a horizon longer than the held-out
    window has nothing to report yet, and the entry says so (``n`` of 0) rather
    than borrowing rows from the training window -- where the fitted vector would
    be marking its own homework.
    """
    after = result.train_window[1] if result.train_window else None
    vectors = (("in_force", WEIGHTS), ("fitted", result.deployed_weights))
    out: list[dict[str, Any]] = []
    for label, threshold in CALIBRATION_LABELS:
        rows = [
            r
            for r in rows_from_store(store, label=label, threshold=threshold)
            if after is None or r.ts > after
        ]
        labels = [r.label for r in rows]
        entry: dict[str, Any] = {
            "label": label,
            "threshold": threshold,
            "n": len(rows),
            "positives": sum(labels),
            "base_rate": base_rate(labels) if rows else None,
        }
        if 0 < sum(labels) < len(labels):
            for name, weights in vectors:
                scores = [prior_score(r, weights) for r in rows]
                entry[name] = {
                    "auc": auc(labels, scores),
                    "auc_ci95": _pair(auc_interval(labels, scores)),
                }
        out.append(entry)
    return out


def final_score_comparison(
    store: Any, rows: Sequence[Row], result: FitResult
) -> dict[str, Any]:
    """Both vectors on the number the board ranks by, on the held-out rows.

    The fit compares pillar composites. The board ranks the score *after* the
    completeness multiplier and the regime and contradiction modifiers, which can
    reorder rows, so the vector being adopted is also checked on that number,
    recomputed from each token's stored trigger-time input packet.
    """
    packets = {row["snapshot_id"]: row for row in store.trigger_time_scores()}
    test = time_split(rows).test
    labels = [r.label for r in test]
    out: dict[str, Any] = {}
    for name, weights in (("in_force", WEIGHTS), ("fitted", result.deployed_weights)):
        scores: list[float] = []
        for r in test:
            packet = packets[r.snapshot_id]
            candidate = json.loads(packet["input_snapshot"] or "{}")
            final = score_candidate(
                candidate, regime=packet.get("batch_regime"), weights=weights
            ).score
            scores.append(final if final is not None else float("-inf"))
        out[name] = _ranking(labels, scores)
    return out


def render(
    store: Any,
    *,
    label: str = DEFAULT_LABEL,
    threshold: float | None = DEFAULT_THRESHOLD,
    force: bool = False,
) -> dict[str, Any]:
    """Produce the full calibration report. Every figure is out of sample.

    The default outcome is the backtest's, a 1.5x inside six hours, fixed in
    calibration/backtest.py before any fit was run. Choosing the label after
    looking at which one fits best would be a multiple-comparisons error with a
    sample this small.
    """
    return build(store, label=label, threshold=threshold, force=force)[0]


def build(
    store: Any,
    *,
    label: str = DEFAULT_LABEL,
    threshold: float | None = DEFAULT_THRESHOLD,
    force: bool = False,
) -> tuple[dict[str, Any], FitResult | None, ExitCriteria]:
    """:func:`render`, plus the fit and the exit criteria it was judged against."""
    triggered = store.snapshot_count()
    survivors, dead = store.survivor_counts("7d")
    complete_social = store.snapshots_with_complete_social()
    criteria = check_exit_criteria(triggered, survivors, dead, complete_social)

    rows = rows_from_store(store, label=label, threshold=threshold)
    result: FitResult | None = None
    error: str | None = None
    if rows:
        try:
            result = fit(
                rows, label=label, threshold=threshold, exit_criteria=criteria, force=force
            )
        except RuntimeError as exc:
            error = str(exc)

    code, explanation = verdict(result, criteria)
    report: dict[str, Any] = {
        "verdict": code,
        "explanation": explanation,
        "label": label,
        "threshold": threshold,
        "weights_version_in_force": WEIGHTS_VERSION,
        "phase_0_exit_criteria": criteria.to_dict(),
        "labelled_rows_available": len(rows),
        "base_rate_by_mcap_band": base_rate_by_mcap_band(store, label, threshold),
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
        report["adoption_checks"] = result.checks(criteria)
        report["may_replace_weights_in_force"] = result.may_replace_priors(criteria)
        if result.positives_test:
            report["held_out_by_label"] = held_out_by_label(store, result)
            report["final_score"] = final_score_comparison(store, rows, result)
        if result.forced:
            report["warning"] = (
                "This fit was forced past Phase 0's exit criteria. Treat every "
                "figure below as a small-sample measurement, not a validated result."
            )
    return report, result, criteria


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def calibration_record(
    report: dict[str, Any], result: FitResult, criteria: ExitCriteria, *, version: str
) -> dict[str, Any]:
    """The JSON a weight vector is adopted from. See the module docstring."""
    train_from, train_to = result.train_window or (None, None)
    test_from, test_to = result.test_window or (None, None)
    return {
        "weights_version": version,
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": (
            "calibration/fit.py: logistic regression on the seven pillar scores, one "
            "row per token as scored at its trigger, split forward in time (last 30% "
            "held out). Coefficients clamped at zero, renormalised to sum to 1 and "
            "rounded to two places (LogisticModel.deployable_weights)."
        ),
        "label": report["label"],
        "threshold": report["threshold"],
        "weights": result.deployed_weights,
        "replaces": {"weights_version": WEIGHTS_VERSION, "weights": dict(WEIGHTS)},
        "coefficients": {**result.model.weights, "bias": result.model.bias},
        "train": {
            "tokens": result.train_size,
            "from": _iso(train_from),
            "to": _iso(train_to),
            "base_rate": result.base_rate_train,
        },
        "test": {
            "tokens": result.test_size,
            "positives": result.positives_test,
            "from": _iso(test_from),
            "to": _iso(test_to),
            "base_rate": result.base_rate_test,
            "base_rate_ci95": list(result.interval_test),
            "regimes": {name: block["n"] for name, block in result.by_regime.items()},
        },
        "out_of_sample": {
            "fitted": {
                "auc": result.deployed_auc,
                "auc_ci95": _pair(result.deployed_auc_ci),
                "top_decile_lift": result.deployed_top_decile_lift,
                "top_decile_lift_ci95": _pair(result.deployed_top_decile_lift_ci),
            },
            "replaced": {
                "auc": result.prior_auc,
                "auc_ci95": _pair(result.prior_auc_ci),
                "top_decile_lift": result.prior_top_decile_lift,
                "top_decile_lift_ci95": _pair(result.prior_top_decile_lift_ci),
            },
            "logistic_model": {"auc": result.auc, "auc_ci95": _pair(result.auc_ci)},
            "final_score": report.get("final_score"),
            "concordance_benchmark": PUBLISHED_CONCORDANCE_BENCHMARK,
        },
        "held_out_by_label": report.get("held_out_by_label"),
        "checks": result.checks(criteria),
        "phase_0_exit_criteria": criteria.to_dict(),
        "forced_past_exit_criteria": result.forced,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibration-report",
        description="Phase 2. Report whether the top decile beats the base rate.",
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="the multiple that counts as a yes (ignored for survival labels)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="fit even though Phase 0 has not exited; the report will say so",
    )
    parser.add_argument(
        "--record",
        metavar="PATH",
        help=(
            "also write the calibration record for this fit to PATH, e.g. "
            "scoring/weights/fitted-v2.json; the file name is the weights version"
        ),
    )
    args = parser.parse_args(argv)

    config = load_config()
    with Store(args.db or str(config.db_path), read_only=True) as store:
        report, result, criteria = build(
            store, label=args.label, threshold=args.threshold, force=args.force
        )
    if args.record:
        if result is None:
            parser.error("no fit was produced, so there is nothing to record")
        path = Path(args.record)
        record = calibration_record(report, result, criteria, version=path.stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
