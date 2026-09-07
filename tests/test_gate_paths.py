# -*- coding: utf-8 -*-
"""单元测试: selector 旧 API 兼容层 + paper_after_gate 数据源 + 新脚本语法."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from factor_library import (
    selector_weights, score_factor, _pb_rev_score, _roe_score, _mf_net_score,
)


class TestSelectorCompat(unittest.TestCase):
    def test_selector_weights_keys(self):
        W = selector_weights()
        for k in ("signal", "trend", "govern", "liquidity",
                  "vol", "mom_rev", "pb_rev", "roe", "mf_net"):
            self.assertIn(k, W)

    def test_score_factor_vol_monotonic(self):
        bars_hi = pd.DataFrame({"close": np.cumprod(
            1 + np.random.default_rng(0).normal(0, 0.02, 40))})
        bars_lo = pd.DataFrame({"close": np.cumprod(
            1 + np.random.default_rng(1).normal(0, 0.002, 40))})
        s_lo = score_factor("vol", bars_lo)
        s_hi = score_factor("vol", bars_hi)
        self.assertGreater(s_lo, s_hi, "低波动应得更高分")

    def test_score_factor_mom_reversal(self):
        up = pd.DataFrame({"close": np.linspace(10, 20, 40)})   # 上涨
        down = pd.DataFrame({"close": np.linspace(20, 10, 40)})  # 下跌
        self.assertGreater(score_factor("mom", down), score_factor("mom", up))

    def test_pb_roe_mf_ranges(self):
        self.assertGreater(_pb_rev_score(0.8), _pb_rev_score(2.5))
        self.assertGreater(_roe_score(0.2), _roe_score(0.05))
        self.assertGreater(_mf_net_score(5e6, 1e8), _mf_net_score(-5e6, 1e8))
        self.assertEqual(_pb_rev_score(None), 0.5)
        self.assertEqual(_roe_score(None), 0.5)
        self.assertEqual(_mf_net_score(None, None), 0.5)


class TestPaperAfterGateSource(unittest.TestCase):
    def _write_ledger(self, tmp: str) -> str:
        rows = [{"date": "2026-09-08", "equity": 100000.0, "regime": "caution",
                 "exposure_mult": 0.7, "freeze_new_buys": False,
                 "interval_days": 5, "gate_active": True},
                {"date": "2026-09-09", "equity": 100300.0, "regime": "normal",
                 "exposure_mult": 1.0, "freeze_new_buys": False,
                 "interval_days": 3, "gate_active": True}]
        p = os.path.join(tmp, "paper_after_gate.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "start": "2026-09-08", "end": "2026-09-09"}, f)
        return p

    def test_loader_and_detect(self):
        import strategy_validation as sv
        tmp = tempfile.mkdtemp()
        self._write_ledger(tmp)
        with patch.object(sv, "DATA_DIR", tmp):
            res = sv.detect_source("paper_after_gate")
            self.assertTrue(res["ok"], res)
            self.assertIn("2026-09-08", res["meta"]["tag"])
            self.assertGreater(len(res["rows"]), 0)

    def test_missing_ledger_error(self):
        import strategy_validation as sv
        tmp = tempfile.mkdtemp()
        with patch.object(sv, "DATA_DIR", tmp):
            res = sv.detect_source("paper_after_gate")
            self.assertFalse(res["ok"])


class TestNewScriptsCompile(unittest.TestCase):
    def test_compile_new_modules(self):
        for name in ("refresh_gate_ic.py", "backtest_with_gate.py",
                     "factor_gate.py", "attribution_analysis.py",
                     "gate_refresh_daemon.py", "scheduler_entry.py",
                     "gate_sensitivity.py", "backfill_vnpy_risk.py",
                      "vnpy_backtest.py", "performance_report.py", "dashboard.py",
                      "backtest_audit.py"):
            p = os.path.join(_BASE, name)
            with open(p, encoding="utf-8") as f:
                compile(f.read(), name, "exec")


if __name__ == "__main__":
    unittest.main()
