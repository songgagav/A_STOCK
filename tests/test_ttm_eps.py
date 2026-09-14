# -*- coding: utf-8 -*-
"""近似行 PE(TTM) 的聚合口径 (valuation_backfill.ttm_eps).

口径: `financials.eps` 是**累计**值, 故
  年报期(12-31)      eps_ttm = eps
  其它报告期          eps_ttm = eps(上年年报) + eps(本期累计) - eps(去年同期累计)
任一构成项缺失 -> NaN(不猜、不外推)。
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))


def _fin(rows):
    df = pd.DataFrame(rows, columns=["symbol", "ts", "eps"])
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def test_annual_period_uses_its_own_eps():
    from valuation_backfill import ttm_eps
    out = ttm_eps(_fin([("600519", "2025-12-31", 65.66)]))
    assert out.loc[0, "eps_ttm"] == 65.66


def test_quarterly_aggregation_formula():
    """2026-06-30 的 TTM = 2025 年报 + 2026 中报累计 - 2025 中报累计."""
    from valuation_backfill import ttm_eps
    rows = [("600519", "2025-12-31", 65.66), ("600519", "2025-06-30", 36.18),
            ("600519", "2026-06-30", 35.57)]
    out = ttm_eps(_fin(rows)).set_index("ts")
    exp = 65.66 + 35.57 - 36.18
    assert abs(out.loc[pd.Timestamp("2026-06-30"), "eps_ttm"] - exp) < 1e-9


def test_missing_annual_yields_nan():
    """缺上年年报 -> 不猜(否则会用错误的基数算出畸形 PE)."""
    from valuation_backfill import ttm_eps
    out = ttm_eps(_fin([("600519", "2025-06-30", 36.18),
                        ("600519", "2026-06-30", 35.57)])).set_index("ts")
    assert pd.isna(out.loc[pd.Timestamp("2026-06-30"), "eps_ttm"])


def test_missing_prior_year_same_period_yields_nan():
    from valuation_backfill import ttm_eps
    out = ttm_eps(_fin([("600519", "2025-12-31", 65.66),
                        ("600519", "2026-06-30", 35.57)])).set_index("ts")
    assert pd.isna(out.loc[pd.Timestamp("2026-06-30"), "eps_ttm"])


def test_symbols_are_independent():
    from valuation_backfill import ttm_eps
    rows = [("600519", "2025-12-31", 65.66), ("600519", "2026-06-30", 35.57),
            ("600519", "2025-06-30", 36.18),
            ("000001", "2025-12-31", 2.0), ("000001", "2026-06-30", 1.2),
            ("000001", "2025-06-30", 1.0)]
    out = ttm_eps(_fin(rows)).set_index(["symbol", "ts"])
    assert abs(out.loc[("000001", pd.Timestamp("2026-06-30")), "eps_ttm"]
               - (2.0 + 1.2 - 1.0)) < 1e-9
    assert abs(out.loc[("600519", pd.Timestamp("2026-06-30")), "eps_ttm"]
               - (65.66 + 35.57 - 36.18)) < 1e-9


def test_negative_ttm_is_preserved_for_later_masking():
    """ttm_eps 只负责聚合; 亏损(<=0)由调用方置空 PE, 此处不应把值丢掉."""
    from valuation_backfill import ttm_eps
    rows = [("000002", "2025-12-31", -1.0), ("000002", "2025-06-30", -0.4),
            ("000002", "2026-06-30", -0.6)]
    out = ttm_eps(_fin(rows)).set_index("ts")
    assert out.loc[pd.Timestamp("2026-06-30"), "eps_ttm"] == -1.0 - 0.6 + 0.4
