# -*- coding: utf-8 -*-
"""Observation-window identity contracts."""

from __future__ import annotations

import pytest

from observation_epoch import build_observation_epoch, verify_observation_epoch


def _epoch(**overrides):
    values = {
        "code_sha": "code-sha-1",
        "data_identity": {"data_sha": "data-sha-1", "source": "h5i"},
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
    assert first["identity"]["data_identity"]["data_sha"] == "data-sha-1"
    assert first["identity"]["config_identity"]["config_sha"] == "config-sha-1"
    assert first["identity"]["experiment_identity"]["experiment_hash"] == "experiment-sha-1"
    assert verify_observation_epoch(first) == first


@pytest.mark.parametrize(
    "field, value",
    [
        ("code_sha", "code-sha-2"),
        ("data_identity", {"data_sha": "data-sha-2", "source": "h5i"}),
        ("config_identity", {"config_sha": "config-sha-2", "version": "paper-v1"}),
        ("experiment_identity", {"experiment_hash": "experiment-sha-2", "name": "fixture"}),
    ],
)
def test_epoch_changes_when_any_bound_identity_changes(field, value):
    assert _epoch(**{field: value})["epoch_id"] != _epoch()["epoch_id"]


@pytest.mark.parametrize(
    "field",
    ["code_sha", "data_identity", "config_identity", "experiment_identity"],
)
def test_epoch_rejects_missing_identity(field):
    values = {
        "code_sha": "code-sha-1",
        "data_identity": {"data_sha": "data-sha-1"},
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
