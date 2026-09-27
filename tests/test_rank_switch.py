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
        assert tagged.endswith("_t0.05.json")


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


def test_ic_neutral_artifact_labels_directions_as_a_snapshot():
    """`data/ic_neutral_check.json` 必须把 `directions` 标成**快照**, 并带运行时刻。

    ## 为什么锁这个 (2026-09-27 实测)

    该产物的 `directions` 只是**运行时对 `factor_fusion.DIRECTIONS` 的快照**,
    而 `ic_by_factor` 的数值是按**当时的方向**算的。生产方向一改, 这个文件就
    **自身前后不一致** —— 实测踩到: 文件里 `directions.roe_yy_chg = -1`,
    而 `factor_fusion.py` 已是 `+1`(2026-09-13 修正)。

    **危害**: 读者会把它当权威口径而读错; 且文件此前**没有任何时间字段**,
    "这是哪天的口径"无法从文件本身回答 —— 只能靠文件 mtime, 而 mtime 在
    复制/归档后就丢了。

    故锁三件事: `directions_at_run` 存在、`run_at` 存在、`directions_note` 存在
    且点明"快照"。产物不存在时跳过(不是所有环境都有 data/)。
    """
    import json
    import os

    fp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "ic_neutral_check.json")
    if not os.path.exists(fp):
        pytest.skip("无 data/ic_neutral_check.json(该环境未跑过该脚本)")
    d = json.load(open(fp, encoding="utf-8"))
    assert "directions_at_run" in d, (
        "缺 `directions_at_run` —— 无法区分「运行当时的口径」与「现行口径」")
    assert "run_at" in d, (
        "缺 `run_at` —— 产物新鲜度无法判断(该文件此前没有任何时间字段)")
    assert "directions_note" in d, (
        "缺 `directions_note` —— `directions` 会被读成权威口径")
    assert "快照" in str(d["directions_note"]), "note 必须点明它是**快照**"
    assert isinstance(d["directions_at_run"], dict) and d["directions_at_run"]
    # 注: 快照与现行口径**允许不同**(历史快照本来就该是旧的) —— 不锁相等,
    # 只锁"读者能看出它是旧的"。


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
