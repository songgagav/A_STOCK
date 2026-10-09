# -*- coding: utf-8 -*-
"""Explicit-input, immutable continuous daily OOS dataset builder.

This module is an offline evidence compositor.  It accepts an explicit ordered
trade-day list and explicit finalized Evidence Bundle references.  It never
searches a data directory, chooses a latest file, reads the wall clock, or
calls a production selector, target builder, broker, PaperBook, or DRL path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from evidence_bundle import BundleBuildError, verify_bundle
from observation_epoch import (
    build_observation_epoch,
    validate_shadow_production_state,
    verify_observation_epoch,
)


SCHEMA_VERSION = 1
BUILDER_VERSION = "oos-dataset-phase-c-1"
VALID_STATUSES = {
    "available",
    "pending_maturity",
    "not_applicable",
    "missing",
    "blocked",
    "invalid",
    "tampered",
}
_STATUS_PRIORITY = {
    "available": 0,
    "not_applicable": 0,
    "pending_maturity": 1,
    "missing": 2,
    "blocked": 3,
    "invalid": 4,
    "tampered": 5,
}
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class OOSDatasetBuildError(RuntimeError):
    """Fail-closed dataset construction or verification error."""

    def __init__(self, status: str, reasons: list[str]):
        self.status = status
        self.reasons = list(reasons)
        super().__init__(f"OOS dataset {status}: {'; '.join(self.reasons)}")


@dataclass(frozen=True)
class OOSDayInput:
    """One explicit day-to-bundle mapping.

    A non-available day is still a first-class row.  Its absent bundle is
    represented by ``status`` and ``reason`` rather than a fabricated empty
    metric.
    """

    trade_day: str
    bundle_path: Path | str | None
    expected_manifest_sha256: str | None
    bundle_id: str | None
    status: str
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_path", Path(self.bundle_path) if self.bundle_path is not None else None)
        if not str(self.trade_day):
            raise ValueError("trade_day is required")
        try:
            date.fromisoformat(str(self.trade_day))
        except ValueError as exc:
            raise ValueError("trade_day must be ISO YYYY-MM-DD") from exc
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid day status: {self.status!r}")
        if self.expected_manifest_sha256 is not None and not _SHA256.fullmatch(self.expected_manifest_sha256):
            raise ValueError("expected_manifest_sha256 must be a SHA-256 digest")
        if self.status == "available":
            if self.bundle_path is None:
                raise ValueError("available day requires bundle_path")
            if self.expected_manifest_sha256 is None:
                raise ValueError("available day requires expected_manifest_sha256")
            if not self.bundle_id:
                raise ValueError("available day requires bundle_id")


@dataclass(frozen=True)
class OOSDatasetRequest:
    """All explicit identities and day inputs required for one dataset."""

    output_root: Path | str
    run_id: str
    generated_at: str
    code_sha: str
    data_identity: dict[str, Any]
    config_identity: dict[str, Any]
    experiment_identity: dict[str, Any]
    calendar_identity: dict[str, Any]
    production_state: dict[str, Any]
    trade_days: tuple[str, ...]
    days: tuple[OOSDayInput, ...]
    builder_version: str = BUILDER_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_root", Path(self.output_root))
        object.__setattr__(self, "data_identity", dict(self.data_identity))
        object.__setattr__(self, "config_identity", dict(self.config_identity))
        object.__setattr__(self, "experiment_identity", dict(self.experiment_identity))
        object.__setattr__(self, "calendar_identity", dict(self.calendar_identity))
        object.__setattr__(self, "production_state", dict(self.production_state))
        object.__setattr__(self, "trade_days", tuple(str(day) for day in self.trade_days))
        object.__setattr__(self, "days", tuple(self.days))
        if not str(self.run_id):
            raise ValueError("run_id is required")
        if not str(self.generated_at):
            raise ValueError("generated_at is required; the builder never uses the clock")
        if not str(self.code_sha):
            raise ValueError("code_sha is required")
        for identity_name, identity in (
            ("data_identity", self.data_identity),
            ("config_identity", self.config_identity),
            ("experiment_identity", self.experiment_identity),
            ("calendar_identity", self.calendar_identity),
        ):
            if not isinstance(identity, dict) or not identity:
                raise ValueError(f"{identity_name} is required")
        if not str(self.data_identity.get("data_sha") or ""):
            raise ValueError("data_identity.data_sha is required")
        if not str(self.config_identity.get("config_sha") or ""):
            raise ValueError("config_identity.config_sha is required")
        if not str(self.experiment_identity.get("experiment_hash") or ""):
            raise ValueError("experiment_identity.experiment_hash is required")
        if not str(self.calendar_identity.get("calendar_sha") or ""):
            raise ValueError("calendar_identity.calendar_sha is required")
        if self.calendar_identity.get("trade_days_are_explicit") is not True:
            raise ValueError("calendar_identity.trade_days_are_explicit must be true")
        if not self.trade_days:
            raise ValueError("trade_days is required")
        try:
            parsed_days = [date.fromisoformat(day) for day in self.trade_days]
        except ValueError as exc:
            raise ValueError("trade_days must be ISO YYYY-MM-DD") from exc
        if parsed_days != sorted(parsed_days):
            raise ValueError("trade_days must be sorted")
        if len(set(self.trade_days)) != len(self.trade_days):
            raise ValueError("trade_days must be unique")
        if len(self.days) != len(self.trade_days) or {day.trade_day for day in self.days} != set(self.trade_days):
            raise ValueError("one day input per trade_day is required")
        validate_shadow_production_state(self.production_state)


@dataclass(frozen=True)
class OOSDatasetResult:
    path: Path
    dataset_id: str
    manifest: dict[str, Any]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _manifest_without_hash(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "manifest_hash"}


def _aggregate_status(statuses: list[str]) -> str:
    if not statuses:
        return "available"
    return max(statuses, key=lambda status: _STATUS_PRIORITY[status])


def _read_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OOSDatasetBuildError("invalid", [reason]) from exc
    if not isinstance(value, dict):
        raise OOSDatasetBuildError("invalid", [reason])
    return value


def _load_day(
    day: OOSDayInput,
    expected_observation_epoch: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if day.status != "available":
        row = {
            "trade_day": day.trade_day,
            "status": day.status,
            "reason": day.reason,
            "bundle_id": day.bundle_id,
            "bundle_manifest_sha256": day.expected_manifest_sha256,
            "turnover": None,
            "cost_summary": None,
            "source_artifact_hashes": {},
        }
        raw = {
            "trade_day": day.trade_day,
            "status": day.status,
            "reason": day.reason,
            "bundle_path": str(day.bundle_path) if day.bundle_path is not None else None,
            "bundle_id": day.bundle_id,
            "bundle_manifest_sha256": day.expected_manifest_sha256,
            "constituent_artifact_hashes": {},
        }
        return raw, row

    if day.bundle_path is None or day.expected_manifest_sha256 is None:
        raise OOSDatasetBuildError("invalid", [f"available_day_inputs_missing:{day.trade_day}"])
    manifest_path = day.bundle_path / "manifest.json"
    if not manifest_path.is_file():
        raise OOSDatasetBuildError("missing", [f"manifest_missing:{day.trade_day}"])
    actual_manifest_hash = _file_digest(manifest_path)
    if actual_manifest_hash.lower() != day.expected_manifest_sha256.lower():
        raise OOSDatasetBuildError("tampered", [f"manifest_hash_mismatch:{day.trade_day}"])
    try:
        manifest = verify_bundle(day.bundle_path)
    except BundleBuildError as exc:
        raise OOSDatasetBuildError(exc.status, [f"{day.trade_day}:{reason}" for reason in exc.reasons]) from exc
    if manifest.get("trade_day") != day.trade_day:
        raise OOSDatasetBuildError("invalid", [f"bundle_trade_day_mismatch:{day.trade_day}"])
    if manifest.get("bundle_id") != day.bundle_id:
        raise OOSDatasetBuildError("tampered", [f"bundle_id_mismatch:{day.trade_day}"])
    if expected_observation_epoch is not None and manifest.get("observation_epoch") != expected_observation_epoch:
        raise OOSDatasetBuildError("blocked", [f"observation_epoch_mismatch:{day.trade_day}"])
    turnover = _read_json(day.bundle_path / "derived" / "turnover.json", f"turnover_unreadable:{day.trade_day}")
    cost_summary = _read_json(day.bundle_path / "derived" / "cost_summary.json", f"cost_summary_unreadable:{day.trade_day}")
    effective_status = str(manifest.get("evidence_status") or "invalid")
    if effective_status not in VALID_STATUSES:
        raise OOSDatasetBuildError("invalid", [f"bundle_evidence_status:{day.trade_day}"])
    row = {
        "trade_day": day.trade_day,
        "status": effective_status,
        "reason": None,
        "bundle_id": day.bundle_id,
        "bundle_manifest_sha256": actual_manifest_hash,
        "turnover": turnover,
        "cost_summary": cost_summary,
        "source_artifact_hashes": manifest.get("constituent_artifact_hashes", {}),
        "blocked_reasons": manifest.get("blocked_reasons", []),
    }
    raw = {
        "trade_day": day.trade_day,
        "status": effective_status,
        "reason": None,
        "bundle_path": str(day.bundle_path),
        "bundle_id": day.bundle_id,
        "bundle_manifest_sha256": actual_manifest_hash,
        "constituent_artifact_hashes": manifest.get("constituent_artifact_hashes", {}),
        "raw_artifacts": manifest.get("raw_artifacts", {}),
    }
    return raw, row


def _identity(request: OOSDatasetRequest, raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    observation_epoch = build_observation_epoch(
        code_sha=request.code_sha,
        data_identity=request.data_identity,
        config_identity=request.config_identity,
        experiment_identity=request.experiment_identity,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": request.run_id,
        "generated_at": request.generated_at,
        "code_sha": request.code_sha,
        "data_identity": request.data_identity,
        "config_identity": request.config_identity,
        "experiment_identity": request.experiment_identity,
        "calendar_identity": request.calendar_identity,
        "production_state": request.production_state,
        "trade_days": list(request.trade_days),
        "days": [
            {
                "trade_day": row["trade_day"],
                "status": row["status"],
                "bundle_id": row.get("bundle_id"),
                "bundle_manifest_sha256": row.get("bundle_manifest_sha256"),
                "constituent_artifact_hashes": row.get("constituent_artifact_hashes", {}),
            }
            for row in raw_rows
        ],
        "builder_version": request.builder_version,
        "observation_epoch": observation_epoch,
    }


def _result_from_manifest(path: Path, manifest: dict[str, Any]) -> OOSDatasetResult:
    return OOSDatasetResult(path=path, dataset_id=str(manifest["dataset_id"]), manifest=manifest)


def build_oos_dataset(request: OOSDatasetRequest) -> OOSDatasetResult:
    """Build one immutable dataset from the caller's explicit day list."""

    day_by_date = {day.trade_day: day for day in request.days}
    observation_epoch = build_observation_epoch(
        code_sha=request.code_sha,
        data_identity=request.data_identity,
        config_identity=request.config_identity,
        experiment_identity=request.experiment_identity,
    )
    raw_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    for trade_day in request.trade_days:
        raw_row, metric_row = _load_day(day_by_date[trade_day], observation_epoch)
        raw_rows.append(raw_row)
        metric_rows.append(metric_row)

    identity = _identity(request, raw_rows)
    dataset_id = _digest(identity)
    output_root = request.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    final_path = output_root / dataset_id
    if final_path.exists():
        try:
            existing = verify_oos_dataset(final_path)
        except OOSDatasetBuildError:
            raise
        if existing.get("dataset_id") == dataset_id:
            return _result_from_manifest(final_path, existing)
        raise OOSDatasetBuildError("blocked", ["finalized_dataset_id_conflict"])

    temp_path = Path(tempfile.mkdtemp(prefix=f".{dataset_id}.", dir=str(output_root)))
    try:
        _write_jsonl(temp_path / "raw" / "day_index.jsonl", raw_rows)
        _write_jsonl(temp_path / "derived" / "daily_metrics.jsonl", metric_rows)
        statuses = [str(row["status"]) for row in metric_rows]
        blocked_reasons = [
            f"{row['trade_day']}:{row.get('reason') or 'bundle_blocked'}"
            for row in metric_rows
            if row["status"] in {"missing", "blocked", "invalid", "tampered"}
        ]
        summary = {
            "schema_version": SCHEMA_VERSION,
            "trade_days": list(request.trade_days),
            "n_days": len(metric_rows),
            "daily_available": sum(status == "available" for status in statuses),
            "status_counts": {status: statuses.count(status) for status in sorted(set(statuses))},
            "evidence_status": _aggregate_status(statuses),
            "blocked_reasons": blocked_reasons,
        }
        _write_json(temp_path / "derived" / "summary.json", summary)

        constituent_hashes: dict[str, str] = {}
        for file_path in sorted(path for path in temp_path.rglob("*") if path.is_file()):
            relative = file_path.relative_to(temp_path).as_posix()
            constituent_hashes[relative] = _file_digest(file_path)
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": dataset_id,
            "run_id": request.run_id,
            "trade_days": list(request.trade_days),
            "generated_at": request.generated_at,
            "code_sha": request.code_sha,
            "data_sha": request.data_identity.get("data_sha"),
            "data_identity": request.data_identity,
            "config_sha": request.config_identity.get("config_sha"),
            "config_identity": request.config_identity,
            "experiment_hash": request.experiment_identity.get("experiment_hash"),
            "experiment_identity": request.experiment_identity,
            "observation_epoch": identity["observation_epoch"],
            "calendar_identity": request.calendar_identity,
            "builder_version": request.builder_version,
            "evidence_status": summary["evidence_status"],
            "blocked_reasons": blocked_reasons,
            "daily_available": summary["daily_available"],
            "status_counts": summary["status_counts"],
            "production_state": request.production_state,
            "constituent_artifact_hashes": constituent_hashes,
            "dataset_identity": identity,
            "manifest_hash": None,
        }
        manifest["manifest_hash"] = _digest(_manifest_without_hash(manifest))
        _write_json(temp_path / "manifest.json", manifest)
        for relative, expected in constituent_hashes.items():
            if _file_digest(temp_path / relative) != expected:
                raise OOSDatasetBuildError("tampered", [f"staged_constituent_hash_mismatch:{relative}"])
        if _digest(_manifest_without_hash(manifest)) != manifest["manifest_hash"]:
            raise OOSDatasetBuildError("tampered", ["staged_manifest_hash_mismatch"])
        try:
            os.replace(str(temp_path), str(final_path))
        except FileExistsError:
            shutil.rmtree(temp_path, ignore_errors=True)
            existing = verify_oos_dataset(final_path)
            if existing.get("dataset_id") == dataset_id:
                return _result_from_manifest(final_path, existing)
            raise OOSDatasetBuildError("blocked", ["finalized_dataset_id_conflict"])
        return _result_from_manifest(final_path, manifest)
    except Exception:
        if temp_path.exists():
            shutil.rmtree(temp_path, ignore_errors=True)
        raise


