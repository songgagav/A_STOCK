# -*- coding: utf-8 -*-
"""集成测试: 风险因子观测接入 run_factor_value_drl 默认训练入口.

验证:
  1. 默认 (use_risk_factors=None) 且 config RISK_FACTOR_PPO.rfp_enabled=1
     -> 训练环境为 RiskFactorAugmentedEnv, meta.risk_obs_augmented=True
  2. 显式 use_risk_factors=False -> 回退基础 FactorValueEnv, obs_dim 不含风险段
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

import drl_train
from drl_train import run_factor_value_drl, FactorValueEnv


def _data(T: int = 60, n_factors: int = 4, seed: int = 42):
    np.random.seed(seed)
    factor_history = np.random.randn(T, n_factors).astype(np.float32) * 0.5
    future_returns = np.random.randn(T, n_factors).astype(np.float32) * 0.01
    returns = np.random.randn(T).astype(np.float64) * 0.02
    regime = drl_train._compute_regime_features(returns)
    return factor_history, future_returns, returns, regime


class TestRunFactorValueDrlRiskObs(unittest.TestCase):
    def setUp(self):
        self.day = "20260905"
        self.factor_history, self.future_returns, self.returns, self.regime = _data()
        self._tmp = tempfile.TemporaryDirectory()
        # 把训练产物重定向到临时目录, 不污染 data/drl_factor_value
        self._patcher = patch.object(drl_train, "DATA_DIR", self._tmp.name)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _base_obs_dim(self) -> int:
        env = FactorValueEnv(
            self.factor_history, self.future_returns, self.returns,
            lookback=5, regime_features=self.regime,
        )
        d = int(env.observation_space.shape[0])
        env.close()
        return d

    def test_default_enables_risk_factor_obs(self):
        """默认入口应按配置启用风险因子观测增强."""
        meta = run_factor_value_drl(
            self.day,
            self.factor_history, self.future_returns, self.returns,
            regime_features=self.regime,
            total_timesteps=100, n_epochs=2, lookback=5,
        )
        self.assertTrue(meta["ok"], meta.get("error"))
        self.assertTrue(meta["risk_obs_augmented"])
        self.assertEqual(meta["obs_dim"], self._base_obs_dim() + 4)
        self.assertEqual(len(meta["final_weights"]), self.factor_history.shape[1])

    def test_explicit_false_uses_base_env(self):
        """显式关闭 -> 回退基础 FactorValueEnv (观测不含风险段)."""
        meta = run_factor_value_drl(
            self.day,
            self.factor_history, self.future_returns, self.returns,
            regime_features=self.regime,
            total_timesteps=100, n_epochs=2, lookback=5,
            use_risk_factors=False,
        )
        self.assertTrue(meta["ok"], meta.get("error"))
        self.assertFalse(meta["risk_obs_augmented"])
        self.assertEqual(meta["obs_dim"], self._base_obs_dim())

    def test_config_disabled_defaults_off(self):
        """config rfp_enabled=0 且未显式指定 -> 默认回退基础环境."""
        with patch("config.RISK_FACTOR_PPO", {
            "rfp_enabled": False, "risk_factor_window": 20, "cvar_alpha": 0.05,
        }):
            # drl_train 内部在函数内 from config import RISK_FACTOR_PPO
            import config
            with patch.object(config, "RISK_FACTOR_PPO", {
                "rfp_enabled": False, "risk_factor_window": 20, "cvar_alpha": 0.05,
            }):
                meta = run_factor_value_drl(
                    self.day,
                    self.factor_history, self.future_returns, self.returns,
                    regime_features=self.regime,
                    total_timesteps=100, n_epochs=2, lookback=5,
                )
        self.assertTrue(meta["ok"], meta.get("error"))
        self.assertFalse(meta["risk_obs_augmented"])


if __name__ == "__main__":
    unittest.main()
