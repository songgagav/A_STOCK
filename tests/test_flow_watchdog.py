# -*- coding: utf-8 -*-
"""flow_watchdog 的回归测试（路线图 #4）.

阈值证据（本仓实测）:
  引擎主循环 = tick(); sleep(15)  => 合法写间隔 = 处理耗时 + 15s
  健康:     处理 p50 ≈ 7ms  (2026-09-21 13:08)  => 间隔 ≈ 15s
  实测退化: 处理 p99 = 15.6s (2026-09-21 15:02) => 间隔 ≈ 30.6s
  故取 120s(= 退化间隔 3.9 倍), 而**不**照搬 heartbeat.py 的 3× 标称(45s) —— 见模块 docstring。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from flow_watchdog import (  # noqa: E402
    STALL_AFTER_S, evaluate, gather, state_path,
)

NOW = datetime(2026, 9, 21, 10, 30, 0)


def _ts(seconds_ago: float) -> str:
    from datetime import timedelta
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _s(**kw):
    """默认：盘中、3 秒前刚写过、进程活着、无行情源错误。"""
    base = {"in_session": True, "updated": _ts(3), "tick": 100, "prev_tick": 99,
            "pid_alive": True, "engine_pid": 4242, "feed_error": "", "live_source": "akshare_spot"}
    base.update(kw)
    return base


class TestIdle:
    """非盘中**绝不告警** —— 夜间误报是『狼来了』的典型来源。"""

    def test_night_is_idle_not_stalled(self):
        """实测场景: 2026-09-21 23:45, live_state 停在 15:02(8.7 小时前), 引擎已退出。"""
        r = evaluate(_s(in_session=False, updated=_ts(8 * 3600), pid_alive=False,
                        live_source="price_hold"), now=NOW)
        assert r["level"] == "OK"
        assert r["cause"] == "idle"
        assert r["age_s"] == 8 * 3600  # 年龄照样如实报出, 只是不据此告警

    def test_idle_wins_over_dead_process(self):
        """收盘后引擎退出是**正常**的, 不得因 pid 不在就报 CRITICAL。"""
        assert evaluate(_s(in_session=False, pid_alive=False), now=NOW)["cause"] == "idle"


class TestHealthy:
    def test_flowing_data_is_ok(self):
        r = evaluate(_s(), now=NOW)
        assert (r["level"], r["cause"]) == ("OK", "ok")
        assert "流动" in r["reason"]

    def test_unknown_pid_alive_is_not_treated_as_dead(self):
        """pid_alive=None 是『未知』, 不得当 False 报进程不在(受限环境/非 Windows)。"""
        r = evaluate(_s(pid_alive=None), now=NOW)
        assert r["cause"] == "ok"

    def test_degraded_but_legitimate_gap_is_not_stalled(self):
        """**关键边界**: 退化盘里合法间隔可达 30.6s(15s 间隔 + 15.6s 处理), 不得误报。"""
        r = evaluate(_s(updated=_ts(31)), now=NOW)
        assert r["level"] == "OK", f"31s 是实测退化下的合法间隔, 误报会毁掉告警可信度: {r}"


class TestStalled:
    def test_deadlock_detected(self):
        r = evaluate(_s(updated=_ts(121), tick=1152, prev_tick=1152), now=NOW)
        assert (r["level"], r["cause"]) == ("CRITICAL", "stalled")
        assert "死锁" in r["reason"]

    def test_tick_counter_gives_hard_evidence(self):
        """计数器停在同一个值 = 比时间戳更硬的死锁证据, 必须出现在措辞里。"""
        r = evaluate(_s(updated=_ts(300), tick=1152, prev_tick=1152), now=NOW)
        assert r["tick_stuck"] is True
        assert "tick 停在 1152" in r["reason"]

    def test_advancing_tick_omits_stuck_phrase(self):
        r = evaluate(_s(updated=_ts(300), tick=1153, prev_tick=1152), now=NOW)
        assert r["tick_stuck"] is False
        assert "未推进" not in r["reason"]

    def test_missing_prev_tick_does_not_claim_stuck(self):
        """首次采样没有前值 => 不得声称『未推进』(不知道的事不说)。"""
        r = evaluate(_s(updated=_ts(300), tick=1152, prev_tick=None), now=NOW)
        assert r["tick_stuck"] is False

    def test_boundary_exactly_at_threshold_is_ok(self):
        assert evaluate(_s(updated=_ts(STALL_AFTER_S)), now=NOW)["cause"] == "ok"

    def test_boundary_just_over_threshold_is_stalled(self):
        assert evaluate(_s(updated=_ts(STALL_AFTER_S + 0.5)), now=NOW)["cause"] == "stalled"


class TestAttribution:
    """**归因纪律**: 行情源坏了 ≠ 主循环死锁 —— 两者运维动作不同。"""

    def test_feed_error_is_not_blamed_on_deadlock(self):
        r = evaluate(_s(updated=_ts(600), feed_error="akshare 连续 3 次失败"), now=NOW)
        assert r["cause"] == "feed_stale"
        assert "不是主循环死锁" in r["reason"]
        assert "akshare 连续 3 次失败" in r["reason"]

    def test_static_price_fallback_is_feed_stale(self):
        """盘中退化成静态价 => 数据没在流动, 但循环还在跑 => 归因到行情源。"""
        r = evaluate(_s(updated=_ts(600), live_source="price_hold"), now=NOW)
        assert r["cause"] == "feed_stale"
        assert "price_hold" in r["reason"]

    def test_pool_reference_is_also_feed_stale(self):
        r = evaluate(_s(updated=_ts(600), live_source="duckdb_reference_held"), now=NOW)
        assert r["cause"] == "feed_stale"

    def test_dead_process_is_its_own_cause(self):
        r = evaluate(_s(pid_alive=False, updated=_ts(600)), now=NOW)
        assert (r["level"], r["cause"]) == ("CRITICAL", "dead_process")
        assert "守护 pid 监护域" in r["reason"]

    def test_unparseable_timestamp_is_unknown_not_stalled(self):
        r = evaluate(_s(updated="??"), now=NOW)
        assert (r["level"], r["cause"]) == ("WARN", "unknown")
        assert r["age_s"] is None

    def test_missing_timestamp_is_unknown(self):
        assert evaluate(_s(updated=None), now=NOW)["cause"] == "unknown"


class TestGather:
    """gather 触碰文件系统, 用 tmp 路径注入; 逻辑仍在纯函数里。"""

    def _write(self, tmp_path, **lv):
        p = tmp_path / "live_state.json"
        p.write_text(json.dumps(lv), encoding="utf-8")
        return str(p)

    def test_roundtrip_persists_tick_for_next_run(self, tmp_path):
        lv = self._write(tmp_path, in_session=True, updated=_ts(2), tick=1152,
                         live_source="akshare_spot", feed_error="")
        st = str(tmp_path / "wd.json")
        r1 = gather(live_state=lv, pidfile=str(tmp_path / "none.pid"), state=st,
                    pid_alive_fn=lambda pid: True, now=NOW)
        assert r1["observed"]["prev_tick"] is None      # 首次无前值
        assert r1["cause"] == "ok"
        # 第二次: 引擎冻结(tick 不再推进)且已超阈值 => 死锁 + 硬证据
        r2 = gather(live_state=lv, pidfile=str(tmp_path / "none.pid"), state=st,
                    pid_alive_fn=lambda pid: True, now=NOW.replace(minute=35))
        assert r2["observed"]["prev_tick"] == 1152
        assert (r2["cause"], r2["tick_stuck"]) == ("stalled", True)
        assert "tick 停在 1152" in r2["reason"]

    def test_pid_from_pidfile_is_passed_to_probe(self, tmp_path):
        lv = self._write(tmp_path, in_session=True, updated=_ts(2), tick=7)
        pf = tmp_path / "engine.pid"
        pf.write_text("4242", encoding="utf-8")
        seen = []
        gather(live_state=lv, pidfile=str(pf), state=str(tmp_path / "wd.json"),
               pid_alive_fn=lambda pid: seen.append(pid) or True, now=NOW)
        assert seen == [4242]

    def test_missing_live_state_is_not_a_crash(self, tmp_path):
        r = gather(live_state=str(tmp_path / "nope.json"), pidfile=str(tmp_path / "nope.pid"),
                   state=str(tmp_path / "wd.json"), pid_alive_fn=lambda pid: False, now=NOW)
        assert r["cause"] == "idle"           # 读不到 => in_session 视为 False => 不判定
        assert r["level"] == "OK"

    def test_corrupt_live_state_is_not_a_crash(self, tmp_path):
        p = tmp_path / "live_state.json"
        p.write_text("{半截", encoding="utf-8")
        r = gather(live_state=str(p), pidfile=str(tmp_path / "nope.pid"),
                   state=str(tmp_path / "wd.json"), pid_alive_fn=lambda pid: None, now=NOW)
        assert r["level"] in ("OK", "WARN")

    def test_state_path_default_is_under_data(self):
        assert state_path().replace("\\", "/").endswith("data/flow_watchdog_state.json")
