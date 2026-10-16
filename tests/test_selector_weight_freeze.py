"""Frozen live-selector integration and point-in-time isolation tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import selector_weight_policy as policy


FACTOR_KEYS = policy.FACTOR_KEYS
FROZEN_WEIGHTS = {
    "signal": 0.30, "trend": 0.20, "govern": 0.15, "liquidity": 0.10,
    "vol": 0.10, "mom_rev": 0.0, "pb_rev": 0.05, "roe": 0.05,
    "mf_net": 0.05,
}


def _freeze_fixture(tmp_path: Path, monkeypatch, weights=None) -> str:
    payload = {
        "schema_version": 1,
        "policy": policy.SNAPSHOT_POLICY,
        "captured_at": "2026-10-16T09:00:00+08:00",
        "weights": weights or FROZEN_WEIGHTS,
        "provenance": {
            "source_mode": "adaptive-effective-selector",
            "weights_json_sha256": "a" * 64,
            "factor_health_enabled": True,
            "factor_health_input_sha256": {},
        },
    }
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "data" / "evolution" / "selector_weights" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", digest)
    return digest


def test_frozen_selector_ignores_dynamic_weights_and_factor_health(tmp_path, monkeypatch):
    digest = _freeze_fixture(tmp_path, monkeypatch)
    import factor_library
    import weight_optimizer

    dynamic = {key: 1.0 / len(FACTOR_KEYS) for key in FACTOR_KEYS}
    monkeypatch.setattr(weight_optimizer, "load_weights", lambda: dynamic.copy())
    monkeypatch.setattr(
        factor_library, "_apply_factor_health",
        lambda weights, as_of=None: weights.update({"signal": 0.0}),
    )

    first = factor_library.selector_weights()
    dynamic.update({"signal": 1.0, "trend": 0.0})
    (tmp_path / "data" / "weights.json").write_text(
        '{"weights":{"signal":1,"trend":0}}', encoding="utf-8"
    )
    second = factor_library.selector_weights()

    assert first == second == FROZEN_WEIGHTS
    assert policy.resolve_selector_weight_context().identity == {
        "mode": "frozen", "snapshot_sha256": digest,
    }


def test_invalid_frozen_policy_never_returns_static_fallback(monkeypatch):
    import factor_library

    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", "not-a-digest")
    monkeypatch.setattr(
        factor_library, "_apply_factor_health",
        lambda *args, **kwargs: pytest.fail("invalid frozen policy used adaptive fallback"),
    )

    with pytest.raises(policy.SelectorWeightPolicyError):
        factor_library.selector_weights()


def test_historical_as_of_keeps_point_in_time_weights(tmp_path, monkeypatch):
    _freeze_fixture(tmp_path, monkeypatch, {key: (1.0 if key == "signal" else 0.0) for key in FACTOR_KEYS})
    import factor_library
    import weight_optimizer

    dynamic = {key: 1.0 / len(FACTOR_KEYS) for key in FACTOR_KEYS}
    monkeypatch.setattr(weight_optimizer, "load_weights", lambda: dynamic.copy())
    seen = []

    def health_overlay(weights, as_of=None):
        seen.append(as_of)
        weights["vol"] = 0.0

    monkeypatch.setattr(factor_library, "_apply_factor_health", health_overlay)

    historical = factor_library.selector_weights(as_of="2024-06-28")

    assert seen == ["2024-06-28"]
    assert historical["vol"] == 0.0
    assert historical["signal"] < 0.2


def test_live_selection_records_resolved_weight_identity(monkeypatch):
    import selector
    import strategy_contract
    import target_weighting

    expected_identity = {"mode": "frozen", "snapshot_sha256": "b" * 64}
    context = policy.SelectorWeightContext(FROZEN_WEIGHTS.copy(), expected_identity)
    calls = []
    monkeypatch.setattr(
        selector, "resolve_selector_weight_context",
        lambda: calls.append(context) or context, raising=False,
    )
    monkeypatch.setattr(
        selector, "selector_weights",
        lambda *args, **kwargs: pytest.fail("selection did not use its resolved context"),
    )
    monkeypatch.setattr(selector, "filter_universe", lambda frame: frame.copy())
    monkeypatch.setattr(selector, "last_signal", lambda _bars: 0.2)
    monkeypatch.setattr(selector, "trend_score", lambda _bars: 0.1)
    monkeypatch.setattr(selector, "score_factor", lambda *_args: 0.5)
    monkeypatch.setattr(selector, "governance_score", lambda *_args: 0.5)
    monkeypatch.setattr(selector, "liquidity_score", lambda *_args: 0.5)
    monkeypatch.setattr(selector, "_pb_rev_score", lambda *_args: 0.5)
    monkeypatch.setattr(selector, "_roe_score", lambda *_args: 0.5)
    monkeypatch.setattr(selector, "_mf_net_score", lambda *_args: 0.5)
    monkeypatch.setattr(selector.RotationSelector, "_blend_fml", lambda *_args: None)
    monkeypatch.setattr(selector.RotationSelector, "_apply_fusion_rank", lambda *_args: None)
    monkeypatch.setattr(target_weighting, "allocate_target_weights", lambda items: None)
    monkeypatch.setattr(strategy_contract, "normalize_target_contract", lambda items, **_kwargs: items)

    class FakeDB:
        def get_universe(self):
            return pd.DataFrame([{
                "canon": "000001.SZ", "name6": "test", "amount": 100.0,
                "price": 10.0, "float_mv": 1000.0, "pe_ttm": 10.0, "pb": 1.0,
            }])

        def prefetch_daily_bars(self, **_kwargs):
            return None

        def prefetch_latest_snapshots(self):
            return None

        def get_bars(self, _canon, **_kwargs):
            close = np.linspace(9.0, 10.0, 60)
            return pd.DataFrame({"close": close, "high": close, "low": close})

        def get_financials(self, _canon):
            return {"roe": 0.1}

        def get_valuation(self, _canon):
            return {"pb": 1.0}

        def get_money_flow(self, _canon):
            return None

    result = selector.RotationSelector(FakeDB(), n=1).select()

    assert len(calls) == 1
    assert result["selector_weight_identity"] == expected_identity
    assert len(result["top_n"]) == 1
