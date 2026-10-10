# -*- coding: utf-8 -*-
"""Observation-window identity contracts."""

from __future__ import annotations

import pytest
import hashlib
import json

from observation_epoch import build_observation_epoch, verify_observation_epoch


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
