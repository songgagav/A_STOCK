#!/usr/bin/env python3
"""Build an offline, non-authoritative H5i technical-factor IC bundle."""
from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import ic_curve_diagnostic as diagnostic  # noqa: E402


class _CliArgumentError(Exception):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliArgumentError(message)


def _git_code_sha(repo_root: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("cannot identify the diagnostic code revision") from exc
    if status.strip():
        raise ValueError("diagnostic requires a clean committed code tree")
    return head


def _csv_bytes(frame) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(diagnostic._CSV_COLUMNS)
    ordered = frame.sort_values(["day", "horizon"], kind="mergesort")
    for row in ordered.itertuples(index=False):
        value = "" if row.ic is None or not np.isfinite(float(row.ic)) else format(float(row.ic), ".17g")
        writer.writerow(
            [
                str(row.day),
                str(int(row.horizon)),
                value,
                str(row.status),
                str(int(row.n_factor)),
                str(int(row.n_forward_valid)),
                str(int(row.n_pairs)),
                str(int(row.n_missing_factor)),
                str(int(row.n_missing_forward)),
            ]
        )
    return output.getvalue().encode("utf-8")


def _calendar_due_dates(inputs: diagnostic.DiagnosticInputs) -> dict[str, dict[str, str]]:
    positions = {day: index for index, day in enumerate(inputs.calendar_days)}
    result: dict[str, dict[str, str]] = {}
    for signal_day in inputs.signal_days:
        position = positions[signal_day]
        due: dict[str, str] = {}
        for horizon in diagnostic.DEFAULT_HORIZONS:
            due_index = position + horizon
            if due_index >= len(inputs.calendar_days):
                raise ValueError(f"calendar does not cover {horizon}-day due date for {signal_day}")
            due[str(horizon)] = inputs.calendar_days[due_index]
        result[signal_day] = due
    return result


def _last_settled_days(curves: dict[str, object]) -> dict[str, dict[str, str | None]]:
    result: dict[str, dict[str, str | None]] = {}
    for factor, frame in curves.items():
        result[factor] = {}
        for horizon in diagnostic.DEFAULT_HORIZONS:
            settled = frame.loc[
                (frame["horizon"] == horizon) & (frame["status"] == "settled"), "day"
            ]
            result[factor][str(horizon)] = str(settled.max()) if not settled.empty else None
    return result


def _eligible_counts(
    inputs: diagnostic.DiagnosticInputs, factor_scores: pd.DataFrame
) -> dict[str, int]:
    primary_factor = factor_scores.loc[
        factor_scores["factor"] == diagnostic.FACTOR_NAMES[0], ["day", "symbol"]
    ]
    counts = primary_factor.groupby("day")["symbol"].nunique().to_dict()
    return {day: int(counts.get(day, 0)) for day in inputs.signal_days}


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="build_ic_curve_diagnostic.py",
        description="Build an offline H5i IC diagnostic bundle (never authoritative).",
    )
    parser.add_argument("--h5i-path", required=True)
    parser.add_argument("--calendar-path", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


class _EmptyEligibleWindow(ValueError):
    pass


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        start_day, end_day = diagnostic._day_key(args.start), diagnostic._day_key(args.end)
        earliest_allowed = "20260501"
        if start_day < earliest_allowed:
            raise ValueError("start must be 2026-05-01 or later")
        if start_day > end_day:
            raise ValueError("start must be on or before end")

        code_sha = _git_code_sha(_REPO_ROOT)
        inputs = diagnostic.read_diagnostic_inputs(
            Path(args.h5i_path),
            Path(args.calendar_path),
            start=start_day,
            end=end_day,
        )
        factor_scores, factor_missingness = diagnostic.compute_factor_scores(
            inputs.bars, inputs.signal_days
        )
        if factor_scores.empty:
            raise _EmptyEligibleWindow("no positive-close symbols have a complete 21-close factor history")
        forward_labels, forward_missingness = diagnostic.compute_forward_labels(
            inputs.bars,
            inputs.calendar_days,
            inputs.signal_days,
            inputs.source_max_day,
        )
        curves = diagnostic.build_ic_curves(
            factor_scores,
            forward_labels,
            inputs.signal_days,
        )
        csv_files = {f"{factor}.csv": _csv_bytes(curves[factor]) for factor in diagnostic.FACTOR_NAMES}

        parameters = {
            "start": start_day,
            "end": end_day,
            "horizons": list(diagnostic.DEFAULT_HORIZONS),
            "factor_names": list(diagnostic.FACTOR_NAMES),
            "universe": "all_bar_eligible_at_signal_close",
            "close_adjustment_basis": "as_stored_unverified",
        }
        run_id, run_identity = diagnostic.build_run_identity(
            parameters,
            code_sha=code_sha,
            calendar_sha256=inputs.calendar_sha256,
            schema_fingerprint=inputs.schema_fingerprint,
            queried_rows_sha256=inputs.queried_rows_sha256,
            source_max_day=inputs.source_max_day,
        )
        manifest = {
            "run_identity": run_identity,
            "parameters": parameters,
            "inputs": {
                "h5i_path": str(inputs.source_path),
                "calendar_path": str(inputs.calendar_path),
                "source_max_day": inputs.source_max_day,
                "calendar_sha256": inputs.calendar_sha256,
                "schema_fingerprint": inputs.schema_fingerprint,
                "queried_rows_sha256": inputs.queried_rows_sha256,
                "query_bounds": inputs.query_bounds,
                "close_adjustment_basis": "as_stored_unverified",
            },
            "formulas": {
                "vol_selector_score_v1": "factor_library.score_factor('vol', last_21_finite_positive_closes_as_of_t)",
                "mom_rev_selector_score_v1": "factor_library.score_factor('mom', last_21_finite_positive_closes_as_of_t)",
                "forward_return": "product(1 + change_pct[d,symbol] / 100) - 1 for exact next h calendar days",
                "ic": "Pearson correlation of average ranks (Spearman)",
            },
            "horizons": list(diagnostic.DEFAULT_HORIZONS),
            "source_max_day": inputs.source_max_day,
            "calendar_due_dates": _calendar_due_dates(inputs),
            "last_settled_ic_day": _last_settled_days(curves),
            "row_counts": {factor: int(len(curves[factor])) for factor in diagnostic.FACTOR_NAMES},
            "eligible_counts_by_day": _eligible_counts(inputs, factor_scores),
            "missingness": {
                "factor_by_day_reason": factor_missingness,
                "forward_by_day_reason": forward_missingness,
            },
            "calendar_provenance": inputs.calendar_provenance,
            "diagnostic_only": True,
            "authoritative": False,
            "observation_eligible": False,
        }

        # Revalidate every source immediately before the only output operation.
        diagnostic.verify_inputs_unchanged(inputs)
        final_path = diagnostic.finalize_diagnostic_bundle(
            Path(args.output_root),
            repo_root=_REPO_ROOT,
            run_id=run_id,
            csv_files=csv_files,
            manifest=manifest,
        )
        print(json.dumps({"ok": True, "manifest_path": str(final_path / "manifest.json")}, separators=(",", ":")))
        return 0
    except _CliArgumentError as exc:
        payload = {"ok": False, "error_code": "invalid_arguments", "message": str(exc)}
    except _EmptyEligibleWindow as exc:
        payload = {"ok": False, "error_code": "empty_eligible_window", "message": str(exc)}
    except ValueError as exc:
        payload = {"ok": False, "error_code": "validation_error", "message": str(exc)}
    except OSError as exc:
        payload = {"ok": False, "error_code": "io_error", "message": str(exc)}
    except Exception as exc:  # stable JSON boundary; traceback remains available to direct library callers
        payload = {"ok": False, "error_code": "runtime_error", "message": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
