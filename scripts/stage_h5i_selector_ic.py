#!/usr/bin/env python3
"""Stage a reproducible, non-authoritative selector-factor IC diagnostic."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Sequence

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from diagnostic_ic_bundle import (  # noqa: E402
    DiagnosticBundleError,
    verify_diagnostic_bundle,
    write_diagnostic_bundle,
)
from h5i_selector_ic import (  # noqa: E402
    H5iInputError,
    H5iReadOnlySource,
    H5iSchemaError,
    build_diagnostic_panels,
)
from selector_ic_contract import (  # noqa: E402
    FACTOR_CONTRACTS,
    HORIZONS,
    LABEL_VERSION,
)


MAX_SIGNAL_SESSIONS = 60
CODE_FILES = (
    "src/selector_ic_contract.py",
    "src/h5i_selector_ic.py",
    "src/diagnostic_ic_bundle.py",
    "scripts/stage_h5i_selector_ic.py",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CliInputError(ValueError):
    """Invalid explicit input or calendar/date contract."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a read-only H5i/PIT selector-factor IC diagnostic into an "
            "explicit external staging directory. Output is never authoritative."
        )
    )
    parser.add_argument("--h5i-db", required=True, help="absolute path to an H5i database")
    parser.add_argument("--calendar-json", required=True,
                        help="absolute path to a JSON calendar with a days or trading_days array")
    parser.add_argument("--start", required=True, help="first signal session (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="last signal session (YYYY-MM-DD)")
    parser.add_argument("--output-root", required=True,
                        help="absolute external directory for immutable staging bundles")
    return parser


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _explicit_file(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise CliInputError(f"{label} path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CliInputError(f"{label} file does not exist or cannot be resolved: {path}") from exc
    if not resolved.is_file():
        raise CliInputError(f"{label} path is not a regular file: {resolved}")
    return resolved


def _strict_day(value: str, label: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise CliInputError(f"{label} must use ISO YYYY-MM-DD format")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CliInputError(f"{label} is not a valid calendar date") from exc


def load_calendar(path: str | Path) -> tuple[list[str], str, str]:
    """Read an explicit session calendar and return sessions, provenance, and file hash."""
    calendar_path = _explicit_file(str(path), "calendar")
    try:
        payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliInputError(f"calendar JSON cannot be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise CliInputError("calendar JSON must be an object")
    choices = [key for key in ("days", "trading_days") if key in payload]
    if len(choices) != 1:
        raise CliInputError("calendar JSON must contain exactly one days or trading_days array")
    raw_sessions = payload[choices[0]]
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise CliInputError("calendar session array must be a non-empty list")
    sessions = [_strict_day(value, "calendar session") for value in raw_sessions]
    if len(sessions) != len(set(sessions)):
        raise CliInputError("calendar sessions must be unique")
    if sessions != sorted(sessions):
        raise CliInputError("calendar sessions must be in chronological order")

    provenance = payload.get("provenance", payload.get("source", "unknown"))
    if provenance is None or provenance == "":
        provenance = "unknown"
    if isinstance(provenance, (dict, list)):
        provenance = json.dumps(provenance, ensure_ascii=False, sort_keys=True)
    elif not isinstance(provenance, str):
        provenance = str(provenance)
    return sessions, provenance, _sha256_file(calendar_path)


def validate_signal_range(calendar_path: str | Path, start: str, end: str) -> tuple[list[str], int]:
    sessions, _provenance, _digest = load_calendar(calendar_path)
    start_day = _strict_day(start, "start")
    end_day = _strict_day(end, "end")
    if start_day > end_day:
        raise CliInputError("signal date range start must be <= end")
    if start_day not in sessions or end_day not in sessions:
        raise CliInputError("signal range bounds must both be sessions in the explicit calendar")
    start_index, end_index = sessions.index(start_day), sessions.index(end_day)
    count = end_index - start_index + 1
    if count > MAX_SIGNAL_SESSIONS:
        raise CliInputError(f"signal range exceeds the {MAX_SIGNAL_SESSIONS}-session bound")
    return sessions, count


def _external_output_root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise CliInputError("output-root path must be absolute")
    try:
        resolved = path.resolve(strict=False)
        repo = REPO_ROOT.resolve()
    except (OSError, RuntimeError) as exc:
        raise CliInputError(f"output-root path cannot be resolved: {path}") from exc
    try:
        resolved.relative_to(repo)
    except ValueError:
        return resolved
    raise CliInputError("output-root must be outside the repository and protected data")


def _schema_identity(schema: dict[str, list[str]]) -> tuple[dict[str, list[str]], str]:
    normalized = {str(table): sorted({str(column).lower() for column in columns})
                  for table, columns in sorted(schema.items())}
    return normalized, _sha256_bytes(_canonical_json(normalized))


def _code_identity() -> tuple[dict[str, str], str]:
    hashes = {}
    for relative in CODE_FILES:
        path = REPO_ROOT / relative
        if not path.is_file():
            raise CliInputError(f"required implementation source is missing: {relative}")
        hashes[relative] = _sha256_file(path)
    return hashes, _sha256_bytes(_canonical_json(hashes))


def _factor_contract_payload() -> dict:
    contracts = {key: asdict(contract) for key, contract in FACTOR_CONTRACTS.items()}
    return {
        "schema_version": 1,
        "factors": contracts,
        "unsupported": {
            key: contract.unsupported_reason
            for key, contract in FACTOR_CONTRACTS.items()
            if contract.source_status == "unsupported"
        },
        "orientation_rule": "directed_ic = raw_or_stored_ic * expected_direction exactly once",
        "volatility_rule": "emit selector-facing low-vol score and raw annualized volatility separately",
        "vol_legacy_parity": "unresolved; raw and selector-facing vol ICs are reported separately",
        "momentum_rule": "store raw mom_20; apply expected direction -1 exactly once",
    }


def _panels_to_artifacts(panels: dict, schema: dict, observed_sessions: list[str]) -> dict[str, bytes]:
    required = {"factor_values", "labels", "ic", "coverage"}
    if set(panels) != required:
        raise CliInputError(f"panel builder returned unexpected artifacts: {sorted(panels)}")
    artifacts = {}
    for key, filename in (
        ("factor_values", "factor_values.csv"),
        ("labels", "forward_labels.csv"),
        ("ic", "factor_ic.csv"),
        ("coverage", "coverage.csv"),
    ):
        frame = panels[key]
        if not isinstance(frame, pd.DataFrame):
            raise CliInputError(f"panel {key} is not a DataFrame")
        artifacts[filename] = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    artifacts["factor_contract.json"] = _canonical_json(_factor_contract_payload()) + b"\n"
    artifacts["source_schema.json"] = _canonical_json(schema) + b"\n"
    artifacts["observed_h5i_sessions.json"] = _canonical_json({
        "sessions": observed_sessions,
        "count": len(observed_sessions),
        "meaning": "sessions with at least one observed H5i daily_bars row in the bounded extraction range",
    }) + b"\n"
    return artifacts


def _session_fetch_bounds(sessions: Sequence[str], start: str, end: str) -> tuple[str, str]:
    start_index, end_index = sessions.index(start), sessions.index(end)
    fetch_start = sessions[max(0, start_index - 21)]
    fetch_end = sessions[min(len(sessions) - 1, end_index + max(HORIZONS) - 1)]
    return fetch_start, fetch_end


def _status_summary(frame) -> dict[str, dict[str, int]]:
    if not isinstance(frame, pd.DataFrame) or not {"factor", "status"}.issubset(frame.columns):
        return {}
    counts = frame.groupby(["factor", "status"], dropna=False).size()
    summary: dict[str, dict[str, int]] = {}
    for (factor, status), count in counts.items():
        summary.setdefault(str(factor), {})[str(status)] = int(count)
    return summary


def _coverage_summary(frame) -> list[dict[str, int | str | None]]:
    if not isinstance(frame, pd.DataFrame) or not {"factor", "horizon", "status"}.issubset(frame.columns):
        raise CliInputError("coverage panel is missing factor, horizon, or status")
    result = []
    for (factor, horizon, status), group in frame.groupby(
        ["factor", "horizon", "status"], dropna=False, sort=True
    ):
        summary: dict[str, int | str | None] = {
            "factor": str(factor),
            "horizon": int(horizon) if pd.notna(horizon) else None,
            "status": str(status),
            "rows": int(len(group)),
        }
        for column in ("universe_count", "signal_valid_count", "label_valid_count", "pair_count"):
            if column in group.columns:
                summary[column] = int(pd.to_numeric(group[column], errors="coerce").fillna(0).sum())
        result.append(summary)
    return result


def _execute(args: argparse.Namespace) -> Path:
    h5i_path = _explicit_file(args.h5i_db, "H5i database")
    calendar_path = _explicit_file(args.calendar_json, "calendar")
    output_root = _external_output_root(args.output_root)
    sessions, signal_count = validate_signal_range(calendar_path, args.start, args.end)
    start_day = _strict_day(args.start, "start")
    end_day = _strict_day(args.end, "end")
    calendar_sessions, calendar_provenance, calendar_sha = load_calendar(calendar_path)
    if calendar_sessions != sessions:
        raise CliInputError("calendar changed while inputs were being validated")

    h5i_sha = _sha256_file(h5i_path)
    code_files, code_sha = _code_identity()
    fetch_start, fetch_end = _session_fetch_bounds(sessions, start_day, end_day)

    with H5iReadOnlySource(str(h5i_path)) as source:
        schema, schema_sha = _schema_identity(source.schema())
        observed_sessions = source.trading_sessions(fetch_start, fetch_end)
        panels = build_diagnostic_panels(source, sessions, start_day, end_day)

    # Re-hash source inputs before finalization so a changed input cannot be mislabeled
    # with the initial identity. Opening the database is explicitly read-only.
    if _sha256_file(h5i_path) != h5i_sha:
        raise CliInputError("H5i source changed during the read-only diagnostic")
    if _sha256_file(calendar_path) != calendar_sha:
        raise CliInputError("calendar file changed during the diagnostic")

    identity = {
        "diagnostic_name": "h5i_selector_factor_ic",
        "h5i_db_name": h5i_path.name,
        "h5i_db_size_bytes": h5i_path.stat().st_size,
        "h5i_db_sha256": h5i_sha,
        "calendar_name": calendar_path.name,
        "calendar_sha256": calendar_sha,
        "calendar_provenance": calendar_provenance,
        "calendar_upstream_source_verified": False,
        "calendar_provenance_note": (
            "Metadata is caller-supplied and has not been independently verified; "
            "the calendar file SHA-256 is the reproducible local identity."
        ),
        "signal_range": [start_day, end_day],
        "signal_session_count": signal_count,
        "max_signal_sessions": MAX_SIGNAL_SESSIONS,
        "extraction_range": [fetch_start, fetch_end],
        "horizons": list(HORIZONS),
        "timezone": "Asia/Shanghai",
        "feature_cutoff": "09:25; immediately preceding completed calendar session",
        "universe_definition": "symbols with valid daily bar on the prior calendar session",
        "label_version": LABEL_VERSION,
        "entry_proxy": "daily_open[D]",
        "horizon_exit": "close[D+h-1]",
        "label_formula": (
            "(close[D]/open[D]) * product(1 + change_pct[s]/100 for "
            "s=D+1..D+h-1) - 1; change_pct is percentage points"
        ),
        "paperbook_fill_replay": False,
        "code_files_sha256": code_files,
        "code_sha256": code_sha,
        "schema_sha256": schema_sha,
        "observed_h5i_session_count": len(observed_sessions),
        "coverage_summary": _coverage_summary(panels["coverage"]),
        "direction_contract": {
            key: {"stored_orientation": contract.stored_orientation,
                  "expected_direction": contract.expected_direction,
                  "source_status": contract.source_status,
                  "unsupported_reason": contract.unsupported_reason}
            for key, contract in FACTOR_CONTRACTS.items()
        },
        "vol_legacy_parity": "unresolved; raw and selector-facing vol ICs are reported separately",
        "unsupported_factors": {
            key: contract.unsupported_reason
            for key, contract in FACTOR_CONTRACTS.items()
            if contract.source_status == "unsupported"
        },
    }
    artifacts = _panels_to_artifacts(panels, schema, observed_sessions)
    bundle = write_diagnostic_bundle(output_root, identity, artifacts)
    verify_diagnostic_bundle(bundle)
    return bundle


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bundle = _execute(args)
        with (bundle / "manifest.json").open("r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        panels = {}
        with (bundle / "factor_ic.csv").open("r", encoding="utf-8") as stream:
            ic_frame = pd.read_csv(stream)
        panels["factor_ic"] = _status_summary(ic_frame)
        print(json.dumps({
            "bundle": str(bundle),
            "bundle_sha256": manifest["bundle_sha256"],
            "diagnostic_only": manifest["diagnostic_only"],
            "authoritative": manifest["authoritative"],
            "observation_eligible": manifest["observation_eligible"],
            "factor_statuses": panels["factor_ic"],
            "unsupported_factors": manifest["identity"]["unsupported_factors"],
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except (CliInputError, H5iInputError, H5iSchemaError, DiagnosticBundleError,
            OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
