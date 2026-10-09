"""Deterministic identity for one continuous observation window."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = 1
_REQUIRED_IDENTITIES = (
    ("data_identity", "data_sha"),
    ("config_identity", "config_sha"),
    ("experiment_identity", "experiment_hash"),
)
_SHADOW_STATE = {
    "RANK_BY_FUSION": {"0"},
    "DRL_PLAN_MODE": {"shadow"},
    "FUSION_WEIGHT_MODE": {"shadow"},
    "TRADE_BROKER": {"paper"},
    "alpha_evidence_status": {"not_promotable"},
    "drl_plan_mode_contract": {"not_implemented", "implemented_default_shadow"},
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
    data_identity: Mapping[str, Any],
    config_identity: Mapping[str, Any],
    experiment_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the immutable identity shared by all evidence in one window."""
    if not str(code_sha or ""):
        raise ValueError("code_sha is required")
    identities = {
        name: _copy_identity(name, value, required_key)
        for name, value, required_key in (
            ("data_identity", data_identity, "data_sha"),
            ("config_identity", config_identity, "config_sha"),
            ("experiment_identity", experiment_identity, "experiment_hash"),
        )
    }
    identity = {
        "schema_version": SCHEMA_VERSION,
        "code_sha": str(code_sha),
        **identities,
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
    expected = build_observation_epoch(
        code_sha=identity.get("code_sha"),
        data_identity=identity.get("data_identity"),
        config_identity=identity.get("config_identity"),
        experiment_identity=identity.get("experiment_identity"),
    )
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
