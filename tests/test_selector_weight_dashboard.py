"""Selector weight dashboard reports the live authority separately from proposals."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import dashboard  # noqa: E402
import selector_weight_policy as policy  # noqa: E402


def _effective_weights():
    return {
        "signal": 0.5, "trend": 0.5, "govern": 0.0,
        "liquidity": 0.0, "vol": 0.0, "mom_rev": 0.0,
        "pb_rev": 0.0, "roe": 0.0, "mf_net": 0.0,
    }


def _write_frozen_snapshot(root: Path, weights=None):
    payload = {
        "schema_version": 1,
        "policy": policy.SNAPSHOT_POLICY,
        "captured_at": "2026-10-16T09:00:00+08:00",
        "weights": weights or _effective_weights(),
        "provenance": {
            "source_mode": "adaptive-effective-selector",
            "weights_json_sha256": None,
            "factor_health_enabled": True,
            "factor_health_input_sha256": {},
        },
    }
    raw = policy._canonical_json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    path = root / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return digest


def _write_proposal(root: Path):
    proposal = {
        "schema_version": 1,
        "policy": "selector-weight-proposal/v1",
        "status": "proposal_only",
        "trading_day": "2026-10-15",
        "optimizer_result": {"weights": {"signal": 1.0}, "meta": {"note": "not effective"}},
    }
    raw = policy._canonical_json_bytes(proposal)
    digest = hashlib.sha256(raw).hexdigest()
    path = root / "data" / "weight_proposals" / proposal["trading_day"] / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return proposal, path


def test_dashboard_reports_frozen_effective_weights_and_proposal_separately(
    monkeypatch, tmp_path,
):
    digest = _write_frozen_snapshot(tmp_path)
    proposal, proposal_path = _write_proposal(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "weights.json").write_text(
        json.dumps({"weights": {"signal": 99.0}, "meta": {"note": "stale"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)

    result = dashboard.read_weights()

    assert result["ok"] is True
    assert result["mode"] == "frozen"
    assert result["snapshot_sha256"] == digest
    assert result["effective_source"] == "frozen_snapshot"
    assert result["weights"] == _effective_weights()
    assert result["weights"]["signal"] != 99.0
    assert result["proposal"]["status"] == "proposal_only"
    assert result["proposal"]["sha256"] == proposal_path.stem
    assert result["proposal"]["result"] == proposal["optimizer_result"]


def test_dashboard_returns_not_ok_for_invalid_frozen_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", "a" * 64)

    result = dashboard.read_weights()

    assert result["ok"] is False
    assert result["weights"] == {}
    assert result["mode"] == "frozen"
    assert "error" in result


def test_selector_weight_mode_must_be_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SELECTOR_WEIGHT_MODE", raising=False)

    result = dashboard.read_weights()

    assert result["ok"] is False
    assert result["weights"] == {}
    assert "SELECTOR_WEIGHT_MODE" in result["error"]


def test_adaptive_dashboard_keeps_effective_weights_and_legacy_metadata(
    monkeypatch, tmp_path,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "weights.json").write_text(json.dumps({
        "weights": {"signal": 0.1}, "meta": {"source": "legacy"}, "updated": "today",
    }), encoding="utf-8")
    monkeypatch.setattr(dashboard, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(policy, "_load_adaptive_weights", lambda as_of=None: _effective_weights())
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "adaptive")

    result = dashboard.read_weights()

    assert result["ok"] is True
    assert result["weights"] == _effective_weights()
    assert result["meta"] == {"source": "legacy"}
    assert result["updated"] == "today"
    assert result["mode"] == "adaptive"
