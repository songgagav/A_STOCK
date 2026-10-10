"""Deterministic identity for one continuous observation window."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = 2
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
    if epoch is None and not has_lineage:
        return  # Pre-epoch Phase A artifact.
    verified = verify_observation_epoch(epoch)
    if has_lineage and verified["schema_version"] != SCHEMA_VERSION:
        raise ValueError("lineage-bearing manifests require a v2 observation_epoch")
    if verified["schema_version"] == SCHEMA_VERSION:
        if identity.get("observation_epoch") != verified:
            raise ValueError("manifest observation_epoch differs from artifact identity")
        for key in ("code_sha", "data_lineage_identity", "config_identity",
                    "experiment_identity", "production_state"):
            bound = verified["identity"][key]
            if manifest.get(key) != bound or identity.get(key) != bound:
                raise ValueError(f"observation_epoch bound {key} mismatch")
