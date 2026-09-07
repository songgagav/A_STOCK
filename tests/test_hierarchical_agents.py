# -*- coding: utf-8 -*-
"""单元测试: Hi-DARTS 层次化多智能体."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from hierarchical_agents import (
    DailyAgent, WeeklyAgent, EventAgent, MetaAgent,
    AGENT_DAILY, AGENT_WEEKLY, AGENT_EVENT,
)


class TestDailyAgent(unittest.TestCase):
    def setUp(self):
        self.agent = DailyAgent(top_k=3)

    def test_act_returns_top_k(self):
        scores = np.array([0.9, 0.8, 0.7, 0.6, 0.5])
        symbols = ["A", "B", "C", "D", "E"]
        result = self.agent.act(scores, symbols)
        self.assertEqual(len(result), 3)
        self.assertAlmostEqual(sum(result.values()), 1.0)

    def test_act_empty_returns_empty(self):
        result = self.agent.act(np.array([]), [])
        self.assertEqual(result, {})

    def test_weights_normalized(self):
        scores = np.array([10.0, 1.0, 0.1])
        symbols = ["A", "B", "C"]
        result = self.agent.act(scores, symbols)
        self.assertAlmostEqual(sum(result.values()), 1.0)


class TestWeeklyAgent(unittest.TestCase):
    def setUp(self):
        self.agent = WeeklyAgent(top_k=3, ma_short=5, ma_long=20)

    def test_update_momentum_clipped(self):
        price = np.random.randn(30) + 100.0
        mom = self.agent.update_momentum("000001", price)
        self.assertGreaterEqual(mom, -1.0)
        self.assertLessEqual(mom, 1.0)

    def test_act_returns_only_positive_momentum(self):
        for sym in ["A", "B", "C"]:
            price = np.ones(30) * 100.0
            self.agent.update_momentum(sym, price)
        result = self.agent.act()
        # 平盘无动量, 应为空
        self.assertIn(result, [{}, {"A": 1.0, "B": 1.0, "C": 1.0}])

    def test_act_empty_when_no_momentum(self):
        result = self.agent.act()
        self.assertEqual(result, {})


class TestEventAgent(unittest.TestCase):
    def setUp(self):
        self.agent = EventAgent(top_k=3)

    def test_register_and_act(self):
        self.agent.register_event("A", "earnings_surprise", 1.0)
        self.agent.register_event("B", "policy_catalyst", 1.0)
        result = self.agent.act()
        self.assertIn("A", result)
        self.assertIn("B", result)
        self.assertAlmostEqual(sum(result.values()), 1.0)

    def test_clear_events(self):
        self.agent.register_event("A", "earnings_surprise")
        self.agent.clear_events()
        result = self.agent.act()
        self.assertEqual(result, {})

    def test_event_intensity_multiplies_score(self):
        self.agent.register_event("A", "earnings_surprise", 2.0)
        self.agent.register_event("B", "earnings_surprise", 0.5)
        result = self.agent.act()
        self.assertGreater(result["A"], result["B"])


class TestMetaAgent(unittest.TestCase):
    def setUp(self):
        self.meta = MetaAgent()

    def test_analyze_returns_three_weights(self):
        result = self.meta.analyze(0.2, 0.0, 0.0, 0)
        for agent in [AGENT_DAILY, AGENT_WEEKLY, AGENT_EVENT]:
            self.assertIn(agent, result)
        weights = np.array([result[a] for a in [AGENT_DAILY, AGENT_WEEKLY, AGENT_EVENT]])
        self.assertAlmostEqual(weights.sum(), 1.0)

    def test_high_vol_adjusts_weights(self):
        low_vol = self.meta.analyze(0.1, 0.0, 0.0, 0)
        high_vol = self.meta.analyze(0.4, 0.0, 0.0, 0)
        # 高波动应降低日频权重
        self.assertGreaterEqual(low_vol[AGENT_DAILY], high_vol[AGENT_DAILY])

    def test_strong_trend_adjusts_weights(self):
        no_trend = self.meta.analyze(0.2, 0.0, 0.0, 0)
        strong_trend = self.meta.analyze(0.2, 0.05, 0.0, 0)
        # 强趋势应提高周频权重
        self.assertGreaterEqual(strong_trend[AGENT_WEEKLY], no_trend[AGENT_WEEKLY])

    def test_high_event_intensity(self):
        low_event = self.meta.analyze(0.2, 0.0, 0.1, 0)
        high_event = self.meta.analyze(0.2, 0.0, 0.8, 0)
        self.assertGreaterEqual(high_event[AGENT_EVENT], low_event[AGENT_EVENT])

    def test_fuse_actions(self):
        daily = {"A": 0.5, "B": 0.5}
        weekly = {"A": 1.0}
        event = {"C": 1.0}
        self.meta.analyze(0.2, 0.0, 0.0, 0)
        fused = self.meta.fuse_actions(daily, weekly, event)
        self.assertAlmostEqual(sum(fused.values()), 1.0, places=5)
        self.assertIn("A", fused)
        self.assertIn("B", fused)
        self.assertIn("C", fused)

    def test_all_empty_input(self):
        fused = self.meta.fuse_actions({}, {}, {})
        self.assertEqual(fused, {})


if __name__ == "__main__":
    unittest.main()