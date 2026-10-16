"""Deterministic identity for one continuous observation window."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LINEAGE_KEYS = {
    "source", "schema", "routing", "universe", "calendar_source",
    "calendar_version", "lineage_sha",
}
_SHADOW_STATE = {
    "RANK_BY_FUSION": {"0"},
    "DRL_PLAN_MODE": {"shadow"},
    "FUSION_WEIGHT_MODE": {"shadow"},
    "TRADE_BROKER": {"paper"},
    "alpha_evidence_status": {"not_promotable"},
    "drl_plan_mode_contract": {"implemented_default_shadow"},
}
_MANIFEST_IDENTITY_COPIES = {
    "schema_version", "trade_day", "generated_at", "run_id", "code_sha",
    "data_identity", "data_lineage_identity", "config_identity",
    "snapshot_hash", "experiment_identity", "observation_epoch",
    "builder_version", "production_state", "reference_equity",
    "reference_timestamp", "cost_evidence_level",
    "calendar_identity", "trade_days",
}


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"observation identity is not canonical JSON: {exc}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _copy_identity(name: str, value: Mapping[str, Any], required_key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} is required")
    copied = dict(value)
    if not str(copied.get(required_key) or ""):
        raise ValueError(f"{name}.{required_key} is required")
    # Force a finite/canonical JSON check before the identity becomes an epoch.
    _canonical_bytes(copied)
    return copied


def build_observation_experiment_identity(
    experiment_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal a frozen observation experiment payload with its canonical digest."""
    if not isinstance(experiment_payload, Mapping):
        raise ValueError("experiment payload is required")
    payload = dict(experiment_payload)
    supplied_hash = payload.pop("experiment_hash", None)
    if payload.get("selector_weight_mode") != "frozen":
        raise ValueError("observation experiment requires selector_weight_mode=frozen")
    snapshot_digest = payload.get("selector_weights_snapshot_sha256")
    if not isinstance(snapshot_digest, str) or not _SHA256_RE.fullmatch(snapshot_digest):
        raise ValueError("selector_weights_snapshot_sha256 must be lowercase SHA-256")
    digest = _digest(payload)
    if supplied_hash is not None and supplied_hash != digest:
        raise ValueError("experiment_hash does not match the canonical experiment payload")
    return {**payload, "experiment_hash": digest}


def validate_observation_experiment_identity(
    experiment_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the observation-only frozen selector identity and self-hash."""
    copied = _copy_identity("experiment_identity", experiment_identity, "experiment_hash")
    if copied.get("selector_weight_mode") != "frozen":
        raise ValueError("observation requires experiment_identity.selector_weight_mode=frozen")
    snapshot_digest = copied.get("selector_weights_snapshot_sha256")
    if not isinstance(snapshot_digest, str) or not _SHA256_RE.fullmatch(snapshot_digest):
        raise ValueError(
            "experiment_identity.selector_weights_snapshot_sha256 must be lowercase SHA-256"
        )
    experiment_hash = copied["experiment_hash"]
    if not isinstance(experiment_hash, str) or not _SHA256_RE.fullmatch(experiment_hash):
        raise ValueError("experiment_identity.experiment_hash must be lowercase SHA-256")
    payload = {key: value for key, value in copied.items() if key != "experiment_hash"}
    if _digest(payload) != experiment_hash:
        raise ValueError("experiment_hash does not match the canonical experiment payload")
    return copied


def build_observation_epoch(
    *,
    code_sha: str,
    data_lineage_identity: Mapping[str, Any],
    config_identity: Mapping[str, Any],
    experiment_identity: Mapping[str, Any],
    production_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the immutable identity shared by all evidence in one window."""
    if not str(code_sha or ""):
        raise ValueError("code_sha is required")
    identities = {
        name: _copy_identity(name, value, required_key)
        for name, value, required_key in (
            ("data_lineage_identity", data_lineage_identity, "lineage_sha"),
            ("config_identity", config_identity, "config_sha"),
            ("experiment_identity", experiment_identity, "experiment_hash"),
        )
    }
    lineage = identities["data_lineage_identity"]
    for key in sorted(_LINEAGE_KEYS):
        if not lineage.get(key):
            raise ValueError(f"data_lineage_identity.{key} is required")
    unexpected = set(lineage) - _LINEAGE_KEYS
    if unexpected:
        raise ValueError(f"unexpected data_lineage_identity fields: {sorted(unexpected)!r}")
    if not isinstance(production_state, Mapping):
        raise ValueError("production_state is required")
    state = dict(production_state)
    for key in _SHADOW_STATE:
        if not str(state.get(key) or ""):
            raise ValueError(f"production_state.{key} is required")
    if state["drl_plan_mode_contract"] != "implemented_default_shadow":
        raise ValueError("new epochs require drl_plan_mode_contract=implemented_default_shadow")
    _canonical_bytes(state)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "code_sha": str(code_sha),
        **identities,
        "production_state": state,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "epoch_id": _digest(identity),
        "identity": identity,
    }


