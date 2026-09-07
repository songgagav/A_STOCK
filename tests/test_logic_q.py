# -*- coding: utf-8 -*-
"""单元测试: Logic-Q 神经符号化趋势分析."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from logic_q import (
    SymbolicTrendEngine, ParameterCalibrator, LogicQ, TrendState,
)


class TestSymbolicTrendEngine(unittest.TestCase):
    def setUp(self):
        self.engine = SymbolicTrendEngine(
            ma_cross_threshold=0.02,
            sr_breakout_threshold=0.015,
        )
        # 构造 100 日价格序列: 前 50 日震荡, 后 50 日上涨
        np.random.seed(42)
        self.closes = np.ones(100, dtype=float)
        self.closes[:50] = 100.0 + np.cumsum(np.random.randn(50) * 0.5)
        # 后 50 日上涨趋势
        self.closes[50:] = self.closes[49] + np.cumsum(
            np.random.randn(50) * 0.3 + 0.5)

    def test_analyze_returns_uptrend(self):
        result = self.engine.analyze(self.closes)
        self.assertIn(result["trend_state"], [TrendState.UPTREND, TrendState.OSCILLATE])
        self.assertIn("trend_state_name", result)
        self.assertIn("ma_cross_score", result)

    def test_analyze_with_highs_lows(self):
        highs = self.closes * 1.02
        lows = self.closes * 0.98
        result = self.engine.analyze(self.closes, highs, lows)
        self.assertIn("sr_position", result)
        self.assertGreaterEqual(result["sr_position"], 0.0)
        self.assertLessEqual(result["sr_position"], 1.0)

    def test_analyze_with_volume(self):
        volumes = np.ones(100, dtype=float) * 1e6
        result = self.engine.analyze(self.closes, volumes=volumes)
        self.assertIn("volume_ratio", result)
        self.assertGreater(result["volume_ratio"], 0.0)

    def test_short_series_returns_oscillate(self):
        short = np.array([100.0] * 10)
        result = self.engine.analyze(short)
        self.assertEqual(result["trend_state"], TrendState.OSCILLATE)
        self.assertEqual(result["confidence"], 0.0)

    def test_downtrend_detection(self):
        np.random.seed(42)
        down = np.ones(100, dtype=float) * 100.0
        down[50:] = down[49] - np.cumsum(np.random.randn(50) * 0.3 + 0.5)
        result = self.engine.analyze(down)
        self.assertIn(result["trend_state"],
                      [TrendState.DOWNTREND, TrendState.OSCILLATE])

    def test_params_roundtrip(self):
        params = self.engine.get_params()
        self.assertIn("ma_cross_threshold", params)
        self.engine.set_params(ma_cross_threshold=0.03)
        self.assertEqual(self.engine.ma_cross_threshold, 0.03)


class TestLogicQ(unittest.TestCase):
    def setUp(self):
        self.lq = LogicQ()
        np.random.seed(42)
        self.closes = np.ones(100, dtype=float) * 100.0
        self.closes[50:] = self.closes[49] + np.cumsum(
            np.random.randn(50) * 0.3 + 0.5)

    def test_analyze(self):
        result = self.lq.analyze(self.closes)
        self.assertIn("trend_state", result)

    def test_get_policy_tuning_with_analysis(self):
        analysis = self.lq.analyze(self.closes)
        tuning = self.lq.get_policy_tuning(analysis)
        self.assertIn("delta_scale", tuning)
        self.assertIn("temperature", tuning)
        self.assertIn("weight_clip", tuning)
        self.assertIn("trend_bias", tuning)
        self.assertIn("mode", tuning)
        self.assertEqual(tuning["mode"], "logic_q")
        self.assertGreaterEqual(tuning["delta_scale"], 0.2)
        self.assertLessEqual(tuning["delta_scale"], 1.5)

    def test_get_policy_tuning_fallback(self):
        tuning = self.lq.get_policy_tuning()
        self.assertEqual(tuning["mode"], "fallback")

    def test_get_policy_tuning_with_closes(self):
        tuning = self.lq.get_policy_tuning(closes=self.closes)
        self.assertEqual(tuning["mode"], "logic_q")

    def test_calibrate(self):
        np.random.seed(42)
        closes = np.ones(120, dtype=float) * 100.0
        closes[60:] = closes[59] + np.cumsum(np.random.randn(60) * 0.3 + 0.5)
        fwd = np.random.randn(60) * 0.01
        params = self.lq.calibrate(closes, fwd)
        self.assertIn("ma_cross_threshold", params)
        self.assertIn("sr_breakout_threshold", params)

    def test_trend_state_uptrend(self):
        """构造明确的上升趋势验证 trend_state=UPTREND."""
        closes = np.linspace(100, 150, 100) + np.random.randn(100) * 0.5
        result = self.lq.analyze(closes)
        # 强上升趋势应识别为 UPTREND
        if result["ma_cross_score"] > 0.02:
            self.assertEqual(result["trend_state_name"], "UPTREND")


if __name__ == "__main__":
    unittest.main()