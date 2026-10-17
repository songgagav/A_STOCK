# -*- coding: utf-8 -*-
"""Observation-window identity contracts."""

from __future__ import annotations

import pytest
import hashlib
import json

from observation_epoch import (
    build_observation_epoch,
    build_observation_experiment_identity,
    freeze_observation_epoch,
    observation_epoch_runtime_receipt,
    validate_observation_epoch_for_observation,
    verify_observation_epoch,
)
import selector_weight_policy as selector_policy


def _lineage():
    return {
        "source": "h5i", "schema": "daily-bars-v1", "routing": "h5i-primary",
        "universe": "a-share-v1", "calendar_source": "official-fixture",
        "calendar_version": "2026-v1", "lineage_sha": "lineage-sha-1",
    }


def _state():
    return {
        "RANK_BY_FUSION": "0", "DRL_PLAN_MODE": "shadow",
        "FUSION_WEIGHT_MODE": "shadow", "TRADE_BROKER": "paper",
        "alpha_evidence_status": "not_promotable",
        "drl_plan_mode_contract": "implemented_default_shadow",
    }


def _epoch(**overrides):
    values = {
        "code_sha": "code-sha-1",
        "data_lineage_identity": _lineage(),
        "production_state": _state(),
        "config_identity": {"config_sha": "config-sha-1", "version": "paper-v1"},
        "experiment_identity": {"experiment_hash": "experiment-sha-1", "name": "fixture"},
    }
    values.update(overrides)
    return build_observation_epoch(**values)


def test_epoch_is_deterministic_and_contains_all_identity_inputs():
    first = _epoch()
    second = _epoch()

    assert first == second
    assert len(first["epoch_id"]) == 64
    assert first["identity"]["code_sha"] == "code-sha-1"
    assert first["schema_version"] == 2
    assert first["identity"]["data_lineage_identity"]["lineage_sha"] == "lineage-sha-1"
    assert first["identity"]["production_state"] == _state()
    assert first["identity"]["config_identity"]["config_sha"] == "config-sha-1"
    assert first["identity"]["experiment_identity"]["experiment_hash"] == "experiment-sha-1"
    assert verify_observation_epoch(first) == first


@pytest.mark.parametrize(
    "field, value",
    [
        ("code_sha", "code-sha-2"),
        ("data_lineage_identity", {**_lineage(), "lineage_sha": "lineage-sha-2"}),
        ("config_identity", {"config_sha": "config-sha-2", "version": "paper-v1"}),
        ("experiment_identity", {"experiment_hash": "experiment-sha-2", "name": "fixture"}),
    ],
)
def test_epoch_changes_when_any_bound_identity_changes(field, value):
    assert _epoch(**{field: value})["epoch_id"] != _epoch()["epoch_id"]


@pytest.mark.parametrize(
    "field",
    ["code_sha", "data_lineage_identity", "config_identity", "experiment_identity", "production_state"],
)
def test_epoch_rejects_missing_identity(field):
    values = {
        "code_sha": "code-sha-1",
        "data_lineage_identity": _lineage(),
        "production_state": _state(),
        "config_identity": {"config_sha": "config-sha-1"},
        "experiment_identity": {"experiment_hash": "experiment-sha-1"},
    }
    values[field] = "" if field == "code_sha" else {}

    with pytest.raises(ValueError):
        build_observation_epoch(**values)


def test_epoch_verification_rejects_rewritten_id():
    epoch = _epoch()
    epoch["identity"]["code_sha"] = "rewritten"

    with pytest.raises(ValueError):
        verify_observation_epoch(epoch)


@pytest.mark.parametrize("key,value", [
    ("RANK_BY_FUSION", "1"), ("DRL_PLAN_MODE", "enforce"),
    ("FUSION_WEIGHT_MODE", "off"), ("TRADE_BROKER", "live"),
    ("alpha_evidence_status", "promotable"),
])
def test_epoch_binds_each_production_state_dimension(key, value):
    assert _epoch(production_state={**_state(), key: value})["epoch_id"] != _epoch()["epoch_id"]