def verify_oos_dataset(dataset_path: Path | str) -> dict[str, Any]:
    """Verify the final manifest commit marker and all derived constituents."""

    path = Path(dataset_path)
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise OOSDatasetBuildError("blocked", ["manifest_missing"])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OOSDatasetBuildError("tampered", ["manifest_unreadable"]) from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise OOSDatasetBuildError("invalid", ["manifest_schema_version"])
    if not _SHA256.fullmatch(str(manifest.get("manifest_hash") or "")):
        raise OOSDatasetBuildError("tampered", ["manifest_hash_missing"])
    if _digest(_manifest_without_hash(manifest)) != manifest["manifest_hash"]:
        raise OOSDatasetBuildError("tampered", ["manifest_hash_mismatch"])
    if path.name != str(manifest.get("dataset_id")):
        raise OOSDatasetBuildError("tampered", ["dataset_id_path_mismatch"])
    identity = manifest.get("dataset_identity")
    if not isinstance(identity, dict) or _digest(identity) != manifest.get("dataset_id"):
        raise OOSDatasetBuildError("tampered", ["dataset_identity_mismatch"])
    if "observation_epoch" in manifest:
        try:
            verify_observation_epoch(manifest["observation_epoch"])
        except ValueError as exc:
            raise OOSDatasetBuildError("tampered", ["observation_epoch_mismatch"]) from exc
    for relative, expected in (manifest.get("constituent_artifact_hashes") or {}).items():
        file_path = path / relative
        if not file_path.is_file():
            raise OOSDatasetBuildError("tampered", [f"constituent_missing:{relative}"])
        if _file_digest(file_path) != expected:
            raise OOSDatasetBuildError("tampered", [f"constituent_hash_mismatch:{relative}"])
    if manifest.get("evidence_status") not in VALID_STATUSES:
        raise OOSDatasetBuildError("invalid", ["manifest_evidence_status"])
    return manifest
