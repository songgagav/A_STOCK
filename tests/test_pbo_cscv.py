# -*- coding: utf-8 -*-
"""CSCV-PBO 回归测试 (2026-09-13).

守护本轮修复: 原 test_pbo() 的判据在数学上 pbo >= 0.5 恒成立
(置换不改变 max, 判据退化为"噪声是否把其他配置抬过最大值"),
即 `pbo < 50%` 永不可能通过。本测试用合成数据验证新实现
(a) 在"纯噪声"下给出高 PBO, (b) 在"真有优势"下给出低 PBO,
从而保证该检查确实具备判别力、而非常数函数。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from pbo_cscv import _avg_rank, cscv_pbo, sharpe  # noqa: E402


# --------------------------------------------------------------------------
# 基础函数
# --------------------------------------------------------------------------
def test_avg_rank_matches_known_values():
    r = _avg_rank(np.array([3.0, 1.0, 2.0]))
    assert list(r) == [3.0, 1.0, 2.0]
    # 并列取平均名次: 值 [10, 20, 20, 30] -> 名次 [1, 2.5, 2.5, 4]
    r2 = _avg_rank(np.array([10.0, 20.0, 20.0, 30.0]))
    assert list(r2) == [1.0, 2.5, 2.5, 4.0]


def test_sharpe_edge_cases():
    assert np.isnan(sharpe(np.array([0.01, 0.01])))            # 样本不足
    assert np.isnan(sharpe(np.zeros(50)))                       # 零波动
    v = sharpe(np.linspace(0.001, 0.002, 100))
    assert np.isfinite(v) and v > 0


# --------------------------------------------------------------------------
# 核心判别力
# --------------------------------------------------------------------------
def _noise_blocks(rng, s=12, n=18, days=120):
    """纯噪声: 每个配置在每个块上都是同分布随机收益(无真实优势)."""
    return [rng.normal(0, 0.02, size=(days, n)) for _ in range(s)]


def _skilled_blocks(rng, s=12, n=18, days=120):
    """配置 0 在**所有块**上都有真实正漂移, 其余为噪声 -> IS 最优必然泛化."""
    blocks = [rng.normal(0, 0.02, size=(days, n)) for _ in range(s)]
    for b in blocks:
        b[:, 0] += 0.004
    return blocks


def test_pbo_high_on_pure_noise():
    """纯噪声下 IS 最优不应稳定泛化 -> PBO 应接近 0.5.

    单次 CSCV 估计因组合高度重叠而方差较大(实测 std≈0.19), 故对多个种子取均值,
    均值才是"该估计量在此设定下是否正确"的可靠指标。
    """
    vals = [cscv_pbo(_noise_blocks(np.random.RandomState(100 + r)))["pbo"]
            for r in range(8)]
    mean = float(np.mean(vals))
    assert 0.35 <= mean <= 0.68, f"纯噪声下 PBO 均值={mean:.3f}, 偏离 0.5 过多"
    for r in range(8):
        res = cscv_pbo(_noise_blocks(np.random.RandomState(100 + r)))
        assert res["n_blocks"] == 12 and res["n_configs"] == 18


def test_pbo_low_when_true_edge_exists():
    """存在真实优势配置时 IS 最优应能泛化 -> PBO 应很低."""
    vals = [cscv_pbo(_skilled_blocks(np.random.RandomState(200 + r)))["pbo"]
            for r in range(4)]
    mean = float(np.mean(vals))
    assert mean < 0.05, f"有真实优势时 PBO 均值={mean:.3f} 过高"


def test_pbo_is_not_a_constant_function():
    """关键回归: PBO 必须随数据变化.

    旧实现 math 上 pbo >= 0.5 恒成立(置换不改变 max, 判据退化为"噪声是否把其他
    配置抬过最大值"), 是一个常数函数, 因此 `pbo < 50%` 永不可能通过。这里要求
    新实现既能给出 >0.5 也能给出 <0.5, 且两者差异显著。
    """
    noise_vals = [cscv_pbo(_noise_blocks(np.random.RandomState(300 + r)))["pbo"]
                  for r in range(6)]
    skill_vals = [cscv_pbo(_skilled_blocks(np.random.RandomState(400 + r)))["pbo"]
                  for r in range(6)]
    assert np.mean(noise_vals) - np.mean(skill_vals) > 0.30, "无判别力"
    assert min(skill_vals) < 0.5, "无法给出 <0.5 的 PBO(旧实现缺陷的特征)"
    assert np.std(noise_vals) > 0.0, "PBO 不随数据变化, 疑似又退化为常数函数"


def test_pbo_always_in_unit_interval():
    rng = np.random.RandomState(5)
    for _ in range(3):
        assert 0.0 <= cscv_pbo(_noise_blocks(rng))["pbo"] <= 1.0


def test_lambda_logit_consistency():
    """λ = logit(rank/(N+1)) 必须在合法范围, 且 lambda_p05 < p50 < p95."""
    rng = np.random.RandomState(13)
    res = cscv_pbo(_noise_blocks(rng))
    n = res["n_configs"]
    lo = np.log((1 / (n + 1)) / (1 - 1 / (n + 1)))
    hi = np.log((n / (n + 1)) / (1 - n / (n + 1)))
    assert lo - 1e-9 <= res["lambda_p05"] <= res["lambda_p95"] <= hi + 1e-9
    assert res["lambda_p05"] <= res["lambda_p50"] <= res["lambda_p95"]


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------
def test_requires_even_block_count():
    rng = np.random.RandomState(1)
    with pytest.raises(ValueError):
        cscv_pbo([rng.normal(size=(30, 4)) for _ in range(5)])   # 5 块(奇数)
    with pytest.raises(ValueError):
        cscv_pbo([rng.normal(size=(30, 4)) for _ in range(2)])   # 2 块(<4)


def test_requires_two_configs():
    rng = np.random.RandomState(1)
    with pytest.raises(ValueError):
        cscv_pbo([rng.normal(size=(30, 1)) for _ in range(4)])


def test_ragged_blocks_are_allowed():
    """各块天数可不同(CSCV 只要求块数与配置数一致)."""
    rng = np.random.RandomState(2)
    blocks = [rng.normal(size=(30 + i * 5, 6)) for i in range(4)]
    res = cscv_pbo(blocks)
    assert res["n_combos"] == 6          # C(4,2)
    assert res["n_configs"] == 6
