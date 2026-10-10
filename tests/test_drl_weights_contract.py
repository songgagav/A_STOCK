"""DRL-only weight loading contract tests."""

from __future__ import annotations

import json

import pytest


def test_drl_base_weights_use_nested_payload(monkeypatch, tmp_path):
    import drl_train
    import weight_optimizer as wo

    payload = {"weights": {
        "signal": 2.0, "trend": 1.0, "govern": 1.0,
        "liquidity": 1.0, "vol": 1.0, "mom_rev": 1.0,
    }}
    path = tmp_path / "weights.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(wo, "WEIGHTS_FILE", str(path))

    values = drl_train._load_base_weights()

    assert values.tolist() == pytest.approx([2 / 7, 1 / 7, 1 / 7,
                                              1 / 7, 1 / 7, 1 / 7])
