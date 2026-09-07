# -*- coding: utf-8 -*-
"""单元测试: build_adj_close 复权缺口修复 (P1) 与 import 链."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from factor_library import build_adj_close


class TestBuildAdjClose(unittest.TestCase):
    def test_import_chain_ok(self):
        """p08_signal 依赖链可 import (此前 build_adj_close 断链)."""
        import p08_signal  # noqa: F401
        self.assertTrue(callable(p08_signal.build_adj_close))

    def test_no_gap_returns_same_as_raw(self):
        df = pd.DataFrame({"close": [10.0, 11.0, 10.45],
                           "change_pct": [np.nan, 10.0, -5.0]})
        adj = build_adj_close(df).to_numpy()
        # 无除权缺口: 复权价 == 原始价
        np.testing.assert_allclose(adj, [10.0, 11.0, 10.45], rtol=1e-9)

    def test_gap_eliminated(self):
        """某日 change_pct 与 close 不成比例(模拟除权缺口)时链式消除."""
        df = pd.DataFrame({"close": [10.0, 10.5, 9.975],
                           "change_pct": [np.nan, 5.0, -5.0]})
        adj = build_adj_close(df).to_numpy()
        # 复权后收益必须等于 change_pct 口径
        self.assertAlmostEqual(adj[1] / adj[0] - 1.0, 0.05, places=6)
        self.assertAlmostEqual(adj[2] / adj[1] - 1.0, -0.05, places=6)
        # 锚点 = 最新收盘
        self.assertAlmostEqual(adj[2], 9.975, places=6)

    def test_no_change_pct_fallback_raw(self):
        df = pd.DataFrame({"close": [10.0, 8.0, 12.0]})
        adj = build_adj_close(df).to_numpy()
        np.testing.assert_allclose(adj, [10.0, 8.0, 12.0], rtol=1e-9)

    def test_missing_values_tolerated(self):
        df = pd.DataFrame({"close": [10.0, np.nan, 11.0],
                           "change_pct": [np.nan, 3.0, np.nan]})
        adj = build_adj_close(df)
        self.assertTrue(np.isfinite(adj.iloc[0]))
        self.assertTrue(np.isfinite(adj.iloc[-1]))


if __name__ == "__main__":
    unittest.main()
