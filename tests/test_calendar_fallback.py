# -*- coding: utf-8 -*-
"""校准回归: 交易日历的**空结果不得缓存** + 静态日历回退 + 取数失败告警.

背景(2026-09-13): `_full_calendar()` 原先把失败得到的空列表写进进程缓存, 于是
一旦首个调用失败(如 h5i_db 客户端缺失), 该进程内所有 forward_window_days 都返回
[], 上层显示为"前向窗口未来数据不足", 真实故障被伪装成数据不全。实测这会让
fidelity/前向回测的**全部 11 个窗口**被静默跳过。
"""
import datetime as dt
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))

import pytest  # noqa: E402

import vnpy_backtest as V  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后清空日历缓存, 避免相互污染."""
    V._CAL_CACHE.pop("cal", None)
    yield
    V._CAL_CACHE.pop("cal", None)


def test_cache_calendar_never_stores_empty():
    """空结果视为失败, 不得写入缓存(否则故障被永久固化)."""
    assert V._cache_calendar([]) == []
    assert V._CAL_CACHE.get("cal") is None, "空日历被缓存了 -> 故障会被固化"

    assert V._cache_calendar(["2024-01-02"]) == ["2024-01-02"]
    assert V._CAL_CACHE["cal"] == ["2024-01-02"]


def test_full_calendar_returns_non_empty_and_caches():
    """三个日历源(h5i_db / duckdb / data/trade_calendar.json)至少一个可用时,
    日历必须非空、升序且被缓存。

    [2026-09-19] 补 skip: 本用例的断言前提是"环境里存在日历源", 而 CI 里
      - `h5i_db` 不在依赖清单中;
      - `data/` 被 .gitignore 整目录忽略 ⇒ `data/trade_calendar.json` 结构上不可能存在。
    故无源时 skip(与本文件另两处 `pytest.skip("无日历")` 的既有约定一致),
    有源时仍严格断言。
    """
    cal = V._full_calendar()
    if not cal:
        pytest.skip("环境无任何日历源(h5i_db 未安装且 data/trade_calendar.json 不存在)")
    assert cal, "三个源都拿不到日历时该用例会失败, 说明环境缺数据源"
    assert cal == sorted(cal), "日历必须升序"
    assert V._CAL_CACHE.get("cal") == cal, "非空日历应被缓存"


def test_static_calendar_file_is_usable():
    """项目自带 data/trade_calendar.json 必须可解析且覆盖研究区间。

    [2026-09-19] 该文件位于被 .gitignore 忽略的 `data/` 下, 在 CI 中不存在,
    故文件缺失时 skip; 存在时仍严格校验覆盖区间。

    另注: 该文件契约是 `days` 键 + 'YYYYMMDD' 无分隔格式
    (见 vnpy_backtest._calendar_from_static_file)。scripts/export_trade_calendar.py
    曾只写 `trading_days` 键而覆盖此文件, 使回退链失效; 现该脚本写**双键**。
    """
    static_fp = os.path.join(V.DATA_DIR, "trade_calendar.json")
    if not os.path.exists(static_fp):
        pytest.skip(f"静态日历文件不存在(被 .gitignore 忽略的数据产物): {static_fp}")
    ds = V._calendar_from_static_file()
    assert ds, "静态日历不可用, 回退链失效"
    assert ds == sorted(ds)
    assert "2018-06-29" in ds and "2026-03-05" in ds, "研究区间未被静态日历覆盖"


def test_forward_window_days_returns_exact_length():
    cal = V._full_calendar()
    if not cal:
        pytest.skip("无日历")
    seg = V.forward_window_days(dt.date(2018, 6, 29), 120)
    assert len(seg) == 120, "前向窗口应恰好 120 个交易日"
    assert seg[0] >= "2018-06-29", "窗口不得包含决策日之前的交易日"
    assert seg[-1] == "2018-12-21", "窗口终点应与既有基线一致(回归守卫)"


def test_forward_window_days_empty_when_not_enough_future_data():
    """末端窗口数据不足时返回 [] (这是合法信号, 与'日历故障'不同)."""
    cal = V._full_calendar()
    if not cal:
        pytest.skip("无日历")
    last = dt.date.fromisoformat(cal[-1])
    assert V.forward_window_days(last, 120) == []


def test_bars_warn_once_only_emits_first(capsys):
    """取数失败告警每个来源只打一次, 避免逐标的刷屏."""
    V._BARS_WARNED.clear()
    V._warn_bars_once("duck", "第一个告警")
    V._warn_bars_once("duck", "重复告警不该出现")
    V._warn_bars_once("h5i", "另一个来源")
    err = capsys.readouterr().err
    assert err.count("第一个告警") == 1
    assert "重复告警不该出现" not in err
    assert err.count("另一个来源") == 1
    V._BARS_WARNED.clear()
