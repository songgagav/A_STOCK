# -*- coding: utf-8 -*-
"""item 7 回归: 滚动窗口 Sharpe 稳健离散度 (MAD 替代 CV).

背景: 原 CV = std/|mean| 在均值近 0 时爆掉, 使检查项退化为"恒 FAIL"。
"""
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from overfitting_test import sharpe_dispersion_stat  # noqa: E402


def test_positive_mean_uses_robust_cv():
    sharpes = [1.8, 2.0, 2.2, 2.4, 2.1, 1.9, 2.3, 2.05]
    kind, stat, ok, detail, thr = sharpe_dispersion_stat(sharpes)
    assert kind == "robust_cv"
    assert ok is True
    assert thr == "< 2.0"
    assert "稳健CV" in detail


def test_small_mean_does_not_explode_and_falls_back_to_sign_consistency():
    """均值/中位近 0 时: 不得返回巨大 CV, 应退化为符号一致性."""
    # 正负成对抵消 -> mean == 0, 旧口径分母被 0.01 兜底, CV 直接爆炸
    sharpes = [-0.6, 0.6, -0.5, 0.5, -0.4, 0.4, -0.3, 0.3, -0.2, 0.2, -0.1, 0.1]
    kind, stat, ok, detail, thr = sharpe_dispersion_stat(sharpes)
    assert kind == "sign_consistency"
    assert thr == "≥ 60%"
    assert 0.0 <= stat <= 1.0
    # 旧实现会给出爆炸值: std/|mean| 在此样本上极大(且随样本放大无上界)
    old = np.std(sharpes) / max(abs(np.mean(sharpes)), 0.01)
    assert old > 10.0, "构造样本应能暴露旧 CV 的爆炸, 否则用例失去意义"
    assert "近 0" in detail


def test_sign_consistency_pass_and_fail():
    mostly_pos = [0.1, 0.2, -0.05, 0.3, 0.4, 0.05, 0.25, 0.15, -0.02, 0.35]
    kind, stat, ok, _, _ = sharpe_dispersion_stat(mostly_pos)
    assert kind == "sign_consistency" and ok is True
    half = [0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.4, -0.4, 0.05, -0.05]
    kind2, _, ok2, _, _ = sharpe_dispersion_stat(half)
    assert kind2 == "sign_consistency" and ok2 is False


def test_robust_to_single_outlier():
    base = [2.0, 2.1, 1.9, 2.05, 1.95, 2.02, 2.08, 1.98]
    spiked = base + [12.0]
    _, s0, ok0, _, _ = sharpe_dispersion_stat(base)
    _, s1, ok1, _, _ = sharpe_dispersion_stat(spiked)
    assert ok0 and ok1, "单个离群窗口不应把稳健统计量推到 FAIL"
    assert s1 < 2.0
    # 对比: 标准差口径会被离群点显著抬高
    assert np.std(spiked) > 5 * np.std(base)


def test_empty_and_nan_handling():
    kind, stat, ok, _, _ = sharpe_dispersion_stat([])
    assert kind == "empty" and ok is False
    kind2, _, ok2, _, _ = sharpe_dispersion_stat([float("nan"), float("inf")])
    assert kind2 == "empty" and ok2 is False


def test_threshold_floor_boundary():
    """|median| 恰好跨过 floor 时应切换判定类型."""
    lo = [0.24, 0.20, 0.28, 0.22, 0.26, 0.24]
    hi = [0.30, 0.40, 0.20, 0.30, 0.35, 0.30]
    assert sharpe_dispersion_stat(lo, floor=0.25)[0] == "sign_consistency"
    assert sharpe_dispersion_stat(hi, floor=0.25)[0] == "robust_cv"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
