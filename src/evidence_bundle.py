# -*- coding: utf-8 -*-
"""Immutable, explicit-input Evidence Bundle builder.

Phase A is deliberately an offline side channel.  This module never searches
for latest files, derives a trading day from the clock, mutates production
state, or imports a selector/broker/DRL runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evidence_cost import replay_costs
from evidence_turnover import compute_turnover
from observation_epoch import (
    build_observation_epoch,
    validate_shadow_production_state,
    verify_observation_epoch,
)


SCHEMA_VERSION = 1
BUILDER_VERSION = "evidence-bundle-phase-a-1"
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
_REQUIRED_ARTIFACTS = {
    "market_data",
    "positions_before",
    "positions_after",
    "target_positions",
    "orders",
    "fills",
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class BundleBuildError(RuntimeError):
    """A fail-closed bundle construction or integrity error."""

    def __init__(self, status: str, reasons: list[str]):
        self.status = status
        self.reasons = list(reasons)
        super().__init__(f"Evidence Bundle {status}: {'; '.join(self.reasons)}")


@dataclass(frozen=True)
class ArtifactInput:
    """One explicitly supplied source artifact and its expected digest."""

    name: str
    source_path: Path | str
    expected_sha256: str
    format: str = "json"

    def __post_init__(self) -> None:
        if not _SAFE_NAME.fullmatch(str(self.name)):
            raise ValueError(f"invalid artifact name: {self.name!r}")
        object.__setattr__(self, "source_path", Path(self.source_path))
        if not _SHA256.fullmatch(str(self.expected_sha256)):
            raise ValueError(f"expected_sha256 must be a SHA-256 digest: {self.name}")
        if self.format not in {"json", "jsonl", "bytes"}:
            raise ValueError(f"unsupported artifact format: {self.format!r}")


@dataclass(frozen=True)
class EvidenceBundleRequest:
    """All formal inputs required for one deterministic bundle build."""

    output_root: Path | str
    trade_day: str
    generated_at: str
    run_id: str
    code_sha: str
    data_identity: dict[str, Any]
    config_identity: dict[str, Any]
    snapshot: ArtifactInput
    artifacts: dict[str, ArtifactInput]
    experiment_identity: dict[str, Any]
    production_state: dict[str, Any]
    reference_equity: float
    reference_timestamp: str
    cost_evidence_level: str
    observation_statuses: dict[str, str] = field(default_factory=dict)
    builder_version: str = BUILDER_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_root", Path(self.output_root))
        object.__setattr__(self, "artifacts", dict(self.artifacts))
        object.__setattr__(self, "data_identity", dict(self.data_identity))
        object.__setattr__(self, "config_identity", dict(self.config_identity))
        object.__setattr__(self, "experiment_identity", dict(self.experiment_identity))
        object.__setattr__(self, "production_state", dict(self.production_state))
        object.__setattr__(self, "observation_statuses", dict(self.observation_statuses))
        if not str(self.trade_day):
            raise ValueError("trade_day is required")
        if not str(self.generated_at):
            raise ValueError("generated_at is required; the builder never uses the clock")
        if not str(self.run_id):
            raise ValueError("run_id is required")
        if not str(self.code_sha):
            raise ValueError("code_sha is required")
        if not str(self.data_identity.get("data_sha") or ""):
            raise ValueError("data_identity.data_sha is required")
        if not str(self.config_identity.get("config_sha") or ""):
            raise ValueError("config_identity.config_sha is required")
        if not str(self.experiment_identity.get("experiment_hash") or ""):
            raise ValueError("experiment_identity.experiment_hash is required")
        validate_shadow_production_state(self.production_state)
        try:
            equity = float(self.reference_equity)
        except (TypeError, ValueError) as exc:
            raise ValueError("reference_equity must be finite and positive") from exc
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("reference_equity must be finite and positive")
        if not str(self.reference_timestamp):
            raise ValueError("reference_timestamp is required")
        if self.cost_evidence_level not in {"estimated", "simulated", "realized"}:
            raise ValueError(f"invalid cost_evidence_level: {self.cost_evidence_level!r}")
        if self.snapshot.name != "snapshot":
            raise ValueError("snapshot artifact must have name='snapshot'")
        missing = sorted(_REQUIRED_ARTIFACTS - set(self.artifacts))
        if missing:
            raise ValueError(f"required artifacts missing: {', '.join(missing)}")
        for name, artifact in self.artifacts.items():
            if name != artifact.name:
                raise ValueError(f"artifact mapping key/name mismatch: {name!r}/{artifact.name!r}")
        for name, status in self.observation_statuses.items():
            if status not in VALID_STATUSES:
                raise ValueError(f"invalid observation status {name!r}: {status!r}")


@dataclass(frozen=True)
class BundleResult:
    path: Path
    bundle_id: str
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
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_payload(artifact: ArtifactInput, data: bytes) -> Any:
    if artifact.format == "bytes":
        return data
    text = data.decode("utf-8")
    if artifact.format == "json":
        return json.loads(text)
    rows = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BundleBuildError("invalid", [f"{artifact.name}: invalid JSONL line {line_number}"]) from exc
    return rows


def _read_and_verify(artifact: ArtifactInput) -> tuple[bytes, Any, dict[str, Any]]:
    if not artifact.source_path.exists():
        raise BundleBuildError("missing", [f"artifact_missing:{artifact.name}"])
    if not artifact.source_path.is_file():
        raise BundleBuildError("invalid", [f"artifact_not_file:{artifact.name}"])
    data = artifact.source_path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual.lower() != artifact.expected_sha256.lower():
        raise BundleBuildError("tampered", [f"artifact_hash_mismatch:{artifact.name}"])
    try:
        payload = _read_payload(artifact, data)
    except UnicodeDecodeError as exc:
        raise BundleBuildError("invalid", [f"artifact_not_utf8:{artifact.name}"]) from exc
    metadata = {
        "source_path": str(artifact.source_path),
        "source_sha256": actual,
        "format": artifact.format,
    }
    return data, payload, metadata


def _require_mapping(payload: Any, name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BundleBuildError("invalid", [f"{name}_must_be_object"])
    return payload


def _require_rows(payload: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise BundleBuildError("invalid", [f"{name}_must_be_object_array"])
    return payload


def _aggregate_status(field_statuses: dict[str, str]) -> str:
    if not field_statuses:
        return "available"
    return max(field_statuses.values(), key=lambda status: _STATUS_PRIORITY[status])


def _write_cost_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _manifest_without_hash(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "manifest_hash"}


def _build_identity(request: EvidenceBundleRequest, source_hashes: dict[str, str]) -> dict[str, Any]:
    observation_epoch = build_observation_epoch(
        code_sha=request.code_sha,
        data_identity=request.data_identity,
        config_identity=request.config_identity,
        experiment_identity=request.experiment_identity,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "trade_day": request.trade_day,
        "generated_at": request.generated_at,
        "run_id": request.run_id,
        "code_sha": request.code_sha,
        "data_identity": request.data_identity,
        "config_identity": request.config_identity,
        "snapshot_hash": source_hashes["snapshot"],
        "experiment_identity": request.experiment_identity,
        "source_artifact_hashes": source_hashes,
        "builder_version": request.builder_version,
        "production_state": request.production_state,
        "reference_equity": float(request.reference_equity),
        "reference_timestamp": request.reference_timestamp,
        "cost_evidence_level": request.cost_evidence_level,
        "observation_statuses": request.observation_statuses,
        "observation_epoch": observation_epoch,
    }


def _result_from_manifest(path: Path, manifest: dict[str, Any]) -> BundleResult:
    return BundleResult(path=path, bundle_id=str(manifest["bundle_id"]), manifest=manifest)


def build_bundle(request: EvidenceBundleRequest) -> BundleResult:
    """Build or idempotently return one immutable finalized bundle."""

    all_inputs = [request.snapshot, *[request.artifacts[name] for name in sorted(request.artifacts)]]
    loaded: dict[str, tuple[ArtifactInput, bytes, Any, dict[str, Any]]] = {}
    for artifact in all_inputs:
        data, payload, metadata = _read_and_verify(artifact)
        loaded[artifact.name] = (artifact, data, payload, metadata)

    source_hashes = {name: loaded[name][3]["source_sha256"] for name in sorted(loaded)}
    identity = _build_identity(request, source_hashes)
    bundle_id = _digest(identity)
    output_root = request.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    final_path = output_root / bundle_id

    if final_path.exists():
        try:
            existing = verify_bundle(final_path)
        except BundleBuildError:
            raise
        if existing.get("bundle_id") == bundle_id:
            return _result_from_manifest(final_path, existing)
        raise BundleBuildError("blocked", ["finalized_bundle_id_conflict"])

    temp_path = Path(tempfile.mkdtemp(prefix=f".{bundle_id}.", dir=str(output_root)))
    try:
        raw_dir = temp_path / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(loaded):
            artifact, data, _payload, _metadata = loaded[name]
            suffix = artifact.source_path.suffix or ".bin"
            (raw_dir / f"{name}{suffix}").write_bytes(data)

        positions_before = _require_mapping(loaded["positions_before"][2], "positions_before")
        _require_mapping(loaded["positions_after"][2], "positions_after")
        target_positions = _require_mapping(loaded["target_positions"][2], "target_positions")
        planned_orders = _require_rows(loaded["orders"][2], "orders")
        fills = _require_rows(loaded["fills"][2], "fills")

        turnover = compute_turnover(
            previous_actual_weights=positions_before,
            target_weights=target_positions,
            planned_orders=planned_orders,
            fills=fills,
            reference_equity=request.reference_equity,
            reference_timestamp=request.reference_timestamp,
        )
        costs = replay_costs(fills, evidence_level=request.cost_evidence_level)
        field_statuses = dict(request.observation_statuses)
        field_statuses.update({f"turnover.{name}": value["status"] for name, value in turnover["metrics"].items()})
        field_statuses["cost_replay"] = costs["status"]
        evidence_status = _aggregate_status(field_statuses)
        blocked_reasons = [
            f"{name}:{status}"
            for name, status in sorted(field_statuses.items())
            if status in {"missing", "blocked", "invalid", "tampered"}
        ]

        _write_json(temp_path / "derived" / "turnover.json", turnover)
        _write_cost_records(temp_path / "derived" / "cost_records.jsonl", costs["records"])
        _write_json(temp_path / "derived" / "cost_summary.json", costs["summary"])
        _write_json(temp_path / "derived" / "status.json", {
            "schema_version": SCHEMA_VERSION,
            "field_statuses": field_statuses,
            "evidence_status": evidence_status,
            "blocked_reasons": blocked_reasons,
        })

        constituent_hashes: dict[str, str] = {}
        raw_metadata: dict[str, Any] = {}
        for name in sorted(loaded):
            artifact, _data, _payload, metadata = loaded[name]
            relative = f"raw/{name}{artifact.source_path.suffix or '.bin'}"
            file_path = temp_path / relative
            constituent_hashes[relative] = _file_digest(file_path)
            raw_metadata[name] = {
                **metadata,
                "bundle_path": relative,
                "expected_sha256": artifact.expected_sha256,
            }
        derived_metadata: dict[str, Any] = {}
        for file_path in sorted((temp_path / "derived").iterdir()):
            relative = file_path.relative_to(temp_path).as_posix()
            constituent_hashes[relative] = _file_digest(file_path)
            derived_metadata[file_path.name] = {
                "bundle_path": relative,
                "sha256": constituent_hashes[relative],
            }

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "run_id": request.run_id,
            "trade_day": request.trade_day,
            "generated_at": request.generated_at,
            "code_sha": request.code_sha,
            "data_sha": request.data_identity["data_sha"],
            "data_identity": request.data_identity,
            "config_sha": request.config_identity["config_sha"],
            "config_identity": request.config_identity,
            "snapshot_hash": source_hashes["snapshot"],
            "snapshot_path": raw_metadata["snapshot"]["source_path"],
            "experiment_hash": request.experiment_identity["experiment_hash"],
            "experiment_identity": request.experiment_identity,
            "observation_epoch": identity["observation_epoch"],
            "builder_version": request.builder_version,
            "evidence_status": evidence_status,
            "blocked_reasons": blocked_reasons,
            "field_statuses": field_statuses,
            "production_state": request.production_state,
            "reference_equity": float(request.reference_equity),
            "reference_timestamp": request.reference_timestamp,
            "cost_evidence_level": request.cost_evidence_level,
            "constituent_artifact_hashes": constituent_hashes,
            "raw_artifacts": raw_metadata,
            "derived_artifacts": derived_metadata,
            "bundle_identity": identity,
            "manifest_hash": None,
        }
        manifest["manifest_hash"] = _digest(_manifest_without_hash(manifest))
        _write_json(temp_path / "manifest.json", manifest)

        # Verify the staged commit marker and every constituent before rename.
        for relative, expected in constituent_hashes.items():
            actual = _file_digest(temp_path / relative)
            if actual != expected:
                raise BundleBuildError("tampered", [f"staged_constituent_hash_mismatch:{relative}"])
        if _digest(_manifest_without_hash(manifest)) != manifest["manifest_hash"]:
            raise BundleBuildError("tampered", ["staged_manifest_hash_mismatch"])

        try:
            os.replace(str(temp_path), str(final_path))
        except FileExistsError:
            # Another identical builder may have won the race.  Never replace
            # or merge an already-finalized directory.
            shutil.rmtree(temp_path, ignore_errors=True)
            existing = verify_bundle(final_path)
            if existing.get("bundle_id") == bundle_id:
                return _result_from_manifest(final_path, existing)
            raise BundleBuildError("blocked", ["finalized_bundle_id_conflict"])
        return _result_from_manifest(final_path, manifest)
    except Exception:
        if temp_path.exists():
            shutil.rmtree(temp_path, ignore_errors=True)
        raise


def verify_bundle(bundle_path: Path | str) -> dict[str, Any]:
    """Verify the immutable manifest commit marker and all constituent hashes."""

    path = Path(bundle_path)
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise BundleBuildError("blocked", ["manifest_missing"])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleBuildError("tampered", ["manifest_unreadable"]) from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleBuildError("invalid", ["manifest_schema_version"])
    if not _SHA256.fullmatch(str(manifest.get("manifest_hash") or "")):
        raise BundleBuildError("tampered", ["manifest_hash_missing"])
    if _digest(_manifest_without_hash(manifest)) != manifest["manifest_hash"]:
        raise BundleBuildError("tampered", ["manifest_hash_mismatch"])
    if path.name != str(manifest.get("bundle_id")):
        raise BundleBuildError("tampered", ["bundle_id_path_mismatch"])
    identity = manifest.get("bundle_identity")
    if not isinstance(identity, dict) or _digest(identity) != manifest.get("bundle_id"):
        raise BundleBuildError("tampered", ["bundle_identity_mismatch"])
    if "observation_epoch" in manifest:
        try:
            verify_observation_epoch(manifest["observation_epoch"])
        except ValueError as exc:
            raise BundleBuildError("tampered", ["observation_epoch_mismatch"]) from exc
    for relative, expected in (manifest.get("constituent_artifact_hashes") or {}).items():
        file_path = path / relative
        if not file_path.is_file():
            raise BundleBuildError("tampered", [f"constituent_missing:{relative}"])
        if _file_digest(file_path) != expected:
            raise BundleBuildError("tampered", [f"constituent_hash_mismatch:{relative}"])
    status = manifest.get("evidence_status")
    if status not in VALID_STATUSES:
        raise BundleBuildError("invalid", ["manifest_evidence_status"])
    return manifest
