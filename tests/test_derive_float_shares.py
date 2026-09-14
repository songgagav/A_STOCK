# -*- coding: utf-8 -*-
"""valuation_backfill.derive_float_shares: 快照 float_shares 整列缺失时的换算兜底。

背景 (2026-09-14 观察期首日):
    上游 2026-09-08 段 valuation_snapshot 的 float_shares / total_shares 整列 NULL
    (5551/5551), 但 float_mv 与 price 完整。不补则 `ln_size` 全 NaN,
    规模中性化静默退化为仅行业中性。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))


def _f():
    import valuation_backfill
    return valuation_backfill.derive_float_shares


def test_derives_when_missing():
    fs, n = _f()([np.nan], [11.0e10], [11.0])      # float_mv 已是"元"
    assert n == 1
    assert abs(float(fs.iloc[0]) - 1.0e10) < 1e0


def test_keeps_existing_value():
    fs, n = _f()([1.0e10], [11.0e10], [11.0])
    assert n == 0
    assert float(fs.iloc[0]) == 1.0e10


def test_mixed_rows_only_fills_gap():
    fs, n = _f()([np.nan, 2.0e9, np.nan],
                 [11.0e10, 11.0e10, np.nan],
                 [11.0, 11.0, 11.0])
    assert n == 1                       # 第二行已有值; 第三行缺 float_mv
    assert abs(float(fs.iloc[0]) - 1.0e10) < 1e0
    assert float(fs.iloc[1]) == 2.0e9
    assert np.isnan(float(fs.iloc[2]))


def test_zero_or_negative_price_is_not_used():
    """price<=0 无法换算, 保持 NaN(不猜)."""
    fs, n = _f()([np.nan, np.nan], [1.0e10, 1.0e10], [0.0, -5.0])
    assert n == 0
    assert fs.isna().all()


def test_zero_free_cap_is_not_used():
    fs, n = _f()([np.nan], [0.0], [11.0])
    assert n == 0
    assert np.isnan(float(fs.iloc[0]))


def test_returns_series_aligned_positionally():
    """返回序列必须与输入**位置**对齐(调用方用 .to_numpy())."""
    fs, _ = _f()([np.nan, 5.0], [22.0e10, 1.0], [11.0, 1.0])
    assert len(fs) == 2
    assert abs(float(fs.iloc[0]) - 2.0e10) < 1e0
    assert float(fs.iloc[1]) == 5.0
