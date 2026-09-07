# -*- coding: utf-8 -*-
"""快速验证: FactorValueEnv 环境能正常初始化和 roll-out."""
from __future__ import annotations

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

import numpy as np
from drl_train import FactorValueEnv, _compute_regime_features


def test_factor_value_env_basic():
    T = 60
    n_factors = 4
    fv = np.random.randn(T, n_factors).astype(np.float32) * 0.5
    fr = np.random.randn(T, n_factors).astype(np.float32) * 0.01
    rets = np.random.randn(T) * 0.02
    rf = _compute_regime_features(rets)

    env = FactorValueEnv(fv, fr, rets, lookback=5, regime_features=rf)
    obs, _ = env.reset()
    expected_dim = 5 * 4 + 4 + 1 + 3  # 28
    assert len(obs) == expected_dim, f"obs dim {len(obs)} != {expected_dim}"
    print(f"1. obs 维度正确: {len(obs)}")


def test_factor_value_env_rollout():
    T = 60
    n_factors = 4
    fv = np.random.randn(T, n_factors).astype(np.float32) * 0.5
    fr = np.random.randn(T, n_factors).astype(np.float32) * 0.01
    rets = np.random.randn(T) * 0.02
    rf = _compute_regime_features(rets)

    env = FactorValueEnv(fv, fr, rets, lookback=5, regime_features=rf)
    env.reset()
    rewards = []
    for _ in range(40):
        a = np.random.randn(4).astype(np.float32) * 0.3
        obs, reward, done, _, info = env.step(a)
        rewards.append(reward)
        if done:
            break
    assert len(rewards) > 0, "rollout 应至少产生 1 步"
    assert np.isfinite(np.mean(rewards)), f"奖励应有限值, 均值={np.mean(rewards)}"
    ws = info["weights"]
    assert len(ws) == 4, f"权重应为 4 维, 实际 {len(ws)}"
    assert abs(sum(ws) - 1.0) < 1e-5, f"权重应归一化, 和={sum(ws)}"
    print(f"2. rollout 成功: {len(rewards)} 步, 权重={[f'{w:.3f}' for w in ws]}")


def test_factor_value_env_no_regime():
    """不传 regime_features 时默认填充 0.5."""
    T = 60
    n_factors = 4
    fv = np.random.randn(T, n_factors).astype(np.float32) * 0.5
    fr = np.random.randn(T, n_factors).astype(np.float32) * 0.01
    rets = np.random.randn(T) * 0.02

    env = FactorValueEnv(fv, fr, rets, lookback=5, regime_features=None)
    obs, _ = env.reset()
    # 最后 3 维应为 0.5
    np.testing.assert_array_equal(obs[-3:], [0.5, 0.5, 0.5])
    print("3. 无 regime_features 默认填充 0.5: OK")


def test_factor_value_env_with_brief():
    """传入 brief 情绪因子后观测向量对应位置正确."""
    T = 60
    n_factors = 4
    fv = np.random.randn(T, n_factors).astype(np.float32) * 0.5
    fr = np.random.randn(T, n_factors).astype(np.float32) * 0.01
    rets = np.random.randn(T) * 0.02
    rf = _compute_regime_features(rets)

    brief = {
        "sentiment_factors": {"risk_on_off": 0.8, "rotation_intensity": -0.3,
                               "liquidity_stress": 0.1, "policy_catalyst": 0.5},
        "stance": "加仓",
    }
    env = FactorValueEnv(fv, fr, rets, lookback=5, regime_features=rf, brief=brief)
    obs, _ = env.reset()
    # 情绪因子在观测中的位置: lookback*n_factors=20 之后, 最后 4 维之前
    sent_start = 5 * 4  # 20
    sent_end = sent_start + 4  # 24
    np.testing.assert_array_almost_equal(
        obs[sent_start:sent_end], [0.8, -0.3, 0.1, 0.5], decimal=5
    )
    # stance = 1.4 (加仓)
    assert abs(obs[24] - 1.4) < 1e-5, f"stance 应为 1.4, 实际 {obs[24]}"
    print("4. 情绪因子 + stance 映射正确: OK")


if __name__ == "__main__":
    test_factor_value_env_basic()
    test_factor_value_env_rollout()
    test_factor_value_env_no_regime()
    # test 4 需要 unittest 的 assertAlmostEqual, 用内置方式
    T = 60
    n_factors = 4
    fv = np.random.randn(T, n_factors).astype(np.float32) * 0.5
    fr = np.random.randn(T, n_factors).astype(np.float32) * 0.01
    rets = np.random.randn(T) * 0.02
    rf = _compute_regime_features(rets)
    brief = {
        "sentiment_factors": {"risk_on_off": 0.8, "rotation_intensity": -0.3,
                               "liquidity_stress": 0.1, "policy_catalyst": 0.5},
        "stance": "加仓",
    }
    env = FactorValueEnv(fv, fr, rets, lookback=5, regime_features=rf, brief=brief)
    obs, _ = env.reset()
    sent_start = 5 * 4
    sent_end = sent_start + 4
    assert abs(obs[sent_start] - 0.8) < 1e-5
    assert abs(obs[sent_start + 1] - (-0.3)) < 1e-5
    assert abs(obs[sent_start + 2] - 0.1) < 1e-5
    assert abs(obs[sent_start + 3] - 0.5) < 1e-5
    assert abs(obs[24] - 1.4) < 1e-5
    print("4. 情绪因子 + stance 映射正确: OK")

    print("\n所有 FactorValueEnv 测试通过!")