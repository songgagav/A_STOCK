# -*- coding: utf-8 -*-
"""集成测试: 风险因子模块 (risk_factor_optimizer) 与 CVaR_PPO / FactorValueEnv 兼容性.

验证端到端:
  1. RiskFactorAugmentedEnv 是 FactorValueEnv 子类, 构造参数完全向后兼容
  2. 增强观测的前缀 == 原 FactorValueEnv 观测 (奖励/权重/动作语义不变)
  3. step info 额外携带当前风险因子 (vol/cvar95/mdd/downside_dev)
  4. 与 Risk-First 约束层 / brief 情绪 / regime=None 等既有能力可叠加
  5. CVaR_PPO 可直接在增强环境上初始化/训练/推理, 权重仍归一化
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from drl_train import CVaR_PPO, FactorValueEnv, _compute_regime_features, SCORE_FACTORS
from risk_factor_optimizer import (
    RISK_FACTOR_NAMES, RiskFactorAugmentedEnv,
)


def _make_data(T: int = 80, n_factors: int = 6, seed: int = 7):
    rng = np.random.default_rng(seed)
    factor_history = rng.normal(0, 0.5, (T, n_factors)).astype(np.float32)
    future_returns = rng.normal(0, 0.01, (T, n_factors)).astype(np.float32)
    returns = rng.normal(0, 0.02, T).astype(np.float64)
    return factor_history, future_returns, returns


def _brief() -> dict:
    return {
        "sentiment_factors": {
            "risk_on_off": 0.3, "rotation_intensity": 0.2,
            "liquidity_stress": -0.1, "policy_catalyst": 0.4,
        },
        "stance": "bullish",
    }


class TestRiskFactorEnvCompat(unittest.TestCase):
    """RiskFactorAugmentedEnv 与 FactorValueEnv 的构造/行为兼容."""

    def setUp(self):
        np.random.seed(42)
        self.T = 80
        self.n_factors = len(SCORE_FACTORS)
        self.factor_history, self.future_returns, self.returns = _make_data(
            self.T, self.n_factors)
        self.regime = _compute_regime_features(self.returns)

    def _common_kwargs(self, with_layer: bool = False, brief: dict | None = None):
        from risk_first import RiskFirstLayer
        return dict(
            factor_history=self.factor_history,
            future_returns=self.future_returns,
            returns=self.returns,
            brief=brief,
            lookback=5,
            regime_features=self.regime,
            risk_first_layer=RiskFirstLayer() if with_layer else None,
            factor_names=list(SCORE_FACTORS),
        )

    def test_subclass_and_full_constructor_compat(self):
        """可带原 FactorValueEnv 全部参数构造, 且是 FactorValueEnv 子类."""
        env = RiskFactorAugmentedEnv(**self._common_kwargs(with_layer=True))
        self.assertIsInstance(env, FactorValueEnv)
        self.assertEqual(env.n_factors, self.n_factors)
        self.assertEqual(env.lookback, 5)
        self.assertEqual(len(env.risk_factor_names), len(RISK_FACTOR_NAMES))
        base_dim = int(FactorValueEnv(**self._common_kwargs()).observation_space.shape[0])
        self.assertEqual(env.observation_space.shape[0], base_dim + 4)
        env.close()

    def test_obs_prefix_identical_to_base_env(self):
        """增强观测的前缀与基础 FactorValueEnv 逐步完全一致 (语义不变)."""
        base = FactorValueEnv(**self._common_kwargs())
        aug = RiskFactorAugmentedEnv(**self._common_kwargs())
        base_dim = int(base.observation_space.shape[0])

        rng = np.random.default_rng(1)
        obs_b, _ = base.reset()
        obs_a, _ = aug.reset()
        np.testing.assert_allclose(obs_a[:base_dim], obs_b, rtol=1e-6, atol=1e-6)

        for _ in range(10):
            act = rng.normal(0, 0.5, self.n_factors).astype(np.float32)
            obs_b, r_b, d_b, _, info_b = base.step(act)
            obs_a, r_a, d_a, _, info_a = aug.step(act)
            np.testing.assert_allclose(obs_a[:base_dim], obs_b, rtol=1e-6, atol=1e-6)
            self.assertAlmostEqual(r_a, r_b, places=6)
            self.assertEqual(d_a, d_b)
            self.assertEqual(info_a["weights"], info_b["weights"])
        base.close()
        aug.close()

    def test_step_info_exposes_risk_factors(self):
        """step 的 info 携带 4 维风险因子 (与观测后缀一致)."""
        env = RiskFactorAugmentedEnv(**self._common_kwargs())
        env.reset()
        _, _, _, _, info = env.step(
            np.random.randn(self.n_factors).astype(np.float32))
        self.assertIn("risk_factors", info)
        self.assertEqual(len(info["risk_factors"]), 4)
        self.assertTrue(all(np.isfinite(v) for v in info["risk_factors"]))
        env.close()

    def test_risk_factor_suffix_matches_info(self):
        """观测后缀 == info["risk_factors"] (同一时点同一值)."""
        env = RiskFactorAugmentedEnv(**self._common_kwargs())
        base_dim = int(FactorValueEnv(**self._common_kwargs()).observation_space.shape[0])
        obs, _ = env.reset()
        _, _, _, _, info = env.step(
            np.random.randn(self.n_factors).astype(np.float32))
        np.testing.assert_allclose(
            obs[base_dim:], np.asarray(info["risk_factors"], dtype=np.float32),
            rtol=1e-6, atol=1e-6)
        env.close()

    def test_coexists_with_risk_first_layer(self):
        """Risk-First 惩罚与风险因子观测可叠加, 极端权重触发 risk_penalty."""
        from risk_first import RiskFirstLayer
        env = RiskFactorAugmentedEnv(**self._common_kwargs(with_layer=True))
        env.reset()
        act = np.zeros(self.n_factors, dtype=np.float32)
        act[0] = 1.0   # 极端集中动作
        obs, reward, done, _, info = env.step(act)
        self.assertIn("risk_penalty", info)
        self.assertGreaterEqual(info["risk_penalty"], 0.0)
        self.assertIn("risk_factors", info)
        self.assertTrue(np.isfinite(reward))
        env.close()

    def test_accepts_brief_and_regime_none(self):
        """brief 情绪 + regime_features=None 的旧调用方式仍可用."""
        kwargs = self._common_kwargs(brief=_brief())
        kwargs["regime_features"] = None
        env = RiskFactorAugmentedEnv(**kwargs)
        obs, _ = env.reset()
        self.assertEqual(obs.shape[0], env.observation_space.shape[0])
        self.assertTrue(np.all(np.isfinite(obs)))
        env.close()


class TestCVaRPPOWithRiskFactorEnv(unittest.TestCase):
    """CVaR_PPO 在增强环境上的完整训练/推理链路."""

    def setUp(self):
        np.random.seed(42)
        self.T = 80
        self.n_factors = len(SCORE_FACTORS)
        self.factor_history, self.future_returns, self.returns = _make_data(
            self.T, self.n_factors)
        self.regime = _compute_regime_features(self.returns)
        self.env = RiskFactorAugmentedEnv(
            factor_history=self.factor_history,
            future_returns=self.future_returns,
            returns=self.returns,
            lookback=5,
            regime_features=self.regime,
            factor_names=list(SCORE_FACTORS),
            risk_factor_window=20,
        )

    def tearDown(self):
        self.env.close()

    def test_cvar_ppo_init_predict(self):
        model = CVaR_PPO(
            "MlpPolicy", self.env, verbose=0,
            cvar_alpha=0.05, cvar_coef=0.1,
            n_steps=24, batch_size=12,
        )
        obs, _ = self.env.reset()
        self.assertEqual(obs.shape[0], self.env.observation_space.shape[0])
        action, _ = model.predict(obs, deterministic=True)
        self.assertEqual(len(action), self.n_factors)
        self.assertTrue(np.all(np.isfinite(action)))

    def test_cvar_ppo_train_rollout_weights_normalized(self):
        """训练后 deterministic rollout: 权重非负且行和≈1."""
        model = CVaR_PPO(
            "MlpPolicy", self.env, verbose=0,
            cvar_alpha=0.05, cvar_coef=0.1,
            risk_first_coef=0.0,
            n_steps=24, batch_size=12,
        )
        model.learn(total_timesteps=150)

        obs, _ = self.env.reset()
        done = False
        weights_history = []
        while not done:
            action, _ = model.predict(obs, deterministic=True)
            obs, reward, done, _, info = self.env.step(action)
            self.assertTrue(np.all(np.isfinite(obs)))
            self.assertTrue(np.isfinite(reward))
            self.assertIn("risk_factors", info)
            if info.get("weights"):
                weights_history.append(info["weights"])
        self.assertGreaterEqual(len(weights_history), 5)

        w = np.asarray(weights_history[-1])
        self.assertAlmostEqual(w.sum(), 1.0, places=5)
        self.assertTrue(np.all(w >= 0.0))


if __name__ == "__main__":
    unittest.main()
