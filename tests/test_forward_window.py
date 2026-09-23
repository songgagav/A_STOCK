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


# --------------------------------------------------------------------------
# 前向**持有期**必须与 lookback **解耦** (2026-09-23)
# --------------------------------------------------------------------------
def test_forward_requirement_uses_forward_days_not_lookback(cal, monkeypatch):
    """**核心回归**: 前向数据需求由 `forward_days` 决定, **不是** `lookback_days`。

    2026-09-23 实测缺陷: 前向持有期硬编码复用 `lookback_days`, 而
    `run_daily` 用 `lookback_days=20` + `vnpy_regime_days=5`(取最近 5 个交易日)
    调用 ⇒ 要求每个决策日**之后**还有 20 个交易日, 而最近的决策日之后只剩 1~5 天
    ⇒ **10/10 个 (日,场景) 组合全部 ok=false**, 整个路线图 ⑨ 号能力空转。

    本用例造一个**足够长**的日历, 并把 lookback 设得远大于 forward_days:
    必须能跑通(即按 forward_days 判断), 而不是报"未来数据不足"。
    """
    seen: list[int] = []
    real_fwd = vb.forward_window_days

    def spy(start, days):
        seen.append(days)
        return real_fwd(start, days)

    monkeypatch.setattr(vb, "forward_window_days", spy)
    monkeypatch.setattr(vb, "_load_selection",
                        lambda day_dir: [{"canon": "000001.SZ", "name": "x"}])
    monkeypatch.setattr(vb, "_load_bars", _fake_bars)
    # 取一个"之后还有充足交易日"的决策日(距末端 40 天)
    day = cal[-40]
    try:
        res = vb.run_vnpy_backtest(day, top_n=1, lookback_days=120, forward_days=3,
                                   forward=True, out_tag="t", persist_arctic=False)
    except ModuleNotFoundError as e:
        # 本仓 `.venv314` 没有 vnpy, 函数会在窗口校验**之后**抛错。这恰好证明
        # 校验通过了(否则会提前 return "未来数据不足", 根本走不到那行 import)。
        assert "vnpy" in str(e), e
        res = {}
    # 断言只看**窗口校验**这一环 —— 这才是本用例要守的不变量。
    assert seen, "forward_window_days 从未被调用 —— 校验路径没走到"
    assert 3 in seen, f"前向窗口没有按 forward_days=3 校验: {seen}"
    assert 120 not in seen, (
        f"前向窗口仍在用 lookback_days 校验: {seen} —— "
        "持有期与回看期是**两个不同的量**, 共用参数会让 10/10 组合全部失败")
    assert "未来数据不足" not in str((res or {}).get("error") or ""), (
        f"按 forward_days=3 校验后仍报未来数据不足 —— 该决策日之后有 40 个交易日: "
        f"{(res or {}).get('error')}")


def test_forward_days_defaults_from_config(monkeypatch):
    """不传 `forward_days` 时应取 `PAPER.regime_forward_days`, 且是个小整数。

    它必须**显著小于** lookback(默认 20): 否则"最近 N 个交易日"永远凑不齐未来数据。
    """
    import config
    fd = int(config.PAPER.get("regime_forward_days") or 0)
    assert fd >= 1, "regime_forward_days 必须为正整数"
    assert fd <= 10, (
        f"regime_forward_days={fd} 太大: 前向评估取的是**最近**的交易日, "
        "持有期一长就没有任何决策日能凑齐未来数据(2026-09-23 实测 20 天时 10/10 全失败)")
    # 与调仓间隔一致是本项的取值依据(真实组合约每 3 个自然日调仓一次)
    assert fd == int(config.PAPER.get("rebalance_interval_days", fd)), (
        "持有期应与 rebalance_interval_days 一致, 否则成本敏感性与执行口径不符")


