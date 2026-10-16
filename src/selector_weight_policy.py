"""Explicit selector-weight policy and immutable effective-weight snapshots.

The live selector must never infer adaptive behavior from an unset setting.
Frozen snapshots are content-addressed by their exact canonical UTF-8 bytes and
published with a create-only hard-link operation so concurrent writers cannot
replace an existing authority or expose a partially written destination.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


FACTOR_KEYS = (
    "signal", "trend", "govern", "liquidity", "vol", "mom_rev",
    "pb_rev", "roe", "mf_net",
)
SNAPSHOT_POLICY = "selector-weight-freeze/v1"
SNAPSHOT_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPO_ROOT = Path(__file__).resolve().parents[1]


class SelectorWeightPolicyError(ValueError):
    """Raised when selector weights or their configured authority are invalid."""


@dataclass(frozen=True)
class SelectorWeightContext:
    weights: dict[str, float]
    identity: dict[str, str]


def selector_weight_mode() -> str:
    """Return the explicitly configured policy; never infer a default."""
    mode = os.environ.get("SELECTOR_WEIGHT_MODE")
    if mode not in {"adaptive", "frozen"}:
        raise SelectorWeightPolicyError(
            "SELECTOR_WEIGHT_MODE must be explicitly set to adaptive or frozen"
        )
    return mode


def _load_adaptive_weights(as_of: str | None = None) -> dict[str, float]:
    """Read the existing effective resolver, retained for adaptive/history use."""
    try:
        from factor_library import _selector_weights_adaptive

        result = _selector_weights_adaptive(as_of=as_of)
    except Exception as exc:  # noqa: BLE001 - expose a typed policy boundary
        raise SelectorWeightPolicyError(
            f"adaptive selector weights could not be resolved: {type(exc).__name__}"
        ) from exc
    if not isinstance(result, dict):
        raise SelectorWeightPolicyError("adaptive selector weights are not a mapping")
    return {str(key): float(value) for key, value in result.items()}


def _validated_weights(weights: Any) -> dict[str, float]:
    if not isinstance(weights, dict) or set(weights) != set(FACTOR_KEYS):
        raise SelectorWeightPolicyError("snapshot weights must contain exactly the nine selector factors")
    result: dict[str, float] = {}
    for key in FACTOR_KEYS:
        value = weights[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SelectorWeightPolicyError(f"snapshot weight {key} must be numeric")
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            raise SelectorWeightPolicyError(f"snapshot weight {key} must be finite and non-negative")
        result[key] = number
    total = math.fsum(result.values())
    if not math.isfinite(total) or not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise SelectorWeightPolicyError("snapshot weights must sum to one within 1e-8")
    return result


def _validate_snapshot(payload: Any) -> dict[str, Any]:
    required = {"schema_version", "policy", "captured_at", "weights", "provenance"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise SelectorWeightPolicyError("snapshot must contain exactly the schema-v1 fields")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
    ):
        raise SelectorWeightPolicyError("unsupported selector-weight snapshot schema")
    if payload.get("policy") != SNAPSHOT_POLICY:
        raise SelectorWeightPolicyError("unsupported selector-weight snapshot policy")
    captured_at = payload.get("captured_at")
    if not isinstance(captured_at, str):
        raise SelectorWeightPolicyError("snapshot captured_at must be a timezone-aware ISO-8601 string")
    try:
        parsed_time = datetime.fromisoformat(captured_at)
    except ValueError as exc:
        raise SelectorWeightPolicyError("snapshot captured_at is not valid ISO-8601") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise SelectorWeightPolicyError("snapshot captured_at must include a timezone")

    provenance = payload.get("provenance")
    provenance_keys = {
        "source_mode", "weights_json_sha256", "factor_health_enabled",
        "factor_health_input_sha256",
    }
    if not isinstance(provenance, dict) or set(provenance) != provenance_keys:
        raise SelectorWeightPolicyError("snapshot provenance fields are invalid")
    if provenance["source_mode"] != "adaptive-effective-selector":
        raise SelectorWeightPolicyError("snapshot source_mode is invalid")
    if not isinstance(provenance["factor_health_enabled"], bool):
        raise SelectorWeightPolicyError("factor_health_enabled must be boolean")
    source_digest = provenance["weights_json_sha256"]
    if source_digest is not None and (
        not isinstance(source_digest, str) or not _SHA256_RE.fullmatch(source_digest)
    ):
        raise SelectorWeightPolicyError("weights_json_sha256 must be null or lowercase SHA-256")
    input_hashes = provenance["factor_health_input_sha256"]
    if not isinstance(input_hashes, dict):
        raise SelectorWeightPolicyError("factor-health input hashes must be a mapping")
    for name, digest in input_hashes.items():
        if not isinstance(name, str) or not name or not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise SelectorWeightPolicyError("factor-health input hash entry is invalid")

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "policy": SNAPSHOT_POLICY,
        "captured_at": captured_at,
        "weights": _validated_weights(payload["weights"]),
        "provenance": provenance,
    }


def _read_frozen_snapshot(repo_root: Path, digest: str) -> dict[str, Any]:
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise SelectorWeightPolicyError(
            "SELECTOR_WEIGHT_SNAPSHOT_SHA256 must be 64 lowercase hexadecimal characters"
        )
    path = repo_root / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SelectorWeightPolicyError("configured selector-weight snapshot is unavailable") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != digest:
        raise SelectorWeightPolicyError("selector-weight snapshot byte hash does not match configured SHA-256")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectorWeightPolicyError("selector-weight snapshot is not valid UTF-8 JSON") from exc
    validated = _validate_snapshot(payload)
    if _canonical_json_bytes(payload) != raw:
        raise SelectorWeightPolicyError("selector-weight snapshot bytes are not canonical JSON")
    return validated


def resolve_selector_weight_context(*, as_of: str | None = None) -> SelectorWeightContext:
    """Resolve effective weights and their identity from one policy read."""
    mode = selector_weight_mode()
    if as_of is not None:
        weights = _validated_weights(_load_adaptive_weights(as_of=as_of))
        return SelectorWeightContext(weights, {"mode": "historical", "as_of": str(as_of)})
    if mode == "adaptive":
        weights = _load_adaptive_weights()
        return SelectorWeightContext(weights, {"mode": "adaptive"})

    digest = os.environ.get("SELECTOR_WEIGHT_SNAPSHOT_SHA256", "")
    payload = _read_frozen_snapshot(REPO_ROOT, digest)
    return SelectorWeightContext(
        payload["weights"], {"mode": "frozen", "snapshot_sha256": digest}
    )


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    try:
        text = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SelectorWeightPolicyError("snapshot cannot be serialized as canonical JSON") from exc
    return text.encode("utf-8")


def _file_sha256(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SelectorWeightPolicyError(f"cannot hash provenance input: {path.name}") from exc
    return hashlib.sha256(raw).hexdigest()


def _factor_health_input_hashes(repo_root: Path) -> dict[str, str]:
    paths = [repo_root / "data" / "factor_gate_config.json"]
    ic_root = repo_root / "data" / "ic"
    if ic_root.is_dir():
        paths.extend(sorted(ic_root.glob("ic_curve_*_k20.csv")))
    hashes: dict[str, str] = {}
    for path in paths:
        digest = _file_sha256(path)
        if digest is not None:
            hashes[path.relative_to(repo_root).as_posix()] = digest
    return hashes


def _atomic_create_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.stem}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != raw:
                raise SelectorWeightPolicyError("refusing to replace an existing snapshot")
        except OSError as exc:
            raise SelectorWeightPolicyError("atomic create-only snapshot publish failed") from exc
    except SelectorWeightPolicyError:
        raise
    except OSError as exc:
        raise SelectorWeightPolicyError("could not prepare selector-weight snapshot") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def create_frozen_snapshot(
    repo_root: str, *, captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Capture the current adaptive effective vector into an immutable snapshot."""
    if selector_weight_mode() != "adaptive":
        raise SelectorWeightPolicyError("snapshot creation requires explicit adaptive mode")
    root = Path(repo_root).resolve()
    weights = _validated_weights(_load_adaptive_weights())
    captured = captured_at or datetime.now(ZoneInfo("Asia/Shanghai"))
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise SelectorWeightPolicyError("captured_at must include a timezone")

    weight_path = root / "data" / "weights.json"
    provenance = {
        "source_mode": "adaptive-effective-selector",
        "weights_json_sha256": _file_sha256(weight_path),
        "factor_health_enabled": os.environ.get("FACTOR_HEALTH_ENABLED", "1") != "0",
        "factor_health_input_sha256": _factor_health_input_hashes(root),
    }
    payload = _validate_snapshot({
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "policy": SNAPSHOT_POLICY,
        "captured_at": captured.isoformat(),
        "weights": weights,
        "provenance": provenance,
    })
    raw = _canonical_json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    path = root / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    _atomic_create_bytes(path, raw)
    return {"snapshot_sha256": digest, "path": str(path), "weights": weights}