def test_new_epoch_rejects_unimplemented_contract():
    with pytest.raises(ValueError, match="implemented_default_shadow"):
        _epoch(production_state={**_state(), "drl_plan_mode_contract": "not_implemented"})


@pytest.mark.parametrize("key", list(_lineage()))
def test_epoch_requires_complete_stable_lineage(key):
    lineage = _lineage()
    del lineage[key]
    with pytest.raises(ValueError, match=key):
        _epoch(data_lineage_identity=lineage)


def test_daily_artifact_hash_cannot_be_smuggled_into_stable_lineage():
    with pytest.raises(ValueError, match="unexpected"):
        _epoch(data_lineage_identity={**_lineage(), "market_data_sha": "daily-hash"})


def test_legacy_v1_epoch_still_verifies_without_new_production_contract():
    identity = {
        "schema_version": 1, "code_sha": "legacy-code",
        "data_identity": {"data_sha": "old-daily-sha"},
        "config_identity": {"config_sha": "old-config"},
        "experiment_identity": {"experiment_hash": "old-experiment"},
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    legacy = {"schema_version": 1, "epoch_id": digest, "identity": identity}
    assert verify_observation_epoch(legacy) == legacy
    legacy["identity"]["code_sha"] = "changed"
    with pytest.raises(ValueError):
        verify_observation_epoch(legacy)


def _frozen_experiment(digest="a" * 64):
    return build_observation_experiment_identity({
        "name": "selector-weight-observation",
        "selector_weight_mode": "frozen",
        "selector_weights_snapshot_sha256": digest,
    })


def _write_frozen_weight_snapshot(root, monkeypatch):
    payload = {
        "schema_version": selector_policy.SNAPSHOT_SCHEMA_VERSION,
        "policy": selector_policy.SNAPSHOT_POLICY,
        "captured_at": "2026-10-16T09:00:00+08:00",
        "weights": {key: 1.0 / len(selector_policy.FACTOR_KEYS)
                    for key in selector_policy.FACTOR_KEYS},
        "provenance": {
            "source_mode": "adaptive-effective-selector",
            "weights_json_sha256": None,
            "factor_health_enabled": True,
            "factor_health_input_sha256": {},
        },
    }
    raw = selector_policy._canonical_json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    path = root / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    monkeypatch.setattr(selector_policy, "REPO_ROOT", root)
    return digest


def test_weight_mode_or_snapshot_digest_changes_epoch_id():
    frozen = _frozen_experiment()
    changed_digest = _frozen_experiment("b" * 64)
    adaptive = {
        "name": "selector-weight-observation",
        "selector_weight_mode": "adaptive",
        "selector_weights_snapshot_sha256": "a" * 64,
        "experiment_hash": "adaptive-experiment",
    }

    base_id = _epoch(experiment_identity=frozen)["epoch_id"]

    assert _epoch(experiment_identity=changed_digest)["epoch_id"] != base_id
    assert _epoch(experiment_identity=adaptive)["epoch_id"] != base_id


@pytest.mark.parametrize("identity", [
    {"experiment_hash": "legacy"},
    {"experiment_hash": "bad", "selector_weight_mode": "adaptive",
     "selector_weights_snapshot_sha256": "a" * 64},
    {"experiment_hash": "bad", "selector_weight_mode": "frozen",
     "selector_weights_snapshot_sha256": "not-a-digest"},
    {"experiment_hash": "bad", "selector_weight_mode": "frozen",
     "selector_weights_snapshot_sha256": "a" * 64},
])
def test_observation_freeze_requires_valid_frozen_identity(tmp_path, identity):
    epoch = _epoch(experiment_identity=identity)

    with pytest.raises(ValueError):
        freeze_observation_epoch(tmp_path, epoch)


def test_epoch_record_is_create_only_without_breaking_legacy_verification(tmp_path, monkeypatch):
    digest = _write_frozen_weight_snapshot(tmp_path, monkeypatch)
    epoch = _epoch(experiment_identity=_frozen_experiment(digest))
    verified = validate_observation_epoch_for_observation(epoch)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv(
        "SELECTOR_WEIGHT_SNAPSHOT_SHA256",
        digest,
    )

    path = freeze_observation_epoch(tmp_path, verified)
    first_bytes = path.read_bytes()
    assert path == tmp_path / "data" / "evolution" / "epochs" / f"{epoch['epoch_id']}.json"
    assert freeze_observation_epoch(tmp_path, epoch) == path
    assert path.read_bytes() == first_bytes
    mismatch = observation_epoch_runtime_receipt(
        tmp_path, epoch["epoch_id"],
        {"mode": "frozen", "snapshot_sha256": "b" * 64},
    )
    assert mismatch["status"] == "blocked"

    path.write_bytes(b"pre-existing conflicting record")
    with pytest.raises(ValueError, match="refusing to replace"):
        freeze_observation_epoch(tmp_path, epoch)
    assert path.read_bytes() == b"pre-existing conflicting record"

    legacy = {
        "schema_version": 1, "epoch_id": "legacy-id",
        "identity": {
            "schema_version": 1, "code_sha": "legacy-code",
            "data_identity": {"data_sha": "old-daily-sha"},
            "config_identity": {"config_sha": "old-config"},
            "experiment_identity": {"experiment_hash": "old-experiment"},
        },
    }
    legacy["epoch_id"] = hashlib.sha256(json.dumps(
        legacy["identity"], sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    assert verify_observation_epoch(legacy) == legacy


def test_freeze_requires_frozen_runtime_mode_and_matching_snapshot_sha(tmp_path, monkeypatch):
    digest = _write_frozen_weight_snapshot(tmp_path, monkeypatch)
    epoch = _epoch(experiment_identity=_frozen_experiment(digest))
    monkeypatch.delenv("SELECTOR_WEIGHT_MODE", raising=False)
    monkeypatch.delenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", raising=False)

    with pytest.raises(ValueError, match="SELECTOR_WEIGHT_MODE=frozen"):
        freeze_observation_epoch(tmp_path, epoch)

    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    with pytest.raises(ValueError, match="active selector-weight snapshot SHA"):
        freeze_observation_epoch(tmp_path, epoch)

    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", "b" * 64)
    with pytest.raises(ValueError, match="active selector-weight snapshot SHA"):
        freeze_observation_epoch(tmp_path, epoch)

    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)
    path = freeze_observation_epoch(tmp_path, epoch)
    assert path.is_file()


def test_freeze_requires_resolvable_active_weight_snapshot(tmp_path, monkeypatch):
    digest = "c" * 64
    epoch = _epoch(experiment_identity=_frozen_experiment(digest))
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)
    monkeypatch.setattr(selector_policy, "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match="active selector-weight snapshot is invalid"):
        freeze_observation_epoch(tmp_path, epoch)

    snapshot_path = tmp_path / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text("not a snapshot", encoding="utf-8")
    with pytest.raises(ValueError, match="active selector-weight snapshot is invalid"):
        freeze_observation_epoch(tmp_path, epoch)


def test_freeze_requires_epoch_record_root_to_match_snapshot_checkout(tmp_path, monkeypatch):
    resolver_root = tmp_path / "resolver"
    record_root = tmp_path / "record"
    digest = _write_frozen_weight_snapshot(resolver_root, monkeypatch)
    epoch = _epoch(experiment_identity=_frozen_experiment(digest))
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)

    with pytest.raises(ValueError, match="record_root must match the resolver checkout"):
        freeze_observation_epoch(record_root, epoch)

    assert not (record_root / "data" / "evolution" / "epochs" / f"{epoch['epoch_id']}.json").exists()
