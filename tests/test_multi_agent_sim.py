# -*- coding: utf-8 -*-
"""单元测试: StockMARL 多智能体模拟学习."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from multi_agent_sim import (
    IntradayTrader, MomentumChaser, RiskAverse,
    ValueInvestor, HeterogeneousAgentSim,
)


class TestIntradayTrader(unittest.TestCase):
    def setUp(self):
        self.agent = IntradayTrader()

    def test_act_returns_weights(self):
        # 确定性下跌序列: 最新收益低于近5日均值 -> 均值回归买入信号
        rets = {"A": np.linspace(0.02, -0.02, 10)}
        result = self.agent.act(rets)
        self.assertIn("A", result)
        self.assertAlmostEqual(sum(result.values()), 1.0)

    def test_short_series_skipped(self):
        rets = {"A": np.array([0.01])}
        result = self.agent.act(rets)
        self.assertEqual(result, {})


class TestMomentumChaser(unittest.TestCase):
    def setUp(self):
        self.agent = MomentumChaser()

    def test_positive_momentum(self):
        rets = {"A": np.random.randn(20) * 0.01 + 0.005}
        result = self.agent.act(rets)
        self.assertIn("A", result)

    def test_negative_momentum_returns_empty(self):
        rets = {"A": np.random.randn(20) * 0.01 - 0.01}
        result = self.agent.act(rets)
        # 负动量不选
        self.assertIn(result, [{}, {"A": 1.0}])


class TestRiskAverse(unittest.TestCase):
    def setUp(self):
        self.agent = RiskAverse(max_positions=3)

    def test_act_limits_positions(self):
        rets = {f"{i}": np.random.randn(30) * 0.01 + 0.001 for i in range(10)}
        vols = {f"{i}": 0.02 for i in range(10)}
        for i in range(5):
            vols[f"{i}"] = 0.01  # 低波动
        result = self.agent.act(rets, vols)
        self.assertLessEqual(len(result), 3)

    def test_all_high_vol_returns_empty(self):
        rets = {f"{i}": np.random.randn(30) * 0.05 for i in range(5)}
        vols = {f"{i}": 0.05 for i in range(5)}
        result = self.agent.act(rets, vols)
        # 高波动不选
        self.assertIn(result, [{}, {}])


class TestValueInvestor(unittest.TestCase):
    def setUp(self):
        self.agent = ValueInvestor(top_k=3)

    def test_act_returns_top_k(self):
        fundamentals = {
            "A": {"pb": 0.5, "roe": 0.2},
            "B": {"pb": 1.0, "roe": 0.15},
            "C": {"pb": 2.0, "roe": 0.1},
            "D": {"pb": 1.5, "roe": 0.05},
        }
        result = self.agent.act(fundamentals)
        self.assertLessEqual(len(result), 3)
        self.assertAlmostEqual(sum(result.values()), 1.0)

    def test_negative_pb_skipped(self):
        fundamentals = {"A": {"pb": -1.0, "roe": 0.2}}
        result = self.agent.act(fundamentals)
        self.assertEqual(result, {})


class TestHeterogeneousAgentSim(unittest.TestCase):
    def setUp(self):
        self.sim = HeterogeneousAgentSim()

    def test_step_returns_all_agents(self):
        rets = {f"{i:06d}": np.random.randn(30) * 0.02 for i in range(5)}
        fundamentals = {f"{i:06d}": {"pb": 1.0, "roe": 0.1} for i in range(5)}
        actions = self.sim.step(rets, fundamentals)
        for name in self.sim.agent_names:
            self.assertIn(name, actions)

    def test_consensus_signal(self):
        rets = {f"{i:06d}": np.random.randn(30) * 0.02 for i in range(5)}
        fundamentals = {f"{i:06d}": {"pb": 1.0, "roe": 0.1} for i in range(5)}
        actions = self.sim.step(rets, fundamentals)
        consensus = self.sim.get_consensus_signal(actions)
        self.assertGreater(len(consensus), 0)
        for v in consensus.values():
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)

    def test_herding_index(self):
        rets = {f"{i:06d}": np.random.randn(30) * 0.02 for i in range(5)}
        fundamentals = {f"{i:06d}": {"pb": 1.0, "roe": 0.1} for i in range(5)}
        actions = self.sim.step(rets, fundamentals)
        herding = self.sim.get_herding_index(actions)
        self.assertGreaterEqual(herding, 0.0)
        self.assertLessEqual(herding, 1.0)

    def test_state_vector(self):
        rets = {f"{i:06d}": np.random.randn(30) * 0.02 for i in range(5)}
        fundamentals = {f"{i:06d}": {"pb": 1.0, "roe": 0.1} for i in range(5)}
        self.sim.step(rets, fundamentals)
        vec = self.sim.get_state_vector()
        self.assertEqual(len(vec), 4)
        self.assertTrue(np.all(np.isfinite(vec)))

    def test_agent_names(self):
        names = self.sim.agent_names
        self.assertIn("intraday_trader", names)
        self.assertIn("momentum_chaser", names)
        self.assertIn("risk_averse", names)
        self.assertIn("value_investor", names)


if __name__ == "__main__":
    unittest.main()