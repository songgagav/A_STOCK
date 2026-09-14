# -*- coding: utf-8 -*-
"""估值覆盖率哨兵守护 (src/sentinel_daemon.py) 的调度判据与导入安全性。

要点:
  - 到点+当日未执行 -> 跑; 未到点 / 当日已执行 -> 不跑
  - --trading-days-only 时非交易日不跑
  - import 本模块**不得**改变 CWD(否则会污染同进程内的其它测试)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))

AT = 18 * 60 + 30      # 18:30


def _r(now, mark=None, at=AT, trading_only=False, trading=True):
    import sentinel_daemon as sd
    return sd.should_run(now, mark or {}, at,
                         trading_only=trading_only, is_trading=trading)


def test_runs_after_target_when_not_yet_done():
    ok, why = _r(datetime(2026, 9, 14, 18, 30))
    assert ok, why


def test_runs_when_started_late_same_day():
    """进程 23:00 才起来也必须补跑当天(到点即跑, 不是固定窗口)."""
    assert _r(datetime(2026, 9, 14, 23, 0))[0]


def test_skips_before_target():
    ok, why = _r(datetime(2026, 9, 14, 18, 29))
    assert not ok
    assert "未到触发时间" in why


def test_skips_when_already_done_today():
    ok, why = _r(datetime(2026, 9, 14, 20, 0), {"date": "20260914"})
    assert not ok
    assert "今日已执行" in why


def test_runs_next_day_even_if_yesterday_marked():
    assert _r(datetime(2026, 9, 15, 18, 30), {"date": "20260914"})[0]


def test_trading_only_skips_weekend():
    ok, why = _r(datetime(2026, 9, 13, 19, 0), trading_only=True, trading=False)
    assert not ok
    assert "非交易日" in why


def test_trading_only_still_runs_on_trading_day():
    assert _r(datetime(2026, 9, 14, 19, 0), trading_only=True, trading=True)[0]


def test_parse_hhmm():
    import sentinel_daemon as sd
    assert sd._hhmm("18:30") == 1110
    assert sd._hhmm("00:05") == 5
    assert sd._hhmm("9") == 540


def test_module_import_does_not_chdir():
    """模块必须可被安全导入: import 不得改变 CWD."""
    cwd = os.getcwd()
    import importlib

    import sentinel_daemon
    importlib.reload(sentinel_daemon)
    assert os.getcwd() == cwd
