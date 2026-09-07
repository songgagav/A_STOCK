# -*- coding: utf-8 -*-
"""单元测试: 策略达标检测 (strategy_validation)."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from strategy_validation import (
    THRESHOLDS, REQUIRED_METRICS,
    curve_metrics, trade_metrics, evaluate,
    metrics_from_vnpy_summary, metrics_from_curve,
    detect_source,
)


class TestCurveMetrics(unittest.TestCase):
    def test_monotone_down_exact(self):
        """单边下跌: 回撤与总收益数值可精确核对."""
        m = curve_metrics([100.0, 90.0, 81.0])
        self.assertAlmostEqual(m["max_drawdown"], 0.19, places=6)
        self.assertAlmostEqual(m["total_return"], -0.19, places=6)
        self.assertAlmostEqual(m["recovery"], -1.0, places=4)
        self.assertLess(m["cagr"], 0.0)

    def test_monotone_up_no_drawdown(self):
        """严格单边上涨(步长变化): 无回撤 -> calmar/recovery/sortino 为空, 夏普为正."""
        eq = list(100.0 + np.cumsum(np.linspace(0.05, 1.0, 200)))
        m = curve_metrics(eq)
        self.assertEqual(m["max_drawdown"], 0.0)
        self.assertIsNone(m["calmar"])
        self.assertIsNone(m["recovery"])
        self.assertIsNone(m["sortino"])   # 无下行波动
        self.assertGreater(m["sharpe"], 0.0)

    def test_recovery_equals_total_over_dd(self):
        """恢复因子 = 总收益 / 最大回撤."""
        rng = np.random.default_rng(3)
        eq = [100.0]
        for i in range(250):
            eq.append(eq[-1] * (1 + float(rng.normal(0.0008, 0.006))))
        m = curve_metrics(eq)
        self.assertGreater(m["max_drawdown"], 0.0)
        expect = m["total_return"] / m["max_drawdown"]
        self.assertAlmostEqual(m["recovery"], expect, places=4)

    def test_calmar_equals_cagr_over_dd(self):
        m = curve_metrics([100.0, 103.0, 101.0, 106.0, 104.0, 110.0])
        if m["calmar"] is not None:
            expect = m["cagr"] / m["max_drawdown"]
            # 指标已 4 位舍入, 用相对误差校验
            self.assertLess(abs(m["calmar"] - expect) / max(abs(expect), 1e-9), 0.01)

    def test_aux_wfe_cv_range(self):
        """WFE/CV 若可算则处于合理范围."""
        rng = np.random.default_rng(5)
        eq = [100.0]
        for i in range(400):
            eq.append(eq[-1] * (1 + float(rng.normal(0.001, 0.005))))
        m = curve_metrics(eq)
        if m["wfe"] is not None:
            self.assertGreater(m["wfe"], -5.0)
        if m["cv"] is not None:
            self.assertGreaterEqual(m["cv"], 0.0)

    def test_short_curve_returns_partial(self):
        m = curve_metrics([100.0, 101.0])
        self.assertLessEqual(m["n_points"], 2)


class TestTradeMetrics(unittest.TestCase):
    def test_known_values(self):
        t = trade_metrics([300.0, 100.0, -100.0])
        self.assertEqual(t["n_trades"], 3)
        self.assertAlmostEqual(t["win_rate"], 0.6667, places=3)
        self.assertAlmostEqual(t["profit_factor"], 4.0)
        self.assertAlmostEqual(t["expectancy"], 100.0)
        self.assertAlmostEqual(t["gross_profit"], 400.0)
        self.assertAlmostEqual(t["gross_loss"], 100.0)

    def test_all_losses_zero_profit_factor(self):
        """全亏损: 盈利=0 -> 盈亏比 0 (0/亏损), 判定为未达标."""
        t = trade_metrics([-50.0, -150.0])
        self.assertEqual(t["win_rate"], 0.0)
        self.assertEqual(t["profit_factor"], 0.0)
        self.assertLess(t["expectancy"], 0.0)

    def test_empty(self):
        t = trade_metrics([])
        self.assertEqual(t["n_trades"], 0)
        self.assertIsNone(t["expectancy"])


class TestEvaluate(unittest.TestCase):
    def _good(self):
        return {
            "sharpe": 1.5, "sortino": 1.4, "calmar": 1.2, "cagr": 0.12,
            "max_drawdown": 0.10, "recovery": 2.5, "profit_factor": 2.0,
            "win_rate": 0.55, "expectancy": 50.0, "wfe": 0.6, "cv": 0.15,
        }

    def test_all_pass(self):
        ev = evaluate(self._good())
        self.assertEqual(ev["verdict"], "PASS")
        self.assertEqual(ev["failed_required"], [])
        self.assertEqual(ev["missing_required"], [])
        self.assertEqual(len(ev["passed_required"]), len(REQUIRED_METRICS))

    def test_one_fail(self):
        m = self._good()
        m["sharpe"] = 0.5
        ev = evaluate(m)
        self.assertEqual(ev["verdict"], "FAIL")
        self.assertEqual(ev["failed_required"], ["sharpe"])

    def test_max_drawdown_over_limit_fails(self):
        m = self._good()
        m["max_drawdown"] = 0.45
        ev = evaluate(m)
        self.assertIn("max_drawdown", ev["failed_required"])

    def test_missing_required_insufficient(self):
        m = self._good()
        m["win_rate"] = None
        ev = evaluate(m)
        self.assertEqual(ev["verdict"], "INSUFFICIENT_DATA")
        self.assertIn("win_rate", ev["missing_required"])

    def test_missing_aux_does_not_block(self):
        """辅助指标缺失不影响基础验证结论."""
        m = self._good()
        m["wfe"] = None
        m["cv"] = None
        ev = evaluate(m)
        self.assertEqual(ev["verdict"], "PASS")

    def test_expectancy_pass_when_zero(self):
        m = self._good()
        m["expectancy"] = 0.0
        ev = evaluate(m)
        self.assertEqual(ev["verdict"], "PASS")


class TestVnpyAdapter(unittest.TestCase):
    def _summary(self):
        return {
            "stats": {
                "annual_return": 20.0,
                "max_ddpercent": -10.0,
                "max_drawdown": -5000.0,
                "total_net_pnl": 2000.0,
                "sharpe_ratio": 1.2,
                "total_trade_count": "8",
                "capital": 100000.0,
            }
        }

    def test_mapping(self):
        m = metrics_from_vnpy_summary(self._summary())
        self.assertAlmostEqual(m["sharpe"], 1.2)
        self.assertAlmostEqual(m["cagr"], 0.20, places=4)
        self.assertAlmostEqual(m["max_drawdown"], 0.10, places=4)
        self.assertAlmostEqual(m["calmar"], 2.0, places=4)
        self.assertAlmostEqual(m["recovery"], 0.4, places=4)
        self.assertAlmostEqual(m["expectancy"], 250.0, places=2)
        self.assertIsNone(m["sortino"])
        self.assertIsNone(m["profit_factor"])
        self.assertIsNone(m["win_rate"])

    def test_evaluate_verdict_insufficient(self):
        m = metrics_from_vnpy_summary(self._summary())
        ev = evaluate(m)
        self.assertEqual(ev["verdict"], "INSUFFICIENT_DATA")
        self.assertEqual(sorted(ev["missing_required"]), ["profit_factor", "sortino", "win_rate"])

    def test_no_trade_count(self):
        s = self._summary()
        del s["stats"]["total_trade_count"]
        m = metrics_from_vnpy_summary(s)
        self.assertIsNone(m["expectancy"])


class TestCurveAdapter(unittest.TestCase):
    def test_dict_curve(self):
        eq = 100.0
        curve = []
        for i in range(60):
            eq *= 1.003
            curve.append({"day": f"2026-{(i % 12) + 1:02d}-01", "equity": round(eq, 2)})
        m = metrics_from_curve(curve)
        self.assertGreater(m["total_return"], 0.0)
        self.assertIn("_meta", m)

    def test_flat_curve(self):
        m = metrics_from_curve([{"day": "20260101", "equity": 100.0},
                                {"day": "20260102", "equity": 100.0},
                                {"day": "20260105", "equity": 100.0}])
        self.assertEqual(m["total_return"], 0.0)


class TestDetectSource(unittest.TestCase):
    def test_unknown_source(self):
        r = detect_source("not_a_source")
        self.assertFalse(r["ok"])

    def test_vnpy_or_error(self):
        """有/无 vnpy 数据都返回结构正确的 dict."""
        r = detect_source("vnpy")
        if r.get("ok"):
            self.assertIn("verdict", r)
        else:
            self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
