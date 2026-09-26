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
from doc_section import code_block_bounds  # noqa: E402

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
        block = src[i:code_block_bounds(src, i)]
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
        block = src[i:code_block_bounds(src, i)]
        assert "dtime(15, 3)" in block, "自愈分支缺少收盘时间闸门 => 收盘后会无限重启"
        assert "engine_done" in block, "过了收盘窗口应标记完成, 而不是继续拉引擎"


class TestObsProcDetection:
    """观测栈组件识别: **不能靠子串匹配**。

    2026-09-22 实测 `_obs_procs()` 把三类完全无关的进程认成了组件:
      · `node.exe`(DSH 会话本体, 命令行含本仓路径);
      · `powershell.exe -Command ...`(命令行含被执行的脚本正文);
      · 一个临时的 `python -c "..."` 诊断脚本(正文里提到了 metrics_server)。
    后果: `metrics in run` 恒为真 ⇒ 9101 上的 metrics_server 死了**永远不会被拉起**,
    而告警链(Prometheus -> Alertmanager -> alert_hook)是夜里唯一会叫的人。

    这与 2026-09-21 的 `ivms320-redis-server` 是同一条通病: **子串匹配无法区分
    "同名/提及" 与 "就是它"**。上次只补了"要求出现本仓目录", 不够。
    """

    def _fake(self, monkeypatch, procs):
        import sys
        import types
        import daemon as D

        class _P:
            def __init__(self, pid, info):
                self.pid = pid
                self.info = info

        fake = types.SimpleNamespace(
            process_iter=lambda attrs=None: iter([_P(pid, i) for pid, i in procs]))
        monkeypatch.setitem(sys.modules, "psutil", fake)
        return D._obs_procs()

    def test_a_mentioning_process_is_not_a_component(self, monkeypatch):
        """**核心**: 命令行里"提到"脚本路径 ≠ 那就是本组件。"""
        import os
        import daemon as D
        repo = D._BASE
        procs = [
            # 真组件: argv 里就是本仓脚本
            (111, {"name": "python.exe",
                   "cmdline": [r"C:\py\python.exe",
                               os.path.join(repo, "src", "metrics_server.py"),
                               "--port", "9101"]}),
            # 假的: node 会话, 命令行只是**包含**本仓路径
            (222, {"name": "node.exe",
                   "cmdline": ["node.exe", os.path.join(repo, "src", "metrics_server.py")]}),
            # 假的: powershell -Command 正文里提到
            (333, {"name": "powershell.exe",
                   "cmdline": ["powershell.exe", "-Command",
                               "Get-Content " + os.path.join(repo, "src", "metrics_server.py")]}),
        ]
        got = self._fake(monkeypatch, procs)
        assert got.get("metrics") == 111, f"应只认 python 真组件, 实得 {got}"

    def test_missing_component_is_reported_missing(self, monkeypatch):
        """**没有真组件时必须报"缺"** —— 否则看护永不拉起(今天就是这个后果)。"""
        import os
        import daemon as D
        repo = D._BASE
        procs = [
            (222, {"name": "node.exe",
                   "cmdline": ["node.exe", os.path.join(repo, "src", "metrics_server.py")]}),
            (333, {"name": "powershell.exe",
                   "cmdline": ["powershell.exe", "-Command", "echo metrics_server.py"]}),
        ]
        got = self._fake(monkeypatch, procs)
        assert "metrics" not in got, f"无真组件却报存在 => 看护永不拉起: {got}"

    def test_self_diagnostic_script_does_not_match(self, monkeypatch):
        """诊断脚本(命令行正文里含组件名)**不得**把自己算成组件。

        这在排查时最容易发生 —— 你一边跑诊断一边看判定, 诊断本身污染了判定。
        """
        import os
        import daemon as D
        repo = D._BASE
        body = "import os; print(os.path.join(r'%s', 'src', 'metrics_server.py'))" % repo
        procs = [(444, {"name": "python.exe", "cmdline": ["python.exe", "-c", body]})]
        got = self._fake(monkeypatch, procs)
        assert "metrics" not in got, f"诊断脚本污染了判定: {got}"

    def test_real_shape_is_still_detected(self, monkeypatch):
        """别把真组件也一起修掉了 —— 正常形态必须仍能识别。"""
        import os
        import daemon as D
        repo = D._BASE
        procs = [
            (1, {"name": "python.exe", "cmdline": [
                os.path.join(repo, ".venv310", "Scripts", "python.exe"),
                os.path.join(repo, "src", "metrics_server.py"), "--port", "9101"]}),
            (2, {"name": "python.exe", "cmdline": [
                os.path.join(repo, ".venv310", "Scripts", "python.exe"),
                os.path.join(repo, "ops", "alert_hook.py"), "--port", "9111"]}),
            (3, {"name": "python.exe", "cmdline": [
                os.path.join(repo, ".venv310", "Scripts", "python.exe"),
                "-m", "celery", "-A", "src.tasks_db", "worker", "--pool=solo"]}),
            (4, {"name": "python.exe", "cmdline": [
                os.path.join(repo, ".venv310", "Scripts", "python.exe"),
                "-m", "celery", "-A", "src.tasks_db", "flower", "--port=5555"]}),
        ]
        got = self._fake(monkeypatch, procs)
        assert got.get("metrics") == 1
        assert got.get("hook") == 2
        assert got.get("celery") == 3
        assert got.get("flower") == 4


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
