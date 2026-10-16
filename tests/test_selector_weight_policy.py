"""Strict policy and immutable selector-weight snapshot contract."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import selector_weight_policy as policy


FACTOR_KEYS = (
    "signal", "trend", "govern", "liquidity", "vol", "mom_rev",
    "pb_rev", "roe", "mf_net",
)


def _canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=True,
    ).encode("utf-8")


def _snapshot(tmp_path, weights=None):
    weights = weights or {
        "signal": 0.30, "trend": 0.20, "govern": 0.15, "liquidity": 0.10,
        "vol": 0.10, "mom_rev": 0.0, "pb_rev": 0.05, "roe": 0.05,
        "mf_net": 0.05,
    }
    payload = {
        "schema_version": 1,
        "policy": "selector-weight-freeze/v1",
        "captured_at": "2026-10-16T09:00:00+08:00",
        "weights": weights,
        "provenance": {
            "source_mode": "adaptive-effective-selector",
            "weights_json_sha256": "a" * 64,
            "factor_health_enabled": True,
            "factor_health_input_sha256": {},
        },
    }
    raw = _canonical_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    directory = tmp_path / "data" / "evolution" / "selector_weights"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{digest}.json").write_bytes(raw)
    return payload, digest


def test_frozen_snapshot_accepts_nine_factor_vector_with_zero(tmp_path, monkeypatch):
    payload, digest = _snapshot(tmp_path)
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)

    context = policy.resolve_selector_weight_context()

    assert tuple(sorted(context.weights)) == tuple(sorted(FACTOR_KEYS))
    assert context.weights["mom_rev"] == 0.0
    assert sum(context.weights.values()) == pytest.approx(1.0)
    assert context.identity == {"mode": "frozen", "snapshot_sha256": digest}
    assert payload["weights"] == context.weights


@pytest.mark.parametrize("bad_digest", [
    "../" + "a" * 60, "A" * 64, "g" * 64, "a" * 63, "a" * 65,
    "subdir/" + "a" * 57,
])
def test_frozen_snapshot_rejects_malformed_hash_before_path_lookup(
    tmp_path, monkeypatch, bad_digest,
):
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", bad_digest)

    with pytest.raises(policy.SelectorWeightPolicyError):
        policy.resolve_selector_weight_context()


@pytest.mark.parametrize("weights", [
    {"signal": 1.0},
    {**{key: 0.125 for key in FACTOR_KEYS}, "unexpected": 0.0},
    {**{key: 0.125 for key in FACTOR_KEYS}, "signal": -0.001},
    {**{key: 0.125 for key in FACTOR_KEYS}, "signal": float("nan")},
    {**{key: 0.125 for key in FACTOR_KEYS}, "signal": float("inf")},
    {key: 0.1 for key in FACTOR_KEYS},
])
def test_frozen_snapshot_rejects_malformed_weights_and_content_hash(
    tmp_path, monkeypatch, weights,
):
    _, digest = _snapshot(tmp_path, weights)
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)

    with pytest.raises(policy.SelectorWeightPolicyError):
        policy.resolve_selector_weight_context()


def test_frozen_snapshot_rejects_byte_hash_mismatch_and_missing_file(
    tmp_path, monkeypatch,
):
    _, digest = _snapshot(tmp_path)
    path = tmp_path / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    path.write_bytes(path.read_bytes() + b" ")
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)
    with pytest.raises(policy.SelectorWeightPolicyError):
        policy.resolve_selector_weight_context()

    path.unlink()
    with pytest.raises(policy.SelectorWeightPolicyError):
        policy.resolve_selector_weight_context()


def test_frozen_snapshot_rejects_invalid_schema(tmp_path, monkeypatch):
    payload, _ = _snapshot(tmp_path)
    payload["schema_version"] = 2
    raw = _canonical_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    path.write_bytes(raw)
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)

    with pytest.raises(policy.SelectorWeightPolicyError):
        policy.resolve_selector_weight_context()


def test_frozen_snapshot_rejects_noncanonical_bytes(tmp_path, monkeypatch):
    payload, _ = _snapshot(tmp_path)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    path.write_bytes(raw)
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)

    with pytest.raises(policy.SelectorWeightPolicyError, match="canonical"):
        policy.resolve_selector_weight_context()


@pytest.mark.parametrize("mode", [None, "", "FROZEN", "automatic", "adaptive "])
def test_missing_or_invalid_mode_fails_closed(monkeypatch, mode):
    monkeypatch.delenv("SELECTOR_WEIGHT_MODE", raising=False)
    if mode is not None:
        monkeypatch.setenv("SELECTOR_WEIGHT_MODE", mode)

    with pytest.raises(policy.SelectorWeightPolicyError):
        policy.selector_weight_mode()


def test_snapshot_creation_is_create_only_canonical_and_provenanced(
    tmp_path, monkeypatch,
):
    data = tmp_path / "data"
    (data / "ic").mkdir(parents=True)
    weights_path = data / "weights.json"
    weights_path.write_bytes(b'{"weights":{"signal":1}}\n')
    gate_path = data / "factor_gate_config.json"
    gate_path.write_bytes(b'{"factor_ic_drift":0.15}\n')
    ic_path = data / "ic" / "ic_curve_signal_k20.csv"
    ic_path.write_bytes(b"date,ic_h20\n2026-10-15,0.01\n")
    captured = datetime(2026, 10, 16, 1, 2, 3, tzinfo=timezone.utc)
    vector = {
        "signal": 0.30, "trend": 0.20, "govern": 0.15, "liquidity": 0.10,
        "vol": 0.10, "mom_rev": 0.0, "pb_rev": 0.05, "roe": 0.05,
        "mf_net": 0.05,
    }
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "adaptive")
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(policy, "_load_adaptive_weights", lambda as_of=None: vector.copy())

    result = policy.create_frozen_snapshot(str(tmp_path), captured_at=captured)
    digest = result["snapshot_sha256"]
    path = data / "evolution" / "selector_weights" / f"{digest}.json"
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))

    assert hashlib.sha256(raw).hexdigest() == digest
    assert raw == _canonical_bytes(payload)
    assert set(payload) == {"schema_version", "policy", "captured_at", "weights", "provenance"}
    assert payload["captured_at"].endswith("+00:00")
    assert payload["provenance"]["weights_json_sha256"] == hashlib.sha256(
        weights_path.read_bytes()
    ).hexdigest()
    assert payload["provenance"]["factor_health_enabled"] is True
    assert payload["provenance"]["factor_health_input_sha256"] == {
        "data/factor_gate_config.json": hashlib.sha256(gate_path.read_bytes()).hexdigest(),
        "data/ic/ic_curve_signal_k20.csv": hashlib.sha256(ic_path.read_bytes()).hexdigest()
    }

    second = policy.create_frozen_snapshot(str(tmp_path), captured_at=captured)
    assert second["snapshot_sha256"] == digest
    assert path.read_bytes() == raw


def test_failed_snapshot_write_leaves_no_authoritative_file(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "adaptive")
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        policy, "_load_adaptive_weights",
        lambda as_of=None: {
            "signal": 0.30, "trend": 0.20, "govern": 0.15, "liquidity": 0.10,
            "vol": 0.10, "mom_rev": 0.0, "pb_rev": 0.05, "roe": 0.05,
            "mf_net": 0.05,
        },
    )

    def fail_link(_source, _destination):
        raise OSError("simulated atomic publish failure")

    monkeypatch.setattr(policy.os, "link", fail_link)
    with pytest.raises(policy.SelectorWeightPolicyError):
        policy.create_frozen_snapshot(str(tmp_path))

    snapshot_dir = tmp_path / "data" / "evolution" / "selector_weights"
    assert not snapshot_dir.exists() or list(snapshot_dir.iterdir()) == []
