# -*- coding: utf-8 -*-
"""集成测试: Risk-First / Logic-Q / 动态因子权重 与 CVaR_PPO / FactorValueEnv 兼容性.

验证端到端流程:
  1. CVaR_PPO 带 risk_first_coef > 0 初始化 + 训练
  2. FactorValueEnv 带 risk_first_layer 运行
  3. Logic-Q 调优参数注入 CVaR_PPO
  4. 动态因子权重加载 + 融合
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from drl_train import CVaR_PPO, FactorValueEnv, _compute_regime_features, SCORE_FACTORS
from risk_first import RiskFirstLayer, LLMVarianceFilter, RiskExposurePenalty, CircuitBreaker
from logic_q import LogicQ, SymbolicTrendEngine
from factor_dynamic_weights import DYNAMIC_FACTORS, apply_dynamic_weights, load_dynamic_weights


class TestRiskFirstWithCVaRPPO(unittest.TestCase):
    """Risk-First 与 CVaR_PPO 集成测试."""

    def setUp(self):
        # 最小化 FactorValueEnv 数据
        np.random.seed(42)
        self.T = 60
        self.n_factors = 4
        self.factor_history = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.5
        self.future_returns = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.01
        self.returns = np.random.randn(self.T).astype(np.float64) * 0.02
        self.regime_features = _compute_regime_features(self.returns)

    def test_cvar_ppo_initializes_with_risk_first_coef(self):
        """CVaR_PPO 可以带 risk_first_coef > 0 初始化."""
        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime_features,
            risk_first_layer=RiskFirstLayer(),
            factor_names=SCORE_FACTORS,
        )
        model = CVaR_PPO(
            "MlpPolicy", env, verbose=0,
            cvar_alpha=0.05, cvar_coef=0.1,
            risk_first_coef=0.05,
            n_steps=24, batch_size=12,
        )
        self.assertIsNotNone(model)
        self.assertEqual(model.risk_first_coef, 0.05)
        self.assertIsNotNone(model._risk_first_layer)
        env.close()

    def test_cvar_ppo_risk_first_training(self):
        """CVaR_PPO 带 risk_first_coef 可训练 100 步."""
        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime_features,
            risk_first_layer=RiskFirstLayer(),
            factor_names=SCORE_FACTORS,
        )
        model = CVaR_PPO(
            "MlpPolicy", env, verbose=0,
            cvar_alpha=0.05, cvar_coef=0.1,
            risk_first_coef=0.05,
            n_steps=24, batch_size=12,
        )
        model.learn(total_timesteps=100)
        # 验证训练后可以 predict
        obs, _ = env.reset()
        action, _ = model.predict(obs, deterministic=True)
        self.assertEqual(len(action), self.n_factors)
        self.assertTrue(np.all(np.isfinite(action)), "predict 输出应有限值")
        env.close()

    def test_risk_first_penalty_in_env_reward(self):
        """FactorValueEnv 带 risk_first_layer 时奖励包含惩罚项."""
        rfl = RiskFirstLayer()
        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime_features,
            risk_first_layer=rfl,
            factor_names=["a", "b", "c", "d"],
        )
        env.reset()
        # 用极端权重触发惩罚
        obs, reward, done, _, info = env.step(np.array([0.9, 0.1, 0.0, 0.0], dtype=np.float32))
        self.assertIn("risk_penalty", info)
        self.assertGreaterEqual(info["risk_penalty"], 0.0)
        self.assertTrue(np.isfinite(reward))
        env.close()

    def test_risk_first_no_penalty_without_layer(self):
        """不带 risk_first_layer 时 info 无 risk_penalty 字段."""
        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime_features,
            risk_first_layer=None,
        )
        env.reset()
        obs, reward, done, _, info = env.step(np.random.randn(self.n_factors).astype(np.float32))
        self.assertNotIn("risk_penalty", info)
        env.close()

    def test_circuit_breaker_integration(self):
        """熔断级别 3 时 apply_position_limit 返回全零权重."""
        rfl = RiskFirstLayer()
        rfl.circuit_break_check(0.15)
        weights = np.array([0.2, 0.2, 0.2, 0.2, 0.2])
        adjusted = rfl.apply_position_limit(weights)
        self.assertAlmostEqual(adjusted.sum(), 0.0)

    def test_variance_filter_with_llm_signals(self):
        """方差过滤器在 CVaR_PPO 环境中正确处理 LLM 信号."""
        rfl = RiskFirstLayer()
        # 先输入一组稳定信号
        for _ in range(10):
            rfl.filter_llm_signals([0.5, 0.5, 0.5, 0.5])
        # 再输入异常信号
        filtered, conf = rfl.filter_llm_signals([10.0, 10.0, 10.0, 10.0])
        self.assertLess(conf, 1.0, "异常信号应降权")
        self.assertTrue(np.all(np.isfinite(filtered)), "过滤后信号应有限值")


class TestLogicQWithPolicyTuning(unittest.TestCase):
    """Logic-Q 与策略网络调优集成测试."""

    def setUp(self):
        np.random.seed(42)
        self.closes = np.ones(100, dtype=float) * 100.0
        self.closes[50:] = self.closes[49] + np.cumsum(
            np.random.randn(50) * 0.3 + 0.5)
        self.lq = LogicQ()

    def test_logic_q_produces_compatible_tuning(self):
        """Logic-Q 输出与 neural_ta.py compute_tuning 格式兼容."""
        tuning = self.lq.get_policy_tuning(closes=self.closes)
        # 必须包含 neural_ta 的 3 个字段
        for key in ["delta_scale", "temperature", "weight_clip"]:
            self.assertIn(key, tuning, f"缺失 {key}")
        # Logic-Q 特有字段
        self.assertIn("trend_bias", tuning)
        self.assertIn("mode", tuning)
        self.assertEqual(tuning["mode"], "logic_q")

    def test_logic_q_tuning_in_valid_range(self):
        """调优参数在合理范围内."""
        tuning = self.lq.get_policy_tuning(closes=self.closes)
        self.assertGreaterEqual(tuning["delta_scale"], 0.2)
        self.assertLessEqual(tuning["delta_scale"], 1.5)
        self.assertGreaterEqual(tuning["temperature"], 0.5)
        self.assertLessEqual(tuning["temperature"], 2.5)
        self.assertGreaterEqual(tuning["weight_clip"], 0.3)
        self.assertLessEqual(tuning["weight_clip"], 0.7)
        self.assertGreaterEqual(tuning["trend_bias"], -1.0)
        self.assertLessEqual(tuning["trend_bias"], 1.0)

    def test_logic_q_tuning_can_modify_ppo_params(self):
        """Logic-Q 调优参数可以应用于 CVaR_PPO 的超参数调整."""
        env = FactorValueEnv(
            np.random.randn(60, 4).astype(np.float32) * 0.5,
            np.random.randn(60, 4).astype(np.float32) * 0.01,
            np.random.randn(60).astype(np.float64) * 0.02,
            lookback=5,
        )
        model = CVaR_PPO("MlpPolicy", env, verbose=0,
                         n_steps=24, batch_size=12,
                         ent_coef=0.01)

        # 模拟 Logic-Q 调优
        tuning = self.lq.get_policy_tuning(closes=self.closes)
        model.ent_coef = float(model.ent_coef) * tuning["temperature"]
        model.learning_rate = model.learning_rate * tuning["delta_scale"]
        self.assertGreater(model.ent_coef, 0)
        self.assertGreater(model.learning_rate, 0)
        env.close()

    def test_logic_q_analyze_returns_consistent_state(self):
        """连续两次分析返回相同趋势状态."""
        r1 = self.lq.analyze(self.closes)
        r2 = self.lq.analyze(self.closes)
        self.assertEqual(r1["trend_state"], r2["trend_state"])
        self.assertEqual(r1["trend_state_name"], r2["trend_state_name"])

    def test_logic_q_uptrend_downtrend_different_tuning(self):
        """上升趋势和下降趋势应产生不同的调优参数."""
        np.random.seed(42)
        up = np.ones(100, dtype=float) * 100.0
        up[50:] = up[49] + np.cumsum(np.random.randn(50) * 0.3 + 0.5)

        down = np.ones(100, dtype=float) * 100.0
        down[50:] = down[49] - np.cumsum(np.random.randn(50) * 0.3 + 0.5)

        up_tuning = self.lq.get_policy_tuning(closes=up)
        down_tuning = self.lq.get_policy_tuning(closes=down)

        # 上升趋势 delta_scale 应大于下降趋势
        if up_tuning["trend_state_name"] == "UPTREND" and down_tuning["trend_state_name"] == "DOWNTREND":
            self.assertGreater(up_tuning["delta_scale"], down_tuning["delta_scale"])


class TestDynamicWeightsIntegration(unittest.TestCase):
    """PPO 动态因子权重与 FactorValueEnv 集成测试."""

    def setUp(self):
        np.random.seed(42)
        self.T = 60
        self.n_factors = 5  # pb_inv, ep, ocf_ps, roe_yy_chg, gp4
        self.factor_history = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.5
        self.future_returns = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.01
        self.returns = np.random.randn(self.T).astype(np.float64) * 0.02
        self.regime_features = _compute_regime_features(self.returns)

    def test_factor_value_env_with_5_factors(self):
        """FactorValueEnv 支持 5 因子 (动态因子权重场景)."""
        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime_features,
            factor_names=DYNAMIC_FACTORS,
        )
        obs, _ = env.reset()
        # 观测维度: 5*5 + 4 + 1 + 3 = 33
        expected_dim = 5 * self.n_factors + 4 + 1 + 3
        self.assertEqual(len(obs), expected_dim)
        self.assertEqual(env.action_space.shape[0], self.n_factors)
        env.close()

    def test_factor_value_env_rollout_with_5_factors(self):
        """5 因子环境 rollout 正常."""
        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime_features,
            factor_names=DYNAMIC_FACTORS,
        )
        env.reset()
        for _ in range(30):
            a = np.random.randn(self.n_factors).astype(np.float32) * 0.3
            obs, reward, done, _, info = env.step(a)
            if done:
                break
        self.assertIn("weights", info)
        self.assertEqual(len(info["weights"]), self.n_factors)
        env.close()

    def test_apply_dynamic_weights_blend(self):
        """动态权重融合保持和为 1."""
        static = {"pb_inv": 0.25, "ep": 0.25, "ocf_ps": 0.25, "roe_yy_chg": 0.25}
        dynamic = {"pb_inv": 0.4, "ep": 0.3, "ocf_ps": 0.15, "roe_yy_chg": 0.15}
        blended = apply_dynamic_weights(static, dynamic, blend_ratio=0.3)
        self.assertAlmostEqual(sum(blended.values()), 1.0, places=5)

    def test_apply_dynamic_weights_with_gp4(self):
        """动态权重融合包含 gp4 因子."""
        static = {"pb_inv": 0.2, "ep": 0.2, "ocf_ps": 0.2, "roe_yy_chg": 0.2, "gp4": 0.2}
        dynamic = {"pb_inv": 0.5, "ep": 0.3, "ocf_ps": 0.1, "roe_yy_chg": 0.05, "gp4": 0.05}
        blended = apply_dynamic_weights(static, dynamic, blend_ratio=0.5)
        self.assertAlmostEqual(sum(blended.values()), 1.0, places=5)
        self.assertIn("gp4", blended)

    def test_load_dynamic_weights_nonexistent(self):
        """不存在的日期返回 None."""
        result = load_dynamic_weights("00000000")
        self.assertIsNone(result)

    def test_dynamic_factor_names_match(self):
        """DYNAMIC_FACTORS 包含预期因子."""
        self.assertEqual(len(DYNAMIC_FACTORS), 5)
        self.assertIn("pb_inv", DYNAMIC_FACTORS)
        self.assertIn("gp4", DYNAMIC_FACTORS)
        self.assertIn("ep", DYNAMIC_FACTORS)
        self.assertIn("ocf_ps", DYNAMIC_FACTORS)
        self.assertIn("roe_yy_chg", DYNAMIC_FACTORS)


class TestThreeModuleEndToEnd(unittest.TestCase):
    """三个模块串联端到端测试.

    模拟场景: 训练一个带 Risk-First 约束的 CVaR_PPO,
    同时使用 Logic-Q 调优参数和动态权重.
    """

    def setUp(self):
        np.random.seed(42)
        self.T = 80
        self.n_factors = 5
        self.factor_history = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.5
        self.future_returns = np.random.randn(self.T, self.n_factors).astype(np.float32) * 0.01
        self.returns = np.random.randn(self.T).astype(np.float64) * 0.02
        self.regime_features = _compute_regime_features(self.returns)

    def test_risk_first_and_factor_value_env_together(self):
        """Risk-First + FactorValueEnv 5 因子 + 训练 100 步."""
        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime_features,
            risk_first_layer=RiskFirstLayer(),
            factor_names=DYNAMIC_FACTORS,
        )
        model = CVaR_PPO(
            "MlpPolicy", env, verbose=0,
            cvar_alpha=0.05, cvar_coef=0.1,
            risk_first_coef=0.05,
            n_steps=24, batch_size=12,
        )
        model.learn(total_timesteps=100)
        obs, _ = env.reset()
        action, _ = model.predict(obs, deterministic=True)
        self.assertEqual(len(action), self.n_factors)
        self.assertTrue(np.all(np.isfinite(action)))
        env.close()

    def test_tune_cvar_ppo_with_logic_q_and_risk_first(self):
        """先用 Logic-Q 调优, 再带 Risk-First 训练."""
        # 模拟 Logic-Q 调优
        closes = np.ones(100, dtype=float) * 100.0
        closes[50:] = closes[49] + np.cumsum(np.random.randn(50) * 0.3 + 0.5)
        lq = LogicQ()
        tuning = lq.get_policy_tuning(closes=closes)

        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime_features,
            risk_first_layer=RiskFirstLayer(),
            factor_names=DYNAMIC_FACTORS,
        )
        model = CVaR_PPO(
            "MlpPolicy", env, verbose=0,
            cvar_alpha=0.05, cvar_coef=0.1,
            risk_first_coef=0.05,
            n_steps=24, batch_size=12,
        )
        # 应用 Logic-Q 调优
        model.ent_coef = float(model.ent_coef) * tuning["temperature"]
        model.learning_rate = model.learning_rate * tuning["delta_scale"]

        model.learn(total_timesteps=100)
        obs, _ = env.reset()
        action, _ = model.predict(obs, deterministic=True)
        self.assertTrue(np.all(np.isfinite(action)))
        env.close()


if __name__ == "__main__":
    unittest.main()