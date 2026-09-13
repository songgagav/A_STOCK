# -*- coding: utf-8 -*-
"""RANK_BY_FUSION 排序开关 + PIT 选股缓存隔离的回归用例."""
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))

import pytest  # noqa: E402


def test_rank_key_prefers_fusion_when_present():
    from selector import _rank_key
    assert _rank_key({"score": 0.9}) == 0.9
    assert _rank_key({"score": 0.1, "fusion_rank_key": 0.8}) == 0.8


def test_rank_key_scale_is_uniform_when_set():
    """开启时必须所有条目都有 fusion_rank_key, 否则会混用量纲."""
    from selector import _rank_key
    items = [{"score": s, "fusion_rank_key": r} for s, r in ((0.9, 0.2), (0.1, 0.9))]
    keys = [_rank_key(x) for x in items]
    assert keys == [0.2, 0.9]


def test_apply_fusion_rank_noop_when_disabled(monkeypatch):
    from db import StockDB
    from selector import RotationSelector
    monkeypatch.delenv("RANK_BY_FUSION", raising=False)
    sel = RotationSelector(StockDB(), n=5)
    scored = [{"canon": "000001.SZ", "score": 0.5}]
    sel._apply_fusion_rank(scored, "2024-07-03")
    assert "fusion_rank_key" not in scored[0]


def test_apply_fusion_rank_noop_when_empty(monkeypatch):
    from db import StockDB
    from selector import RotationSelector
    monkeypatch.setenv("RANK_BY_FUSION", "1")
    sel = RotationSelector(StockDB(), n=5)
    sel._apply_fusion_rank([], "2024-07-03")   # 不应抛异常


def test_pit_cache_path_isolated_by_rank_mode(monkeypatch):
    from vnpy_backtest import _pit_cache_path
    monkeypatch.delenv("RANK_BY_FUSION", raising=False)
    monkeypatch.delenv("FUSION_RANK_ALPHA", raising=False)
    p0 = _pit_cache_path("2024-07-03", 20)
    monkeypatch.setenv("RANK_BY_FUSION", "1")
    p1 = _pit_cache_path("2024-07-03", 20)
    assert p0 != p1
    assert p0.endswith("2024-07-03_n20.json")
    assert "_rf1" in os.path.basename(p1)


def test_roe_yy_chg_direction_is_positive():
    """2026-09-13 修正: 中性化后 IC 全视界为正, 方向应为 +1 (回归守卫)."""
    from factor_fusion import DIRECTIONS
    assert DIRECTIONS["roe_yy_chg"] == 1, "roe_yy_chg 方向应为 +1 (见 ic_neutral_check.py)"
    for f in ("pb_inv", "ep", "ocf_ps"):
        assert DIRECTIONS[f] == 1


def test_factor_health_as_of_slices_curve():
    from factor_gate import load_factor_ic_from_curves
    assert isinstance(load_factor_ic_from_curves(as_of="2018-06-29"), dict)
    assert isinstance(load_factor_ic_from_curves(), dict)


def test_selector_weights_accepts_as_of():
    from factor_library import selector_weights
    w = selector_weights(as_of="2022-12-30")
    assert isinstance(w, dict) and "vol" in w
    assert selector_weights(as_of="2018-06-29")["vol"] >= 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
