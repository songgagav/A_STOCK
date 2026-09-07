# -*- coding: utf-8 -*-
"""单元测试: 执行层滑点双分量分解 (市场冲击 vs 执行风险) + 工具注册."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from slippage_model import (
    market_impact_rate, execution_risk_rate,
    decompose_slippage, apply_slippage,
    almglen_chriss_slippage,
)
from paper_book import PaperBook


class TestDecomposeSlippage(unittest.TestCase):
    def test_zero_horizon_matches_ac_total(self):
        """执行时长为 0 -> 执行风险=0, 总滑点==纯市场冲击==原 AC 值."""
        dec = decompose_slippage(10000, 5e8, 0.02, execution_horizon_days=0.0)
        ac = almglen_chriss_slippage(10000, 5e8, 0.02)
        self.assertEqual(dec["execution_risk_bps"], 0.0)
        self.assertAlmostEqual(dec["total_rate"], ac, places=8)
        self.assertEqual(dec["market_impact_bps"], dec["total_bps"])

    def test_two_components_independent(self):
        """正执行时长 -> 独立分解出市场冲击与执行风险两个分量."""
        dec = decompose_slippage(100000, 1e7, 0.02, execution_horizon_days=5.0)
        self.assertGreater(dec["market_impact_bps"], 0.0)
        self.assertGreater(dec["execution_risk_bps"], 0.0)
        self.assertAlmostEqual(
            dec["total_bps"], dec["market_impact_bps"] + dec["execution_risk_bps"],
            places=4)

    def test_execution_risk_grows_with_horizon(self):
        d1 = decompose_slippage(1e6, 1e8, 0.02, execution_horizon_days=1.0)
        d5 = decompose_slippage(1e6, 1e8, 0.02, execution_horizon_days=5.0)
        self.assertGreater(d5["execution_risk_bps"], d1["execution_risk_bps"])

    def test_market_impact_grows_with_participation(self):
        small = market_impact_rate(1e4, 1e8, 0.02)
        big = market_impact_rate(5e6, 1e8, 0.02)   # 参与率 5%
        self.assertGreater(big, small)

    def test_execution_risk_rate_bounds(self):
        r = execution_risk_rate(0.02, 10.0)
        self.assertGreater(r, 0.0)
        self.assertLessEqual(r, 0.02)
        self.assertEqual(execution_risk_rate(0.02, 0.0), 0.0)

    def test_apply_slippage_direction(self):
        buy_price, dec = apply_slippage(10.0, "buy", 1e4, 5e8, 0.02)
        sell_price, _ = apply_slippage(10.0, "sell", 1e4, 5e8, 0.02)
        self.assertGreater(buy_price, 10.0)
        self.assertLess(sell_price, 10.0)


class TestPaperBookDecomposition(unittest.TestCase):
    def test_buy_records_impact_and_exec_risk(self):
        pb = PaperBook(100000)
        r = pb.buy("000001.SZ", 1000, 10.0,
                   avg_daily_volume=1e7, volatility=0.02,
                   execution_horizon_days=5.0)
        self.assertIsNotNone(r)
        self.assertGreater(r["impact_bps"], 0.0)
        self.assertGreater(r["exec_risk_bps"], 0.0)
        # 成交价含两分量: buy 价 = 10*(1+impact+risk)
        exp_price = 10.0 * (1 + (r["impact_bps"] + r["exec_risk_bps"]) / 1e4)
        self.assertAlmostEqual(r["price"], exp_price, places=4)

    def test_default_fixed_slippage_no_new_fields(self):
        """未传动态滑点参数 -> 仍走固定费率, 不出现分解字段."""
        pb = PaperBook(100000)
        r = pb.buy("000001.SZ", 100, 10.0)
        self.assertIsNotNone(r)
        self.assertNotIn("impact_bps", r)

    def test_sell_decomposition_round_trip(self):
        pb = PaperBook(100000)
        pb.buy("000001.SZ", 1000, 10.0,
               avg_daily_volume=1e7, volatility=0.02,
               execution_horizon_days=2.0)
        pb.trade_date = "2026-09-05"   # 解锁 T+1
        r = pb.sell("000001.SZ", 1000, 10.5,
                    avg_daily_volume=1e7, volatility=0.02,
                    execution_horizon_days=2.0)
        self.assertIsNotNone(r)
        self.assertIn("impact_bps", r)
        self.assertIn("exec_risk_bps", r)
        self.assertLess(r["price"], 10.5)


class TestAgentToolsRegistration(unittest.TestCase):
    def test_new_tools_registered(self):
        import agent_tools
        names = [t["name"] for t in agent_tools.list_tools()]
        for n in ["risk_factor_optimize", "cafpo_latent_extract",
                  "slippage_decompose"]:
            self.assertIn(n, names, f"工具 {n} 应已注册")

    def test_slippage_tool_execute(self):
        import agent_tools
        res = agent_tools.execute(
            "slippage_decompose",
            {"order_size": 100000, "avg_daily_volume": 1e7,
             "volatility": 0.02, "execution_horizon_days": 5.0})
        self.assertTrue(res.get("ok"), res)
        self.assertGreater(res["execution_risk_bps"], 0.0)

    def test_risk_factor_tool_execute(self):
        import agent_tools
        rng = np.random.default_rng(0)
        res = agent_tools.execute(
            "risk_factor_optimize",
            {"returns": rng.normal(0, 0.02, 60).tolist(), "window": 20})
        self.assertTrue(res.get("ok"), res)
        self.assertIn("cvar95", res.get("last_normalized", {}))


if __name__ == "__main__":
    unittest.main()
