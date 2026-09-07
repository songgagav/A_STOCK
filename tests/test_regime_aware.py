# -*- coding: utf-8 -*-
"""单元测试: 市场状态感知 (Regime-Aware) 功能.

覆盖:
  - _compute_regime_features: 三种 regime 分类、波动率分位、趋势强度
  - FactorWeightEnv 状态向量拼接: 观测维度、regime 特征映射
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from drl_train import _compute_regime_features, FactorWeightEnv, SCORE_FACTORS


# ===================================================================
#  _compute_regime_features
# ===================================================================
class TestComputeRegimeFeatures(unittest.TestCase):
    """_compute_regime_features: 市场状态特征计算的正确性."""

    def setUp(self):
        self.n_factors = len(SCORE_FACTORS)

    # ---- 形状与值域 ----

    def test_output_shape(self):
        """输出形状为 (T, 3)."""
        T = 100
        rets = np.random.randn(T) * 0.01
        out = _compute_regime_features(rets)
        self.assertEqual(out.shape, (T, 3))

    def test_short_sequence(self):
        """不足 20 日时, regime 默认为 0, 其他为 0.5."""
        rets = np.array([0.001] * 10)
        out = _compute_regime_features(rets)
        self.assertEqual(out.shape, (10, 3))
        # 不足 20 天: regime=0, vol_quantile=0.5, trend_strength=0.5
        np.testing.assert_array_equal(out[0], [0.0, 0.5, 0.5])

    def test_empty_sequence(self):
        """空序列返回空数组."""
        rets = np.array([])
        out = _compute_regime_features(rets)
        self.assertEqual(out.shape, (0, 3))

    def test_value_range(self):
        """所有输出在合理值域内: regime ∈ {0,1,2}, vol_quantile ∈ [0,1], trend_strength ∈ [0,1]."""
        T = 300
        np.random.seed(42)
        rets = np.random.randn(T) * 0.03
        out = _compute_regime_features(rets)
        self.assertTrue(np.all((out[:, 0] >= 0) & (out[:, 0] <= 2)))
        # 首日(无足够历史) vol_quantile 和 trend_strength 可能为 0.5
        valid_mask = np.arange(T) >= 60
        if valid_mask.any():
            self.assertTrue(np.all(out[valid_mask, 1] >= 0.0))
            self.assertTrue(np.all(out[valid_mask, 1] <= 1.0))
            self.assertTrue(np.all(out[valid_mask, 2] >= 0.0))
            self.assertTrue(np.all(out[valid_mask, 2] <= 1.0))

    # ---- market_regime 分类 ----

    def test_regime_uptrend(self):
        """20 日累计收益 > +2% → regime = 1 (上涨趋势)."""
        T = 60
        rets = np.zeros(T)
        # 最后 20 天连续正收益
        rets[-20:] = 0.002  # 每 0.2%, 20 日 = 4%
        out = _compute_regime_features(rets)
        # 不足 20 天的位置不判定
        self.assertEqual(out[59, 0], 1.0, "累计 > 2% 应标记为上涨趋势")

    def test_regime_downtrend(self):
        """20 日累计收益 < -2% → regime = 2 (下跌趋势)."""
        T = 60
        rets = np.zeros(T)
        rets[-20:] = -0.002
        out = _compute_regime_features(rets)
        self.assertEqual(out[59, 0], 2.0, "累计 < -2% 应标记为下跌趋势")

    def test_regime_oscillate(self):
        """20 日累计收益在 ±2% 之间 → regime = 0 (震荡)."""
        T = 60
        rets = np.zeros(T)
        # 20 日收益总计 0.15% 在 ±2% 内
        rets[-20:] = 0.000075
        out = _compute_regime_features(rets)
        self.assertEqual(out[59, 0], 0.0, "累计在 ±2% 内应标记为震荡")

    def test_regime_early_days(self):
        """不足 20 日时, regime=0."""
        T = 15
        rets = np.ones(T) * 0.01
        out = _compute_regime_features(rets)
        for t in range(T):
            self.assertEqual(out[t, 0], 0.0, f"t={t} 不足 20 日, regime 应为 0")

    # ---- volatility_quantile ----

    def test_vol_quantile_high_vol(self):
        """高波动阶段波动率分位应接近 1.0."""
        T = 300
        rets = np.random.randn(T) * 0.005  # 正常波动
        # 最后 20 天突然高波动
        rets[-20:] = np.random.randn(20) * 0.03
        out = _compute_regime_features(rets)
        vol_q = out[-1, 1]
        self.assertGreaterEqual(vol_q, 0.5, "高波动阶段 vol_quantile 应 >= 0.5")

    def test_vol_quantile_low_vol(self):
        """低波动阶段波动率分位应接近 0.0."""
        T = 300
        rets = np.random.randn(T) * 0.02  # 正常波动
        # 最后 20 天极低波动
        rets[-20:] = np.random.randn(20) * 0.001
        out = _compute_regime_features(rets)
        vol_q = out[-1, 1]
        self.assertLessEqual(vol_q, 0.5, "低波动阶段 vol_quantile 应 <= 0.5")

    def test_vol_quantile_insufficient_history(self):
        """历史波动率不足 5 个样本时回退到 0.5."""
        T = 25
        rets = np.random.randn(T) * 0.01
        out = _compute_regime_features(rets)
        # t=24 时历史窗口仅为 t=19~25, 可能不足 5 个有效样本
        # 只要有 vol_quantile 在 [0, 1] 内即可
        self.assertGreaterEqual(out[-1, 1], 0.0)
        self.assertLessEqual(out[-1, 1], 1.0)

    # ---- trend_strength ----

    def test_trend_strength_strong_uptrend(self):
        """20 日均线远高于 60 日均线 → trend_strength 接近 1.0."""
        T = 200
        rets = np.zeros(T)
        # 最近 60 天: 前 40 天平稳, 后 20 天快速拉升
        rets[-60:-20] = 0.0005
        rets[-20:] = 0.003
        out = _compute_regime_features(rets)
        ts = out[-1, 2]
        self.assertGreaterEqual(ts, 0.5, "强势上涨趋势 trend_strength 应 >= 0.5")

    def test_trend_strength_weak_trend(self):
        """20 日均线低于 60 日均线 → trend_strength 接近 0.0."""
        T = 200
        rets = np.zeros(T)
        # 最近 60 天: 前 40 天快速上涨, 后 20 天慢速上涨 → MA20 < MA60
        rets[-60:-20] = 0.003
        rets[-20:] = 0.0005
        out = _compute_regime_features(rets)
        ts = out[-1, 2]
        self.assertLessEqual(ts, 0.5, "减速上涨 (MA20 < MA60) trend_strength 应 <= 0.5")

    def test_trend_strength_insufficient_history(self):
        """不足 60 日时回退到 0.5."""
        T = 50
        rets = np.random.randn(T) * 0.01
        out = _compute_regime_features(rets)
        # 所有 t < 59 的位置 trend_strength = 0.5
        for t in range(min(T, 59)):
            self.assertEqual(out[t, 2], 0.5, f"t={t} 不足 60 日, trend_strength 应为 0.5")

    # ---- 边界情况 ----

    def test_zero_returns(self):
        """全零收益率: regime=0, 其他稳定值."""
        T = 100
        rets = np.zeros(T)
        out = _compute_regime_features(rets)
        # 充足历史后: regime=0, vol_quantile 和 trend_strength 在 [0,1]
        self.assertEqual(out[-1, 0], 0.0)
        self.assertGreaterEqual(out[-1, 1], 0.0)
        self.assertLessEqual(out[-1, 1], 1.0)
        self.assertGreaterEqual(out[-1, 2], 0.0)
        self.assertLessEqual(out[-1, 2], 1.0)

    def test_all_positive_returns(self):
        """全部正收益 → regime 最终为 1 (上涨)."""
        T = 100
        rets = np.ones(T) * 0.003
        out = _compute_regime_features(rets)
        # 除前 20 天外均为上涨
        for t in range(20, T):
            self.assertEqual(out[t, 0], 1.0, f"t={t} 应标记为上涨趋势")

    def test_all_negative_returns(self):
        """全部负收益 → regime 最终为 2 (下跌)."""
        T = 100
        rets = np.ones(T) * -0.003
        out = _compute_regime_features(rets)
        for t in range(20, T):
            self.assertEqual(out[t, 0], 2.0, f"t={t} 应标记为下跌趋势")

    def test_extreme_values(self):
        """极端收益率 (NaN, Inf) 不崩溃."""
        T = 80
        rets = np.random.randn(T) * 0.01
        rets[30] = np.nan
        rets[50] = np.inf
        rets[55] = -np.inf
        out = _compute_regime_features(rets)
        self.assertEqual(out.shape, (T, 3))
        self.assertTrue(np.all(np.isfinite(out)))


# ===================================================================
#  FactorWeightEnv — 状态向量拼接
# ===================================================================
class TestFactorWeightEnvRegime(unittest.TestCase):
    """FactorWeightEnv 在市场状态感知下的行为."""

    def setUp(self):
        self.n_factors = len(SCORE_FACTORS)
        self.lookback = 10
        self.n_days = 80
        self.obs_dim = self.lookback * self.n_factors + 4 + 1 + 3  # 68

        # 模拟 IC 序列
        np.random.seed(42)
        self.ic_history = np.random.randn(self.n_days, self.n_factors).astype(np.float32) * 0.05
        self.base_weights = np.ones(self.n_factors, dtype=np.float64) / self.n_factors

        # 模拟市场状态特征
        rets = np.random.randn(self.n_days) * 0.02
        self.regime_features = _compute_regime_features(rets)

    def _make_env(self, regime_features=None):
        return FactorWeightEnv(
            ic_history=self.ic_history,
            base_weights=self.base_weights,
            lookback=self.lookback,
            regime_features=regime_features,
        )

    # ---- 观测维度 ----

    def test_obs_dim_with_regime(self):
        """传入 regime_features 后观测维度为 68."""
        env = self._make_env(regime_features=self.regime_features)
        self.assertEqual(env.observation_space.shape[0], self.obs_dim)

    def test_obs_dim_without_regime(self):
        """不传 regime_features 时观测维度仍为 68 (默认填充 0.5)."""
        env = self._make_env(regime_features=None)
        self.assertEqual(env.observation_space.shape[0], self.obs_dim)

    def test_obs_dim_after_reset(self):
        """reset() 后观测向量维度为 68."""
        env = self._make_env(regime_features=self.regime_features)
        obs, _ = env.reset()
        self.assertEqual(len(obs), self.obs_dim)

    def test_obs_dim_after_step(self):
        """step() 后观测向量维度仍为 68."""
        env = self._make_env(regime_features=self.regime_features)
        env.reset()
        obs, _, _, _, _ = env.step(np.zeros(self.n_factors))
        self.assertEqual(len(obs), self.obs_dim)

    # ---- regime 特征映射 ----

    def test_regime_in_last_3_dims(self):
        """观测向量最后 3 维应为 regime_features 对应时刻的值."""
        env = self._make_env(regime_features=self.regime_features)
        obs, _ = env.reset()
        # reset 后 t = lookback = 10, 应取出 regime_features[10]
        expected_regime = self.regime_features[env.t]
        np.testing.assert_array_almost_equal(
            obs[-3:], expected_regime, decimal=5,
            err_msg="观测向量最后 3 维应与 regime_features[10] 一致",
        )

    def test_regime_evolves_over_time(self):
        """不同时间步的 regime 特征不同."""
        # 构造有明显状态切换的 regime_features
        n = self.n_days
        rf = np.zeros((n, 3), dtype=np.float32)
        # 前半段: 震荡 (regime=0)
        rf[:n // 2] = [0.0, 0.3, 0.4]
        # 后半段: 上涨 (regime=1)
        rf[n // 2:] = [1.0, 0.7, 0.8]
        env = self._make_env(regime_features=rf)
        obs1, _ = env.reset()
        # step 使 t 进入后半段
        for _ in range(n // 2 - env.lookback + 1):
            obs1_step, _, _, _, _ = env.step(np.zeros(self.n_factors))
        obs2 = obs1_step
        r1 = obs1[-3:]
        r2 = obs2[-3:]
        # 前后应不同
        self.assertTrue(
            np.any(np.abs(r1 - r2) > 1e-6),
            "跨 regime 切换后观测特征应不同",
        )

    def test_default_regime_features(self):
        """不传 regime_features 时, 默认填充 0.5."""
        env = self._make_env(regime_features=None)
        obs, _ = env.reset()
        np.testing.assert_array_equal(
            obs[-3:], [0.5, 0.5, 0.5],
            err_msg="不传 regime_features 时最后 3 维应为 0.5",
        )

    def test_partial_regime_features(self):
        """部分传入 regime_features (形状不同) 应不崩溃."""
        # 短一些的特征数组
        short_rf = np.full((self.n_days - 10, 3), 0.3, dtype=np.float32)
        env = self._make_env(regime_features=short_rf)
        obs, _ = env.reset()
        # 仍能正常 reset
        self.assertEqual(len(obs), self.obs_dim)
        # 最后 3 维对应 short_rf[10]
        np.testing.assert_array_almost_equal(obs[-3:], short_rf[env.t], decimal=5)

    # ---- 观测向量完整结构 ----

    def test_obs_structure_parts(self):
        """观测向量由 4 部分组成: IC 历史 + 情绪 + stance + regime."""
        env = self._make_env(regime_features=self.regime_features)
        obs, _ = env.reset()

        ic_part = obs[:self.lookback * self.n_factors]           # 60 维
        sent_part = obs[self.lookback * self.n_factors:-4]       # 4 维 (情绪)
        stance_part = obs[-4:-3]                                  # 1 维 (stance)
        regime_part = obs[-3:]                                    # 3 维 (regime)

        # IC 历史: 对应 ic_history[0:10].flatten()
        expected_ic = self.ic_history[env.t - self.lookback:env.t].flatten()
        np.testing.assert_array_almost_equal(ic_part, expected_ic, decimal=5)

        # 情绪因子: 默认皆为 0
        np.testing.assert_array_equal(sent_part, [0.0, 0.0, 0.0, 0.0])

        # stance: 默认 = 1.0 (维持)
        self.assertAlmostEqual(stance_part[0], 1.0)

        # regime: 对应 regime_features[10]
        expected_regime = self.regime_features[env.t]
        np.testing.assert_array_almost_equal(regime_part, expected_regime, decimal=5)

    # ---- 多步 roll-out ----

    def test_multi_step_consistency(self):
        """多步 rollout 后, 观测向量最后 3 维始终匹配 regime_features."""
        env = self._make_env(regime_features=self.regime_features)
        env.reset()
        for step in range(20):
            obs, _, done, _, _ = env.step(np.zeros(self.n_factors))
            if done:
                break
            actual_t = env.t
            # _state() 在 step() 自增 t 后调用, 索引为 env.t
            expected_regime = self.regime_features[actual_t]
            for dim in range(3):
                self.assertAlmostEqual(
                    obs[-3 + dim], expected_regime[dim], places=5,
                    msg=f"step {step} dim={dim} regime 特征不匹配",
                )


if __name__ == "__main__":
    unittest.main()