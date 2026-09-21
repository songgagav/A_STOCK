# -*- coding: utf-8 -*-
"""data_quality_guard 结构校验的回归测试（路线图 #5 第一刀）.

背景: 2026-09-04 事故 —— 406 只 volume 错 100 倍、5172 条 turnover 写成小数,
**静默**进入 h5i。当时的防线只有 dtype 约束, 管不了值域与关系。
本模块的检查全部取"任何来源、任何量纲下都不可能合法"的行, 故测试用纯构造成交验证。
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from data_quality_guard import CORE, gate_decision, validate_daily_bars  # noqa: E402


def _clean(n=5):
    """完全合法的 5 行。"""
    return pd.DataFrame({
        "symbol": [f"00000{i}" for i in range(1, n + 1)],
        "date": pd.to_datetime("2026-09-18"),
        "open": [10.0] * n, "high": [10.5] * n, "low": [9.8] * n, "close": [10.2] * n,
        "volume": [1_000_000.0] * n, "amount": [10_000_000.0] * n,
        "change_pct": [1.0] * n, "turnover": [0.5] * n,
    })


class TestCleanPasses:
    def test_clean_df_is_ok(self):
        r = validate_daily_bars(_clean())
        assert r["ok"] is True and r["bad_rows"] == 0
        assert all(v == 0 for v in r["checks"].values())

    def test_empty_df_is_ok(self):
        r = validate_daily_bars(pd.DataFrame())
        assert r["ok"] is True and r["bad_rows"] == 0

    def test_change_pct_and_turnover_nan_allowed(self):
        """**关键契约**: 这两个字段的 NaN 是合法情形（镜像历史上未提供 turnover）。
        若这条变成 FAIL, 说明有人把"允许空"误改成了"禁止空"。"""
        df = _clean()
        df["change_pct"] = float("nan")
        df["turnover"] = float("nan")
        r = validate_daily_bars(df)
        assert r["ok"] is True

    def test_volume_int_series_accepted(self):
        """引擎返回的 int 在被转成 float64 之前也应能校验（to_numeric 会失败）——
        校验器不得因为 dtype 崩溃。"""
        df = _clean()
        df["volume"] = df["volume"].astype("int64")
        r = validate_daily_bars(df)
        assert "volume" in r["checks"] or r["ok"] is True  # 值合法即可


class TestViolations:
    def test_ohlc_relation_high_below_close(self):
        df = _clean()
        df.loc[0, "high"] = 9.5        # high < close=10.2
        r = validate_daily_bars(df)
        assert r["ok"] is False and r["bad_rows"] == 1
        assert r["checks"]["ohlc_relations"] == 1

    def test_ohlc_relation_low_above_open(self):
        df = _clean()
        df.loc[1, "low"] = 10.3        # low > open=10.0
        r = validate_daily_bars(df)
        assert r["checks"]["ohlc_relations"] == 1 and r["bad_rows"] == 1

    def test_zero_and_negative_prices(self):
        df = _clean()
        df.loc[0, "close"] = 0.0
        df.loc[1, "open"] = -1.0
        r = validate_daily_bars(df)
        assert r["checks"]["positive_prices"] == 2 and r["bad_rows"] == 2

    def test_negative_volume_and_amount(self):
        df = _clean()
        df.loc[0, "volume"] = -100.0
        df.loc[1, "amount"] = -5.0
        r = validate_daily_bars(df)
        assert r["checks"]["nonneg_volume"] == 1
        assert r["checks"]["nonneg_amount"] == 1
        assert r["bad_rows"] == 2

    def test_nan_in_core_columns(self):
        df = _clean()
        df.loc[0, "volume"] = float("nan")
        df.loc[2, "high"] = float("nan")
        r = validate_daily_bars(df)
        assert r["checks"]["core_nan"] == 2 and r["bad_rows"] == 2

    def test_multiple_violations_on_same_row_counted_once(self):
        """坏行按**行**去重 —— 一行同时违反多项, bad_rows 只算一次。"""
        df = _clean()
        df.loc[0, "high"] = 1.0          # 关系
        df.loc[0, "close"] = -3.0        # 非正
        r = validate_daily_bars(df)
        assert r["bad_rows"] == 1
        assert r["checks"]["ohlc_relations"] == 1
        assert r["checks"]["positive_prices"] == 1

    def test_sample_lists_symbols(self):
        df = _clean()
        df.loc[0, "volume"] = -1.0
        r = validate_daily_bars(df)
        assert r["sample"][0]["check"] == "nonneg_volume"
        assert "000001" in r["sample"][0]["symbols"]


class TestMissingColumns:
    def test_missing_core_column_reports_error(self):
        df = _clean().drop(columns=["amount"])
        r = validate_daily_bars(df)
        assert r["ok"] is False and "amount" in r["error"]
        assert r["bad_rows"] == len(df)


class TestGateDecision:
    """入库闸门判定（抽出来就是为了这里能在 CI 里测）。"""

    def test_clean_passes(self):
        g = gate_decision(validate_daily_bars(_clean()))
        assert g["reject"] is False and g["ok"] is True

    def test_violation_rejects(self):
        df = _clean()
        df.loc[0, "high"] = 1.0
        g = gate_decision(validate_daily_bars(df))
        assert g["reject"] is True and g["ok"] is False
        assert g["bad_rows"] == 1
        assert "ohlc_relations=1行" in g["error"]

    def test_reject_reason_names_check_and_counts(self):
        """**响亮**: 错误必须说清是哪种违规、几行, 而不是一句"校验失败"。"""
        df = _clean()
        df.loc[0, "volume"] = -1.0
        df.loc[1, "close"] = 0.0
        g = gate_decision(validate_daily_bars(df))
        assert "nonneg_volume=1行" in g["error"]
        assert "positive_prices=1行" in g["error"]
        assert g["bad_rows"] == 2

    def test_reject_lists_symbols(self):
        df = _clean()
        df.loc[0, "open"] = 0.0
        g = gate_decision(validate_daily_bars(df))
        assert "000001" in (g["sample"] or [[]])[0]

    def test_missing_column_rejects_with_error(self):
        g = gate_decision(validate_daily_bars(_clean().drop(columns=["high"])))
        assert g["reject"] is True and "high" in g["error"]
