# -*- coding: utf-8 -*-
"""pe_ttm 补丁合并的不变量 (factor_fusion._merge_pe_patch).

背景: 主表 valuation 在部分交易日整体缺 pe_ttm(上游写入缺口), ep 因子因此失效。
补丁必须"仅填空、不覆盖、不引入新行", 否则会污染样本或改写已有一致数据。
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))


def _main_df() -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["600519", "600519", "000001"],
        "d": pd.to_datetime(["2026-03-05", "2026-03-04", "2026-03-05"]),
        "pe_ttm": [None, 30.0, 5.0],
        "pb": [1.0, 2.0, 3.0],
        "float_shares": [1.0, 1.0, 1.0],
        "is_st": [False, False, False],
    })


def test_fills_gap_only_and_keeps_main_value():
    from factor_fusion import _merge_pe_patch
    patch = pd.DataFrame({"symbol": ["600519", "000001"],
                          "d": pd.to_datetime(["2026-03-05", "2026-03-05"]),
                          "pe_ttm": [12.5, 99.9]})
    out = _merge_pe_patch(_main_df(), patch)
    assert len(out) == 3
    gap = (out.symbol == "600519") & (out.d == pd.Timestamp("2026-03-05"))
    assert out.loc[gap, "pe_ttm"].iloc[0] == 12.5, "缺口应被补丁填上"
    assert out.loc[out.symbol == "000001", "pe_ttm"].iloc[0] == 5.0, \
        "主表已有值不得被补丁覆盖(仅填空)"
    keep = (out.symbol == "600519") & (out.d == pd.Timestamp("2026-03-04"))
    assert out.loc[keep, "pe_ttm"].iloc[0] == 30.0
    assert list(out.columns) == list(_main_df().columns)


def test_does_not_introduce_new_rows():
    from factor_fusion import _merge_pe_patch
    patch = pd.DataFrame({"symbol": ["999999"], "d": pd.to_datetime(["2026-03-05"]),
                          "pe_ttm": [1.0]})
    out = _merge_pe_patch(_main_df(), patch)
    assert len(out) == 3
    assert "999999" not in set(out["symbol"]), "补丁不得引入主表没有的 (symbol, 日期)"


def test_duplicate_patch_keys_are_rejected_not_multiplied():
    """补丁若未按 (symbol,d) 去重, 合并会放大行数 -> 必须整体放弃以免污染."""
    from factor_fusion import _merge_pe_patch
    d = _main_df()
    patch = pd.DataFrame({"symbol": ["600519", "600519"],
                          "d": pd.to_datetime(["2026-03-05", "2026-03-05"]),
                          "pe_ttm": [1.0, 2.0]})
    out = _merge_pe_patch(d, patch)
    assert len(out) == len(d)
    gap = (out.symbol == "600519") & (out.d == pd.Timestamp("2026-03-05"))
    assert pd.isna(out.loc[gap, "pe_ttm"].iloc[0])


def test_empty_or_missing_patch_is_noop():
    from factor_fusion import _merge_pe_patch
    d = _main_df()
    assert _merge_pe_patch(d, None).equals(d)
    assert _merge_pe_patch(d, pd.DataFrame()).equals(d)
    empty = pd.DataFrame({"symbol": [], "d": pd.to_datetime([]), "pe_ttm": []})
    assert _merge_pe_patch(d, empty).equals(d)
