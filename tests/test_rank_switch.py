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


class TestFusionTrim:
    """FUSION_TRIM_Q: 极端头部截尾 (2026-09-13, 见 docs/pit-valuation.md 第 16 条)."""

    def test_trim_top_masks_highest_fraction(self):
        import numpy as np
        from selector import trim_top
        v = np.arange(100, dtype=float)          # 0..99
        out, mask = trim_top(v, 0.05)
        assert mask.sum() == 5, "应恰好截掉最高的 5%"
        assert np.isnan(out[mask]).all()
        assert np.isfinite(out[~mask]).all()

    def test_trim_top_disabled_when_q_zero(self):
        import numpy as np
        from selector import trim_top
        v = np.arange(100, dtype=float)
        out, mask = trim_top(v, 0.0)
        assert not mask.any() and np.isfinite(out).all()

    def test_trim_top_safe_on_small_sample(self):
        import numpy as np
        from selector import trim_top
        v = np.arange(10, dtype=float)
        out, mask = trim_top(v, 0.05)
        assert not mask.any(), "样本过少不应截尾(否则会把整个池子打空)"

    def test_fusion_trim_q_env_and_clamp(self, monkeypatch):
        from selector import fusion_trim_q
        monkeypatch.delenv("FUSION_TRIM_Q", raising=False)
        assert fusion_trim_q() == 0.0            # 默认关闭
        monkeypatch.setenv("FUSION_TRIM_Q", "0.05")
        assert fusion_trim_q() == 0.05
        monkeypatch.setenv("FUSION_TRIM_Q", "abc")
        assert fusion_trim_q() == 0.0            # 非法值回落关闭
        monkeypatch.setenv("FUSION_TRIM_Q", "0.9")
        assert fusion_trim_q() == 0.5            # 上限保护

    def test_pit_cache_path_tagged_by_trim(self, monkeypatch):
        """截尾改变 score -> PIT 缓存必须与不截尾隔离, 否则会命中旧产物."""
        import vnpy_backtest as V
        monkeypatch.delenv("RANK_BY_FUSION", raising=False)
        monkeypatch.delenv("FUSION_TRIM_Q", raising=False)
        base = V._pit_cache_path("2024-07-03", 10)
        monkeypatch.setenv("FUSION_TRIM_Q", "0.05")
        tagged = V._pit_cache_path("2024-07-03", 10)
        assert base != tagged
        assert "_t0.05" in tagged

    def test_pit_cache_path_carries_data_version(self):
        """补丁更新会改变历史 pe_ttm -> 缓存必须带数据版本, 否则静默命中旧产物."""
        import vnpy_backtest as V
        p = V._pit_cache_path("2024-07-03", 10)
        ver = V._data_version()
        assert ver and len(ver) >= 2
        assert p.endswith(f"_d{ver}.json")
        # 版本标签稳定(同一进程内多次调用一致)
        assert V._pit_cache_path("2024-07-03", 10) == p


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
