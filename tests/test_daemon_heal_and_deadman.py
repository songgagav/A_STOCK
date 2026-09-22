# -*- coding: utf-8 -*-
"""守护进程的两条**静默失效**回归 (2026-09-22 批次)。

两条都源自当天的实测空档: `data/state.json` 与 `data/live_state.json` 的 mtime
双双停在 **12:29:35**, 而当时已 20:00 —— **7.5 小时没有撮合、没有风控**。
机器 `LastBootUpTime = 2026-09-22 16:08:42`(计划外重启), NSSM 开机拉起了两个服务,
所以"它怎么会停"有答案; 真正要修的是"**它停了 4.6 小时没人知道**"。
"""
from __future__ import annotations

import inspect
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))


class TestEngineHealDoesNotDependOnVolatileMemory:
    """引擎自愈**不得** gate 在内存态 `running_day` 上。"""

    def test_heal_branch_exists_outside_the_running_day_guard(self):
        """自愈必须有一条**不依赖 `running_day == day_str`** 的路径。

        原实现把崩溃自愈整段放在 `elif _state["running_day"] == day_str:` 里面,
        而 `running_day` 只在 `_start_engine()` 里被赋值 ⇒ **daemon 自己一重启,
        这个记忆就没了**, 自愈分支永不进入; 更糟的是 `_log` 就在被跳过的分支里,
        **连一行日志都不会产生**。

        当天实测: 机器 16:08:42 重启 -> daemon 16:09:07 起(恢复的 running_day=null),
        而引擎最后一次写 12:29:35 ⇒ 12:29:35~16:08 这段 daemon 活着、引擎已死,
        **零次自愈尝试、零条日志**, 只能靠比对 mtime 考古才发现。
        """
        import daemon as D
        src = inspect.getsource(D.run_loop)
        # 必须存在一条注释说明这条教训(锁住"为什么", 而不只是"有什么")
        assert "自愈不能依赖内存态" in src or "易失" in src, \
            "缺少对『自愈不得依赖易失内存态』的说明"
        # 且必须有一条以 engine_pid 直接探活的兜底分支
        assert "engine_pid" in src
        i = src.find("elif _state[\"running_day\"] != day_str")
        assert i > 0, "缺少不依赖 running_day 的自愈分支"

    def test_heal_path_always_logs(self):
        """**引擎不在跑这件事必须留一句日志**, 不管走哪条分支。

        原实现的失败之所以难查, 就是因为"跳过"与"检查过且正常"在日志上无法区分。
        """
        import daemon as D
        src = inspect.getsource(D.run_loop)
        i = src.find("elif _state[\"running_day\"] != day_str")
        block = src[i:i + 2200]
        assert "_log(" in block, "自愈分支里没有任何日志 —— 失效又会变静默"
        assert "_proc_alive(" in block

    def test_heal_path_has_the_time_gate(self):
        """**自愈必须带时间闸门** —— 否则会变成重启风暴。

        这是首版修复**自己踩的坑**, 留档: 我加的那条自愈路径漏了 `cur_time < 15:03`,
        于是收盘后每次循环都 `_start_engine`: 引擎起来一看已过 15:05 立刻自行退出
        (`已收盘或非交易日, 引擎自动停止`), 30 秒后守护又拉一次 ——
        实测 20:12~20:14 每 30 秒生成一个新 pid。

        教训: 加一条自愈路径时, 必须把**原路径的所有前置条件**一起复制过来。
        时间闸门不是装饰, 它和存活判断同等重要 —— 引擎"本来就该停"与"意外死了"
        必须分开, 否则修好一个静默失效的同时制造一个响亮的故障。
        """
        import daemon as D
        src = inspect.getsource(D.run_loop)
        i = src.find("elif _state[\"running_day\"] != day_str")
        assert i > 0
        # 自愈块必须在遇到收盘闸门时**放弃重启**并转入 engine_done
        block = src[i:i + 2200]
        assert "dtime(15, 3)" in block, "自愈分支缺少收盘时间闸门 => 收盘后会无限重启"
        assert "engine_done" in block, "过了收盘窗口应标记完成, 而不是继续拉引擎"


class TestDeadmanIsActuallyConsumed:
    """死手开关必须**被求值**, 而不只是被喂 tick。"""

    def test_health_state_consumes_the_verdict(self):
        """`deadman_switch.verdict()` 的结论必须进入健康快照。

        2026-09-22 实测: 全仓检索 `deadman_switch.verdict` / `_DMS.verdict` /
        `from deadman_switch import` **零命中**; 唯一生产调用点是 daemon 的
        `_DMS.beat(...)` —— 也就是**只喂 tick、从不问结论**。
        于是本仓唯一一个"失联本身即是证据"的机制(其它监控都要求监测者自己还活着)
        **恰恰是唯一没接线的那个**。当天 daemon 消失 4.6 小时, 全系统零告警;
        我手工跑一次 `verdict()` 立刻得到 OVERDUE。
        """
        import health_state as H
        assert "deadman" in inspect.getsource(H.gather)
        assert "deadman" in inspect.getsource(H.assemble)

    def test_overdue_makes_it_degraded(self):
        import health_state as H
        base = {"tick_ms": {"p50": 5, "p95": 9}, "freshness_ok": True, "l3_today": 0}
        r = H.assemble({**base, "deadman": {"level": "OVERDUE",
                                            "reasons": ["obs_stack: 已 66866s 无 tick"]}})
        assert r["state"] == "DEGRADED"
        assert any("死手开关 OVERDUE" in x for x in r["reasons"])

    def test_unknown_is_degraded_not_normal(self):
        """**账本为空 = 这套监控从未生效, 不是健康**。

        "没有告警"与"没有在监控"必须可区分 —— 这正是该模块 docstring 的立场,
        接线时必须把它带进总状态, 否则首次上线的静默期会被读成"一切正常"。
        """
        import health_state as H
        base = {"tick_ms": {"p50": 5, "p95": 9}, "freshness_ok": True, "l3_today": 0}
        r = H.assemble({**base, "deadman": {"level": "UNKNOWN"}})
        assert r["state"] == "DEGRADED"
        assert any("UNKNOWN" in x for x in r["reasons"])

    def test_absent_key_is_backward_compatible(self):
        """**"没这一项" 与 "有这一项但取不到" 必须分开**。

        前者是向后兼容: 老快照与纯函数调用本就不带这个键, 不该因此被判 DEGRADED。
        后者是采集层异常, 必须说出来。判据取 `"deadman" in snap` 而非
        `dm is None` —— 因为 `gather()` 采集失败时恰恰就是把 `None` 写进去。
        (首版没区分, 一次性弄红 13 个 health_state 用例。)
        """
        import health_state as H
        base = {"tick_ms": {"p50": 5, "p95": 9}, "freshness_ok": True, "l3_today": 0}
        assert H.assemble(dict(base))["state"] == "NORMAL"
        r = H.assemble({**base, "deadman": None})
        assert r["state"] == "DEGRADED"
        assert any("取不到" in x for x in r["reasons"])