def verify_observation_epoch(epoch: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the epoch id against its embedded code/config/data identity."""
    if not isinstance(epoch, Mapping):
        raise ValueError("observation_epoch must be an object")
    identity = epoch.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError("observation_epoch.identity is required")
    if epoch.get("schema_version") == 1:
        # Historical construction semantics are preserved only for verification.
        # New writers cannot use this path to create an unimplemented epoch.
        if not str(identity.get("code_sha") or ""):
            raise ValueError("code_sha is required")
        legacy_identity = {"schema_version": 1, "code_sha": str(identity["code_sha"])}
        for name, required_key in (
            ("data_identity", "data_sha"), ("config_identity", "config_sha"),
            ("experiment_identity", "experiment_hash"),
        ):
            legacy_identity[name] = _copy_identity(name, identity.get(name), required_key)
        expected = {"schema_version": 1, "epoch_id": _digest(legacy_identity), "identity": legacy_identity}
    elif epoch.get("schema_version") == SCHEMA_VERSION:
        expected = build_observation_epoch(
            code_sha=identity.get("code_sha"),
            data_lineage_identity=identity.get("data_lineage_identity"),
            config_identity=identity.get("config_identity"),
            experiment_identity=identity.get("experiment_identity"),
            production_state=identity.get("production_state"),
        )
    else:
        raise ValueError("unsupported observation_epoch schema_version")
    if dict(epoch) != expected:
        raise ValueError("observation_epoch does not match its identity")
    return dict(epoch)


def validate_observation_epoch_for_observation(epoch: Mapping[str, Any]) -> dict[str, Any]:
    """Apply stricter frozen-weight requirements only at the observation boundary."""
    verified = verify_observation_epoch(epoch)
    if verified.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("new observations require a schema-v2 observation_epoch")
    verified = dict(verified)
    verified["identity"] = dict(verified["identity"])
    identity = verified["identity"]
    identity["experiment_identity"] = validate_observation_experiment_identity(
        identity["experiment_identity"]
    )
    return verified


def freeze_observation_epoch(record_root: str | Path, epoch: Mapping[str, Any]) -> Path:
    """Verify then atomically create an immutable epoch record without replacement."""
    verified = validate_observation_epoch_for_observation(epoch)
    experiment_identity = verified["identity"]["experiment_identity"]
    if os.environ.get("SELECTOR_WEIGHT_MODE") != "frozen":
        raise ValueError("freezing an observation epoch requires SELECTOR_WEIGHT_MODE=frozen")
    if os.environ.get("SELECTOR_WEIGHT_SNAPSHOT_SHA256") != experiment_identity[
        "selector_weights_snapshot_sha256"
    ]:
        raise ValueError("active selector-weight snapshot SHA differs from experiment identity")
    epoch_id = verified["epoch_id"]
    path = Path(record_root) / "data" / "evolution" / "epochs" / f"{epoch_id}.json"
    raw = (json.dumps(
        verified, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{epoch_id}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("refusing to replace an existing observation epoch record")
        except OSError as exc:
            raise ValueError("atomic create-only observation epoch publish failed") from exc
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("could not prepare observation epoch record") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return path


def load_observation_epoch_record(record_root: str | Path, epoch_id: str) -> dict[str, Any]:
    """Load one explicitly selected observation record and re-verify its identity."""
    if not isinstance(epoch_id, str) or not _SHA256_RE.fullmatch(epoch_id):
        raise ValueError("OBSERVATION_EPOCH_ID must be lowercase SHA-256")
    path = Path(record_root) / "data" / "evolution" / "epochs" / f"{epoch_id}.json"
    try:
        epoch = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("selected observation epoch record is unavailable or invalid JSON") from exc
    verified = validate_observation_epoch_for_observation(epoch)
    if verified["epoch_id"] != epoch_id:
        raise ValueError("selected observation epoch id does not match its path")
    return verified


def observation_epoch_runtime_receipt(
    record_root: str | Path,
    epoch_id: str | None = None,
    selector_weight_identity: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a non-authoritative runtime receipt for one explicitly bound epoch."""
    selected_id = epoch_id if epoch_id is not None else os.environ.get("OBSERVATION_EPOCH_ID")
    if not selected_id:
        return {"status": "not_configured", "epoch_id": None}
    try:
        epoch = load_observation_epoch_record(record_root, selected_id)
    except ValueError as exc:
        return {
            "status": "blocked", "epoch_id": str(selected_id),
            "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    if (
        not isinstance(selector_weight_identity, Mapping)
        or set(selector_weight_identity) != {"mode", "snapshot_sha256"}
        or selector_weight_identity.get("mode") != "frozen"
        or selector_weight_identity.get("snapshot_sha256")
        != epoch["identity"]["experiment_identity"]["selector_weights_snapshot_sha256"]
    ):
        return {
            "status": "blocked", "epoch_id": epoch["epoch_id"],
            "reason": "active selector-weight identity differs from the observation epoch",
        }
    return {
        "status": "ready",
        "epoch_id": epoch["epoch_id"],
        "experiment_identity": dict(epoch["identity"]["experiment_identity"]),
        "observation_epoch": epoch,
    }


def validate_shadow_production_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Require evidence producers to record the non-promoted runtime state."""
    if not isinstance(state, Mapping):
        raise ValueError("production_state is required")
    copied = dict(state)
    for key, allowed in _SHADOW_STATE.items():
        if str(copied.get(key)) not in allowed:
            raise ValueError(
                f"production_state.{key} must be explicitly recorded as one of {sorted(allowed)!r}"
            )
    return copied


def verify_epoch_manifest_binding(manifest: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    """Verify new manifest metadata against its bound epoch, retaining legacy reads."""
    epoch = manifest.get("observation_epoch")
    has_lineage = "data_lineage_identity" in identity
    if "observation_epoch" not in manifest and "observation_epoch" not in identity and not has_lineage:
        return  # Pre-epoch Phase A artifact.
    verified = verify_observation_epoch(epoch)
    if identity.get("observation_epoch") != verified:
        raise ValueError("manifest observation_epoch differs from artifact identity")
    experiment_identity = verified["identity"].get("experiment_identity", {})
    if (
        isinstance(experiment_identity, Mapping)
        and {"selector_weight_mode", "selector_weights_snapshot_sha256"}
        & set(experiment_identity)
    ):
        validate_observation_experiment_identity(experiment_identity)
    if has_lineage and verified["schema_version"] != SCHEMA_VERSION:
        raise ValueError("lineage-bearing manifests require a v2 observation_epoch")
    if verified["schema_version"] == SCHEMA_VERSION:
        for key in ("code_sha", "data_lineage_identity", "config_identity",
                    "experiment_identity", "production_state"):
            bound = verified["identity"][key]
            if manifest.get(key) != bound or identity.get(key) != bound:
                raise ValueError(f"observation_epoch bound {key} mismatch")
        for key in _MANIFEST_IDENTITY_COPIES:
            if key in identity and manifest.get(key) != identity[key]:
                raise ValueError(f"manifest_identity_mismatch:{key}")
        for outer, parent, inner in (
            ("data_sha", "data_identity", "data_sha"),
            ("config_sha", "config_identity", "config_sha"),
            ("experiment_hash", "experiment_identity", "experiment_hash"),
        ):
            if parent in identity or outer in manifest:
                nested = identity.get(parent)
                if not isinstance(nested, Mapping) or manifest.get(outer) != nested.get(inner):
                    raise ValueError(f"manifest_identity_mismatch:{outer}")
        if "source_artifact_hashes" in identity:
            raw_artifacts = manifest.get("raw_artifacts")
            source_hashes = identity["source_artifact_hashes"]
            if not isinstance(raw_artifacts, Mapping) or not isinstance(source_hashes, Mapping):
                raise ValueError("manifest_identity_mismatch:source_artifact_hashes")
            raw_hashes = {
                name: artifact.get("source_sha256")
                for name, artifact in raw_artifacts.items()
                if isinstance(artifact, Mapping)
            }
            expected_hashes = {
                name: artifact.get("expected_sha256")
                for name, artifact in raw_artifacts.items()
                if isinstance(artifact, Mapping)
            }
            if raw_hashes != dict(source_hashes) or expected_hashes != dict(source_hashes):
                raise ValueError("manifest_identity_mismatch:source_artifact_hashes")
            snapshot = raw_artifacts.get("snapshot")
            if (not isinstance(snapshot, Mapping)
                    or manifest.get("snapshot_path") != snapshot.get("source_path")):
                raise ValueError("manifest_identity_mismatch:snapshot_path")
