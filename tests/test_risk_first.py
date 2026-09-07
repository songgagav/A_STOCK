# -*- coding: utf-8 -*-
"""单元测试: Risk-First 架构 (方差过滤器/暴露惩罚/熔断)."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from risk_first import (
    LLMVarianceFilter, RiskExposurePenalty, CircuitBreaker, RiskFirstLayer,
)


class TestLLMVarianceFilter(unittest.TestCase):
    def test_returns_1_before_window_full(self):
        f = LLMVarianceFilter(window=5, n_std=2.0)
        for i in range(4):
            conf = f.update(float(i))
            self.assertEqual(conf, 1.0, f"第{i}步应返回 1.0")

    def test_normal_signal_returns_1(self):
        f = LLMVarianceFilter(window=10, n_std=2.0)
        for _ in range(10):
            f.update(0.5)
        conf = f.update(0.5)
        self.assertEqual(conf, 1.0)

    def test_anomaly_signal_reduces_confidence(self):
        f = LLMVarianceFilter(window=10, n_std=2.0)
        for _ in range(10):
            f.update(0.0)
        conf = f.update(10.0)  # 巨大异常
        self.assertLess(conf, 1.0, "异常信号应降低置信度")
        self.assertGreaterEqual(conf, f.min_confidence, "不应低于最小置信度")

    def test_reset_clears_history(self):
        f = LLMVarianceFilter(window=10, n_std=2.0)
        for _ in range(5):
            f.update(1.0)
        f.reset()
        self.assertEqual(len(f._history), 0)

    def test_state_dict_roundtrip(self):
        f = LLMVarianceFilter(window=10, n_std=2.0)
        for _ in range(12):
            f.update(np.random.randn())
        state = f.state_dict()
        f2 = LLMVarianceFilter()
        f2.load_state_dict(state)
        self.assertEqual(len(f2._history), len(f._history))
        self.assertEqual(f2.window, f.window)


class TestRiskExposurePenalty(unittest.TestCase):
    def setUp(self):
        self.rp = RiskExposurePenalty(
            exposure_thresholds={"a": 0.5, "b": 0.5},
            penalty_coef=1.0,
        )

    def test_no_penalty_within_threshold(self):
        p = self.rp.compute([0.3, 0.3], ["a", "b"])
        self.assertEqual(p, 0.0)

    def test_penalty_exceeds_threshold(self):
        p = self.rp.compute([0.8, 0.2], ["a", "b"])
        self.assertGreater(p, 0.0)
        expected = (0.8 - 0.5) ** 2 * 1.0
        self.assertAlmostEqual(p, expected)

    def test_portfolio_concentration_penalty(self):
        p = self.rp.compute_portfolio_penalty(
            [0.2, 0.2, 0.2, 0.2, 0.2], concentration_limit=0.15)
        expected = sum((w - 0.15) ** 2 for w in [0.2, 0.2, 0.2, 0.2, 0.2]) * 2.0
        self.assertAlmostEqual(p, expected)

    def test_no_penalty_for_diversified(self):
        p = self.rp.compute_portfolio_penalty(
            [0.1, 0.1, 0.1], concentration_limit=0.15)
        self.assertEqual(p, 0.0)


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.cb = CircuitBreaker(
            drawdown_threshold=0.08,
            vol_threshold=0.35,
            cvar_threshold=0.05,
            drawdown_acceleration=0.03,
        )

    def test_normal_state_level_0(self):
        level = self.cb.evaluate(0.03)
        self.assertEqual(level, 0)

    def test_drawdown_warning_level_1(self):
        level = self.cb.evaluate(0.09)
        self.assertEqual(level, 1)
        self.assertIn("drawdown", self.cb.triggered)

    def test_multi_trigger_level_2(self):
        level = self.cb.evaluate(0.09, annualized_vol=0.40)
        self.assertEqual(level, 2)

    def test_extreme_drawdown_level_3(self):
        level = self.cb.evaluate(0.15)
        self.assertEqual(level, 3)

    def test_triple_trigger_level_3(self):
        level = self.cb.evaluate(0.09, annualized_vol=0.40,
                                 cvar_95=0.06, drawdown_5d_change=0.04)
        self.assertEqual(level, 3)

    def test_position_limit(self):
        self.cb._level = 0
        self.assertEqual(self.cb.get_position_limit(), 1.0)
        self.cb._level = 1
        self.assertEqual(self.cb.get_position_limit(), 0.7)
        self.cb._level = 2
        self.assertEqual(self.cb.get_position_limit(), 0.3)
        self.cb._level = 3
        self.assertEqual(self.cb.get_position_limit(), 0.0)

    def test_reset(self):
        self.cb.evaluate(0.09)
        self.assertGreater(self.cb.level, 0)
        self.cb.reset()
        self.assertEqual(self.cb.level, 0)
        self.assertEqual(len(self.cb.triggered), 0)


class TestRiskFirstLayer(unittest.TestCase):
    def test_filter_llm_signals(self):
        rfl = RiskFirstLayer()
        signals = [0.0, 0.0, 0.0, 0.0]
        filtered, conf = rfl.filter_llm_signals(signals)
        self.assertEqual(len(filtered), 4)
        self.assertGreaterEqual(conf, 0.0)
        self.assertLessEqual(conf, 1.0)

    def test_step_reward_penalty(self):
        rfl = RiskFirstLayer()
        penalty = rfl.step_reward_penalty(
            [0.8, 0.2], factor_names=["a", "b"])
        self.assertGreater(penalty, 0.0)

    def test_circuit_break_check(self):
        rfl = RiskFirstLayer()
        level = rfl.circuit_break_check(0.09)
        self.assertGreaterEqual(level, 0)

    def test_apply_position_limit_normal(self):
        rfl = RiskFirstLayer()
        weights = np.array([0.2, 0.2, 0.2, 0.2, 0.2])
        adjusted = rfl.apply_position_limit(weights)
        self.assertAlmostEqual(adjusted.sum(), 1.0)

    def test_apply_position_limit_circuit_break(self):
        rfl = RiskFirstLayer()
        rfl.circuit_break_check(0.15)
        weights = np.array([0.2, 0.2, 0.2, 0.2, 0.2])
        adjusted = rfl.apply_position_limit(weights)
        self.assertAlmostEqual(adjusted.sum(), 0.0, msg="level 3 应清仓")

    def test_state_dict(self):
        rfl = RiskFirstLayer()
        rfl.filter_llm_signals([0.1, 0.2, 0.3, 0.4])
        state = rfl.state_dict()
        self.assertIn("variance_filter", state)
        self.assertIn("circuit_level", state)


if __name__ == "__main__":
    unittest.main()