# -*- coding: utf-8 -*-
"""单元测试: 归因分析 (attribution_analysis)."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from attribution_analysis import (
    _spearman, daily_returns_from_equity, segment_report,
    daily_ic_from_samples, rolling_ic_stats,
    audit_exposure, audit_vnpy_summary,
)


class TestSpearman(unittest.TestCase):
    def test_perfect_monotone(self):
        r = _spearman(list(range(1, 31)), [x * 2 + 1 for x in range(30)])
        self.assertAlmostEqual(r, 1.0, places=6)

    def test_inverse(self):
        y = list(range(30, 0, -1))
        self.assertAlmostEqual(_spearman(list(range(1, 31)), y), -1.0, places=6)

    def test_too_short_none(self):
        self.assertIsNone(_spearman([1, 2], [3, 4]))


class TestDailyReturnsAndSegment(unittest.TestCase):
    def test_returns_calc(self):
        days, pct = daily_returns_from_equity([
            {"day": "d1", "equity": 100.0},
            {"day": "d2", "equity": 102.0},
            {"day": "d3", "equity": 99.0},
        ])
        self.assertEqual(days, ["d2", "d3"])
        self.assertAlmostEqual(pct[0], 0.02)
        self.assertAlmostEqual(pct[1], 99.0 / 102.0 - 1.0)

    def test_segment_report_detects_concentration(self):
        # 4 天盈利 + 1 天把收益全打回
        recs = [
            {"day": "20260901", "equity": 100.0},
            {"day": "20260902", "equity": 102.0},
            {"day": "20260903", "equity": 104.0},
            {"day": "20260904", "equity": 106.0},
            {"day": "20260907", "equity": 101.0},
        ]
        s = segment_report(recs)
        self.assertTrue(s["ok"])
        self.assertEqual(s["n_days"], 4)
        self.assertAlmostEqual(s["total_return_pct"], 1.0, places=4)
        self.assertEqual(s["top_gain_days"][0]["pnl_pct"], 2.0)
        self.assertEqual(s["top_loss_days"][0]["day"], "20260907")

    def test_insufficient_points(self):
        s = segment_report([{"day": "a", "equity": 100.0}])
        self.assertFalse(s["ok"])


class TestDailyICAndRolling(unittest.TestCase):
    def _samples(self):
        rng = np.random.default_rng(0)
        out = []
        for i in range(60):
            syms = [f"{k:06d}" for k in range(500)]
            z = {s: float(np.random.randn()) for s in syms}
            # 分数与 5 日收益正相关 + 噪声
            f5 = {s: 0.5 * z[s] + float(rng.normal(0, 0.5)) for s in syms}
            out.append({"date": f"2026-{(i % 12)+1:02d}-01", "scores": z, "fwd5": f5,
                        "fwd1": {}, "n_pool": 500})
        return out

    def test_daily_ic_from_samples(self):
        ic = daily_ic_from_samples(self._samples())
        self.assertEqual(len(ic), 60)
        ics = [d["fwd5_ic"] for d in ic if d["fwd5_ic"] is not None]
        self.assertGreater(len(ics), 50)
        self.assertTrue(all(-1.0 <= v <= 1.0 for v in ics))

    def test_rolling_stats_shape(self):
        samples = self._samples()
        ic = daily_ic_from_samples(samples)
        st = rolling_ic_stats(ic, window=20)
        self.assertIn("overall", st)
        self.assertIn("series", st)
        self.assertGreaterEqual(st["n_days"], 50)

    def test_failure_signal_when_recent_negative(self):
        ic = [{"date": f"d{i:03d}", "fwd5_ic": 0.05, "fwd1_ic": 0.05}
              for i in range(40)]
        for i in range(20):
            ic[20 + i]["fwd5_ic"] = -0.02 - i * 0.001   # 尾部转负
        st = rolling_ic_stats(ic, window=20)
        self.assertTrue(st["failure_signals"], "应检出尾部负 IC 信号")
        self.assertLess(st["recent"]["mean"], 0.0)


class TestBehaviorAudit(unittest.TestCase):
    def test_audit_exposure_constant(self):
        rows = [{"day": f"d{i}", "equity": 100.0, "cash": 20.0,
                 "positions": {f"{j:06d}": {} for j in range(8)}} for i in range(6)]
        a = audit_exposure(rows)
        self.assertTrue(a["ok"])
        self.assertAlmostEqual(a["avg_exposure"], 0.80, places=4)
        self.assertEqual(a["avg_n_positions"], 8.0)
        self.assertEqual(a["turnover_approx_daily"], 0.0)

    def test_audit_exposure_changes(self):
        rows = [
            {"day": "d1", "equity": 100.0, "cash": 10.0, "positions": {}},
            {"day": "d2", "equity": 100.0, "cash": 40.0, "positions": {}},
            {"day": "d3", "equity": 100.0, "cash": 10.0, "positions": {}},
        ]
        a = audit_exposure(rows)
        self.assertGreater(a["exposure_max"] - a["exposure_min"], 0.2)

    def test_audit_vnpy_summary(self):
        s = {
            "weights": [0.1] * 10,
            "stats": {
                "total_days": "121", "total_trade_count": "10",
                "total_turnover": 81027.0, "capital": 100000.0,
            },
        }
        a = audit_vnpy_summary(s)
        self.assertTrue(a["ok"])
        self.assertEqual(a["n_symbols"], 10)
        self.assertEqual(a["total_trades"], 10)
        self.assertAlmostEqual(a["turnover_rounds_total"], 0.8103, places=3)
        self.assertIsNotNone(a["avg_holding_days_approx"])


if __name__ == "__main__":
    unittest.main()
