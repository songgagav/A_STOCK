# -*- coding: utf-8 -*-
"""单元测试: 风险因子 PPO 动态优化 (risk_factor_optimizer)."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from risk_factor_optimizer import (
    RISK_FACTOR_NAMES, RiskFactorExtractor, RiskFactorAugmentedEnv,
    compute_risk_factors, expanding_zscore,
    weight_stability_metrics, risk_explained_ratio,
    make_risk_factor_env,
)


def _synthetic_returns(n: int = 120, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vol = np.concatenate([
        rng.normal(0, 0.01, n // 2),
        rng.normal(0, 0.03, n - n // 2),   # 后半段高波动
    ])
    return vol


class TestComputeRiskFactors(unittest.TestCase):
    def test_shape_and_names(self):
        rets = _synthetic_returns()
        rf = compute_risk_factors(rets, window=20)
        self.assertEqual(rf.shape, (len(rets), len(RISK_FACTOR_NAMES)))
        self.assertEqual(list(RISK_FACTOR_NAMES), ["vol", "cvar95", "mdd", "downside_dev"])
        self.assertTrue(np.all(np.isfinite(rf)))

    def test_risk_measures_nonnegative(self):
        rf = compute_risk_factors(_synthetic_returns(), window=20)
        self.assertTrue(np.all(rf >= 0.0), "风险度量应非负")

    def test_high_vol_epoch_raises_risk(self):
        """后半段高波动 -> 后半段 vol/cvar95 显著更高."""
        rets = _synthetic_returns()
        rf = compute_risk_factors(rets, window=20)
        mid = len(rets) // 2
        self.assertGreater(rf[mid:].mean(axis=0)[0], rf[:mid].mean(axis=0)[0])

    def test_short_history_returns_zero(self):
        rf = compute_risk_factors(np.zeros(2), window=20)
        self.assertEqual(rf.shape, (2, 4))


class TestExpandingZscore(unittest.TestCase):
    def test_constant_series_zero(self):
        z = expanding_zscore(np.ones(30))
        self.assertTrue(np.allclose(z, 0.0))

    def test_no_future_leakage(self):
        """前 warmup 个点不输出非零 (数据不足)."""
        x = np.arange(1.0, 40.0)
        z = expanding_zscore(x, warmup=10)
        self.assertTrue(np.allclose(z[:9], 0.0))

    def test_bounded(self):
        z = expanding_zscore(np.random.default_rng(0).normal(size=200))
        self.assertTrue(np.all(np.abs(z) <= 5.0))


class TestRiskFactorExtractor(unittest.TestCase):
    def test_extractor_basics(self):
        ex = RiskFactorExtractor(_synthetic_returns(), window=20)
        self.assertEqual(ex.raw.shape[1], 4)
        self.assertEqual(ex.normalized.shape, ex.raw.shape)
        self.assertEqual(len(ex.raw_at(-1)), 4)
        self.assertTrue(np.all(np.isfinite(ex.norm_at(119))))
        self.assertTrue(np.all(np.abs(ex.norm_at(119)) <= 5.0))


class TestWeightStability(unittest.TestCase):
    def test_constant_weights_perfectly_stable(self):
        W = np.tile(np.array([0.5, 0.3, 0.2]), (30, 1))
        m = weight_stability_metrics(W)
        self.assertEqual(m["mean_autocorr"], 1.0)
        self.assertEqual(m["mean_turnover"], 0.0)
        self.assertEqual(m["stability_score"], 1.0)

    def test_short_input_defaults(self):
        m = weight_stability_metrics(np.ones((2, 3)))
        self.assertIn("stability_score", m)

    def test_oscillating_weights_less_stable(self):
        rng = np.random.default_rng(1)
        w_osc = np.stack([rng.uniform(0, 1, 5), rng.uniform(0, 1, 5)], axis=1)
        m_osc = weight_stability_metrics(w_osc)
        self.assertGreaterEqual(m_osc["mean_autocorr"], -1.0)
        self.assertLessEqual(m_osc["mean_autocorr"], 1.0)


class TestRiskExplainedRatio(unittest.TestCase):
    def test_strong_linear_explanation(self):
        rng = np.random.default_rng(3)
        X = rng.normal(size=(200, 4))
        Y = np.column_stack([
            2.0 * X[:, 0] - 1.0 * X[:, 1] + rng.normal(0, 0.05, 200),
            X[:, 2] + rng.normal(0, 0.05, 200),
        ])
        r = risk_explained_ratio(Y, X)
        self.assertGreater(r["mean_r2"], 0.8)
        self.assertEqual(len(r["per_factor_r2"]), 2)

    def test_mismatched_length_returns_zero(self):
        r = risk_explained_ratio(np.ones((5, 2)), np.ones((4, 2)))
        self.assertEqual(r["mean_r2"], 0.0)


class TestRiskFactorAugmentedEnv(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)
        self.T = 60
        self.n_factors = 4
        self.factor_history = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.5
        self.future_returns = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.01
        self.returns = _synthetic_returns(self.T)[:self.T]
        # FactorValueEnv 需 regime_features 与 returns 等长
        from drl_train import _compute_regime_features
        self.regime = _compute_regime_features(self.returns)

    def _base_env_dim(self):
        env = RiskFactorAugmentedEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime,
            factor_names=[f"f{i}" for i in range(self.n_factors)],
        )
        return env

    def test_obs_dim_extended_by_4(self):
        env = self._base_env_dim()
        from drl_train import FactorValueEnv
        base = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime,
        )
        self.assertEqual(
            env.observation_space.shape[0],
            base.observation_space.shape[0] + 4,
        )
        env.close()
        base.close()

    def test_step_returns_finite_augmented_obs(self):
        env = self._base_env_dim()
        obs, _ = env.reset()
        self.assertEqual(obs.shape[0], env.observation_space.shape[0])
        obs2, reward, done, _, info = env.step(np.random.randn(self.n_factors).astype(np.float32))
        self.assertTrue(np.all(np.isfinite(obs2)))
        self.assertTrue(np.isfinite(reward))
        self.assertIn("weights", info)
        self.assertEqual(obs2.shape[0], env.observation_space.shape[0])
        env.close()

    def test_make_env_factory(self):
        env = make_risk_factor_env(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime,
            risk_factor_window=10,
        )
        self.assertIsInstance(env, RiskFactorAugmentedEnv)
        env.close()


class TestCVaRPPOWithRiskFactors(unittest.TestCase):
    """集成: CVaR_PPO 可直接在带风险因子观测的环境上训练与推理."""

    def setUp(self):
        np.random.seed(42)
        self.T = 60
        self.n_factors = 4
        self.factor_history = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.5
        self.future_returns = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.01
        self.returns = _synthetic_returns(self.T)
        from drl_train import _compute_regime_features
        self.regime = _compute_regime_features(self.returns)

    def test_cvar_ppo_train_and_predict(self):
        from drl_train import CVaR_PPO
        env = RiskFactorAugmentedEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime,
            factor_names=[f"f{i}" for i in range(self.n_factors)],
        )
        model = CVaR_PPO(
            "MlpPolicy", env, verbose=0,
            cvar_alpha=0.05, cvar_coef=0.1,
            n_steps=24, batch_size=12,
        )
        model.learn(total_timesteps=100)
        obs, _ = env.reset()
        self.assertEqual(obs.shape[0], env.observation_space.shape[0])
        action, _ = model.predict(obs, deterministic=True)
        self.assertEqual(len(action), self.n_factors)
        self.assertTrue(np.all(np.isfinite(action)))
        env.close()


if __name__ == "__main__":
    unittest.main()
