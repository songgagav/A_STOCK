# -*- coding: utf-8 -*-
"""前向窗口(无前视)回归测试 (2026-09-13).

背景: 原 run_vnpy_backtest 用 _load_bars(s6, day, 120) 取 [day-120, day] 的日线,
而目标池是 as-of day 的 PIT 选股 —— 评估期整体落在决策日之前, 选股"知道"了评估期
走势(实测 2022-12-30 窗口回测 +90.03%, 而所选 10 只在该区间自身的等权涨幅即 +93.47%,
两者几乎相等, 属机械前视)。现新增 forward=True: 窗口 [决策日, 决策日+N 个交易日]。

本测试用合成日历与合成行情, 不依赖数据库/网络。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

vb = pytest.importorskip("vnpy_backtest",
                         reason="需要 polars 等依赖(见 requirements-dev.txt)")


def _fake_calendar(start="2022-01-03", end="2022-12-30"):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]


@pytest.fixture
def cal(monkeypatch):
    c = _fake_calendar()
    monkeypatch.setattr(vb, "_full_calendar", lambda: list(c))
    return c


# --------------------------------------------------------------------------
# forward_window_days
# --------------------------------------------------------------------------
def test_window_starts_on_or_after_start_day(cal):
    got = vb.forward_window_days(dt.date(2022, 3, 1), 30)
    assert len(got) == 30
    assert got[0] >= "2022-03-01"
    assert got == sorted(got)
    assert got[-1] == cal[cal.index(got[0]) + 29]


def test_window_starts_at_exact_trading_day(cal):
    d = cal[10]
    got = vb.forward_window_days(dt.date.fromisoformat(d), 5)
    assert got[0] == d


def test_window_skips_non_trading_start(cal):
    """起点落在非交易日时, 从其后第一个交易日开始(不向前取)."""
    start = dt.date(2022, 3, 5)           # 周六
    got = vb.forward_window_days(start, 3)
    assert got[0] > start.strftime("%Y-%m-%d")


def test_returns_empty_when_insufficient_future(cal):
    """接近数据末端(未来不足 N 天)必须返回 [], 不得回退到历史数据."""
    last = cal[-1]
    assert vb.forward_window_days(dt.date.fromisoformat(last), 5) == []
    assert vb.forward_window_days(dt.date.fromisoformat(last), 1) == [last]


def test_returns_empty_after_calendar_end(cal):
    assert vb.forward_window_days(dt.date(2030, 1, 1), 5) == []


def test_empty_calendar(monkeypatch):
    monkeypatch.setattr(vb, "_full_calendar", lambda: [])
    assert vb.forward_window_days(dt.date(2022, 3, 1), 5) == []


# --------------------------------------------------------------------------
# _load_bars_forward: 核心不变量 —— 绝不包含决策日之前的数据
# --------------------------------------------------------------------------
def _fake_bars(_symbol, end_day, days):
    """模拟 _load_bars: 返回截至 end_day 的最后 days 根(升序)."""
    idx = pd.bdate_range(end=pd.Timestamp(end_day), periods=days)
    n = len(idx)
    return pd.DataFrame({
        "date": idx,
        "open": np.arange(n, dtype=float) + 1.0,
        "close": np.arange(n, dtype=float) + 1.0,
        "volume": 1.0, "amount": 1.0, "change_pct": 0.0,
    })


def test_forward_bars_exclude_pre_decision_dates(cal, monkeypatch):
    """关键不变量: 返回的每一根 bar 都 >= 决策日."""
    monkeypatch.setattr(vb, "_load_bars", _fake_bars)
    start = dt.date.fromisoformat(cal[20])
    df = vb._load_bars_forward("000001", start, 120)
    assert not df.empty
    assert df["date"].min() >= pd.Timestamp(start), "出现了决策日之前的数据(前视)"
    assert len(df) <= 120


def test_forward_bars_window_end_within_range(cal, monkeypatch):
    monkeypatch.setattr(vb, "_load_bars", _fake_bars)
    start = dt.date.fromisoformat(cal[20])
    df = vb._load_bars_forward("000001", start, 60)
    seg = vb.forward_window_days(start, 60)
    assert df["date"].max() <= pd.Timestamp(seg[-1])
    assert str(df["date"].iloc[0])[:10] == seg[0]


def test_forward_bars_empty_when_no_future(cal, monkeypatch):
    monkeypatch.setattr(vb, "_load_bars", _fake_bars)
    last = dt.date.fromisoformat(cal[-1])
    assert vb._load_bars_forward("000001", last, 20).empty


def test_forward_bars_empty_when_symbol_has_no_data(cal, monkeypatch):
    monkeypatch.setattr(vb, "_load_bars",
                        lambda *a, **k: pd.DataFrame())
    df = vb._load_bars_forward("000001", dt.date.fromisoformat(cal[5]), 30)
    assert df.empty


# --------------------------------------------------------------------------
# 失败路径: 未来数据不足时应给出明确错误, 而不是静默用历史数据回测
# --------------------------------------------------------------------------
def test_run_backtest_reports_insufficient_future(cal, monkeypatch):
    monkeypatch.setattr(vb, "_load_selection",
                        lambda day_dir: [{"canon": "000001.SZ", "name": "x"}])
    monkeypatch.setattr(vb, "_load_bars", _fake_bars)
    last = cal[-1]
    res = vb.run_vnpy_backtest(last, top_n=1, lookback_days=120, forward=True,
                               out_tag="t", persist_arctic=False)
    assert res["ok"] is False
    assert "未来数据不足" in str(res.get("error"))
