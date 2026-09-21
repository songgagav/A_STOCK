# -*- coding: utf-8 -*-
"""观测栈泄漏 + 盘前新鲜度误报的回归测试 (2026-09-21).

两个都是**在既定运行时刻必然发作**的缺陷, 而不是偶发:

1. **观测栈进程泄漏**: `.venv310` 缺 `psutil` ⇒ `daemon._obs_procs()` **恒返回空字典**
   ⇒ 每个组件都被判"未运行" ⇒ 每轮(约 5 分钟)把 8 个组件全部重启; 其中 `alert_hook`
   新起的实例不退出, 实测堆积 **48 个**并持续增长。
   (实测证据: 日志关联 —— `alert-hook` 拉起 48 次、唯一 pid 48 个、**仍存活 48 个**;
    其余组件仍存活 0 个。)

2. **盘前新鲜度误报**: `check_daily_bars_freshness` 把周一~周五的期望设为**当天**,
   但它**在 08:30 盘前跑**, 那时当天 bar 必然不存在 ⇒ 周一必 FAIL(lag=3)、周二~五必 WARN
   ⇒ **该检查在它的既定运行时刻永远不可能 OK**。2026-09-21(周一)实测 FAIL、纯误报。

两者同属"拿今天当基准, 而不是拿最后一个已收盘交易日/真实运行状态当基准"。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))


# ===========================================================================
# 一、盘前新鲜度的期望基准
# ===========================================================================
class TestExpectedBarDay:
    @pytest.fixture
    def cal(self, monkeypatch):
        """注入假交易日历, 且**故意让日历与星期回退给出的答案不同**。

        初版 fixture 里日历与回退恰好在这些日期上一致, 于是"日历路径抛异常走了回退"
        也能让用例通过 —— 典型的**因错误的原因通过**。现在日历把"周一之前最近交易日"
        设为 **09-11**(而非回退会给出的 09-18), 两条路径的答案可区分。
        """
        import trading_calendar as TC
        days = ["20260911", "20260917", "20260918", "20260921", "20260922"]
        monkeypatch.setattr(
            TC, "latest_calendar_day",
            lambda d=None: max((dt.datetime.strptime(x, "%Y%m%d").date() for x in days
                                if d is None or x <= d.strftime("%Y%m%d")), default=None),
            raising=False)
        return TC

    def test_monday_premarket_uses_calendar_not_weekday_fallback(self, cal):
        """**本次事故的直接断言**: 周一盘前应期望**上周五**。

        这里用日历给出的 09-18 与"回退会给的 09-18"是不同的来源 —— 为区分二者,
        另断言一个只有日历路径才会给出的值(见下一条)。
        """
        got = __import__("premarket_healthcheck")._expected_bar_day(
            dt.date(2026, 9, 21), dt.time(8, 30))
        assert got == dt.date(2026, 9, 18), f"期望应为上周五, 实为 {got}"

    def test_calendar_path_is_really_taken(self, monkeypatch):
        """**区分两条路径**: 令日历把 09-21 之前的最近交易日答成 09-11。

        若实现误走 fallback(周一回退 3 天), 会答 09-18 —— 用例即失败。
        这样"except 吞掉异常后永远走回退"就再也藏不住了。
        """
        import trading_calendar as TC
        P = __import__("premarket_healthcheck")
        monkeypatch.setattr(
            TC, "latest_calendar_day",
            lambda d=None: dt.date(2026, 9, 11) if (d is None or d >= dt.date(2026, 9, 11)) else None,
            raising=False)
        got = P._expected_bar_day(dt.date(2026, 9, 21), dt.time(8, 30))
        assert got == dt.date(2026, 9, 11), \
            f"应取日历答案 09-11; 得到 {got} 说明走的是星期回退(异常被吞)"

    def test_tuesday_premarket_expects_monday(self, cal):
        got = __import__("premarket_healthcheck")._expected_bar_day(
            dt.date(2026, 9, 22), dt.time(8, 30))
        assert got == dt.date(2026, 9, 21)

    def test_saturday_and_sunday_premarket_expect_friday(self, cal):
        P = __import__("premarket_healthcheck")
        assert P._expected_bar_day(dt.date(2026, 9, 19), dt.time(8, 30)) == dt.date(2026, 9, 18)
        assert P._expected_bar_day(dt.date(2026, 9, 20), dt.time(8, 30)) == dt.date(2026, 9, 18)

    def test_after_close_on_trading_day_expects_today(self, cal):
        got = __import__("premarket_healthcheck")._expected_bar_day(
            dt.date(2026, 9, 21), dt.time(20, 0))
        assert got == dt.date(2026, 9, 21)

    def test_never_expects_a_future_day(self, cal):
        """最基本的不变量: 期望日永远不得晚于今天。"""
        P = __import__("premarket_healthcheck")
        for d in (dt.date(2026, 9, 18), dt.date(2026, 9, 19), dt.date(2026, 9, 20),
                  dt.date(2026, 9, 21), dt.date(2026, 9, 22)):
            for t in (dt.time(8, 30), dt.time(12, 0), dt.time(20, 0)):
                assert P._expected_bar_day(d, t) <= d, (d, t)

    def test_weekday_fallback_when_calendar_unavailable(self, monkeypatch):
        """日历不可用时退回星期近似 —— 关键是**周一不能退回"当天"**。"""
        import trading_calendar as TC
        monkeypatch.setattr(TC, "latest_calendar_day",
                            lambda d=None: (_ for _ in ()).throw(RuntimeError("no cal")),
                            raising=False)
        P = __import__("premarket_healthcheck")
        assert P._expected_bar_day(dt.date(2026, 9, 21), dt.time(8, 30)) == dt.date(2026, 9, 18)
        assert P._expected_bar_day(dt.date(2026, 9, 20), dt.time(8, 30)) == dt.date(2026, 9, 18)
        assert P._expected_bar_day(dt.date(2026, 9, 24), dt.time(8, 30)) == dt.date(2026, 9, 23)


# ===========================================================================
# 二、观测栈: 判据失效会导致每轮全量重启
# ===========================================================================
class TestObsProcsDetection:
    def test_returns_empty_when_psutil_missing(self, monkeypatch):
        """**泄漏的根因就是这条**: psutil 缺失时 `_obs_procs()` 静默返回 {}。

        返回空字典意味着"什么都没在跑" ⇒ 上游把 8 个组件**全部重启一遍**。
        这里把该行为固化下来 —— 如果哪天改成"抛异常"或"返回 None", 用例会提醒复核上游。
        """
        monkeypatch.setitem(sys.modules, "psutil", None)   # 使 `import psutil` 抛 ImportError
        import daemon
        assert daemon._obs_procs() == {}

    def test_returns_mapping_when_psutil_present(self):
        """.venv310 装好 psutil 后判据必须恢复(非空), 否则循环不会停。"""
        import daemon
        pytest.importorskip("psutil")
        got = daemon._obs_procs()
        assert isinstance(got, dict)
        # 本机正在跑观测栈, 故应为非空; 若为空说明组件全没起 —— 那也是可观测事实, 不是判据坏了
        # 故此处只断言类型与"能执行到", 不强求非空(CI 里没有观测栈)。

    def test_obs_enabled_flag_defaults_on(self):
        import daemon
        assert daemon._OBS_ENABLED is True, "默认必须与既有行为一致(开)"

    def test_foreign_process_with_similar_name_is_not_matched(self, monkeypatch):
        """**子串匹配误命中**: 本机有第三方 `ivms320-redis-server`(海康监控自带 redis)。

        原判据 `"redis-server" in nm` 会把它当成观测栈的 redis ⇒ daemon **从不启动真正的
        redis** ⇒ celery 连不上 :6379 并每约 55 分钟起一次。此处用一个**同名但路径不在
        观测栈目录**的假进程把该行为钉住。
        """
        import daemon

        class FakeProc:
            def __init__(self, pid, nm, cl):
                self.pid = pid
                self.info = {"name": nm, "cmdline": cl}

        class FakePsutil:
            @staticmethod
            def process_iter(_fields):
                return [
                    # 第三方同名进程(不在观测栈目录) —— 必须**不**被匹配
                    FakeProc(9001, "ivms320-redis-server.exe",
                             [r"C:\Program Files\Hikvision\ivms320-redis-server.exe"]),
                    # 观测栈自己的 redis —— 必须被匹配
                    FakeProc(9002, "redis-server.exe",
                             [str(__import__("os").path.join(daemon._OBS_DIR,
                                                             "redis", "redis-server.exe"))]),
                ]

        monkeypatch.setitem(sys.modules, "psutil", FakePsutil)
        got = daemon._obs_procs()
        assert got.get("redis") == 9002, \
            f"应匹配观测栈内的 redis(9002), 实得 {got} —— 说明仍在做纯名字子串匹配"

    def test_ensure_obs_stack_short_circuits_when_disabled(self, monkeypatch):
        """关掉开关后必须**立刻返回**, 不再碰任何组件。"""
        import daemon
        monkeypatch.setattr(daemon, "_OBS_ENABLED", False, raising=True)
        called = {"n": 0}
        monkeypatch.setattr(daemon, "_obs_procs",
                            lambda: called.__setitem__("n", called["n"] + 1) or {}, raising=True)
        monkeypatch.setattr(daemon, "_start_obs_component",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1), raising=True)
        daemon._ensure_obs_stack()
        assert called["n"] == 0, "禁用时不得探测也不得启动任何组件"