def test_comparison_row_keeps_error(cal, monkeypatch):
    """`run_regime_scenarios` 的 comparison 行**必须带 error**。

    DISC-2 ⑤「归因在中间层丢失」的又一实例: `run_vnpy_backtest` 有 error,
    而构造 row 时只挑 7 个字段把它丢了 ⇒ `run_regime_batch` 汇总出的
    10 个组合全是 `{ok:false, total_return_pct:null, ...}`, **一个原因都看不到**,
    真因("前向窗口未来数据不足")被呈现成"所有组合都没产出统计"。
    """
    monkeypatch.setattr(vb, "_load_selection",
                        lambda day_dir: [{"canon": "000001.SZ", "name": "x"}])
    monkeypatch.setattr(vb, "_load_bars", _fake_bars)
    last = cal[-1]                     # 末端日: 必然"未来数据不足"
    r = vb.run_regime_scenarios(last, scenarios=["normal"], top_n=1,
                                lookback_days=20, forward=True, persist_arctic=False)
    rows = r.get("comparison") or []
    assert rows, "没有 comparison 行"
    assert rows[0]["ok"] is False
    assert rows[0].get("error"), (
        "comparison 行丢了 error —— 失败原因在中间层消失, 排查会被引向错方向")
    assert "未来数据不足" in str(rows[0]["error"])


# --------------------------------------------------------------------------
# 决策日选取必须按**前向持有期**留足未来数据 (2026-09-23)
# --------------------------------------------------------------------------
def _pick_like_run_daily(all_days, rdays, fdays):
    """复刻 `run_daily` 的决策日选取, 返回被选中的交易日。

    刻意复刻而不是 import: 那段逻辑在 `run_daily` 的闭包里, 无法直接调用。
    **复刻有漂移风险**, 故下面同时断言 `run_daily.py` 源码里就是 `-(_fd + 1)` ——
    这样一旦有人改了源码而没改这里, 用例会失败而不是悄悄放过。
    """
    cand = all_days[:-(fdays + 1)] if len(all_days) > fdays + 1 else []
    return cand[-rdays:] if cand else []


def test_every_picked_decision_day_has_enough_future(cal):
    """**每一个**被选中的决策日, 之后都必须有 >= forward_days 个交易日。

    2026-09-23 实测: 原实现留 `lookback_days`(=20) 个交易日, 而 `vnpy_regime_days`
    只取 5 天 ⇒ 要求决策日之后还有 20 天 ⇒ 最近的决策日之后只剩 1~5 天
    ⇒ 10/10 组合全 `ok=false`。
    我第一次"修"成 `_all[-(_rdays + _reserve):-1]` 时只排除了末端**一天**,
    于是 pick 里最后两天仍缺未来数据 —— 故本用例对**每个**决策日逐一断言,
    而不是只看第一个或最后一个。
    """
    for fdays in (1, 3, 5, 20):
        pick = _pick_like_run_daily(cal, 5, fdays)
        assert len(pick) == 5, (fdays, pick)
        for d in pick:
            i = cal.index(d)
            remain = len(cal) - i - 1
            assert remain >= fdays, (
                f"forward_days={fdays}: 决策日 {d} 之后仅剩 {remain} 个交易日 "
                f"(需 {fdays}) —— 该组合必然失败, 且失败原因会被埋掉")


def test_run_daily_source_uses_the_correct_slice():
    """`run_daily` 的切片上界必须是 `-(_fd + 1)`, 防止源码与上面的复刻漂移。"""
    p = os.path.join(ROOT, "src", "run_daily.py")
    src = open(p, encoding="utf-8").read()
    assert "_all[:-(_fd + 1)]" in src, (
        "run_daily 的决策日切片不是 `_all[:-(_fd + 1)]` —— "
        "若改成只排除末端一天, pick 里最后几天仍会缺未来数据(2026-09-23 踩过)")
    assert "forward_days=_fd" in src, (
        "run_daily 必须显式传 forward_days —— 不传就走 PAPER 默认值, "
        "与日期选取用的局部变量可能漂移(选取按 A 留、校验按 B 判)")


