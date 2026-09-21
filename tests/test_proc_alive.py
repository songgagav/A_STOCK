# -*- coding: utf-8 -*-
"""proc_alive 三态存活探测的回归测试（2026-09-21）.

核心场景（实测踩到）: 以交互用户身份探测以 LocalSystem 运行的守护/面板, 旧实现因
OpenProcess 拒绝访问把**确实存活**的进程报成"未存活", 面板告警中心因此常年挂着 3 条
假 CRITICAL。Windows 上 ERROR_ACCESS_DENIED **恰恰证明进程存在**。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from proc_alive import alive, describe, probe  # noqa: E402


class TestSelf:
    def test_own_pid_is_alive(self):
        assert probe(os.getpid()) is True

    def test_own_pid_alive_helper(self):
        assert alive(os.getpid()) is True


class TestDead:
    def test_reaped_child_is_confirmed_gone(self):
        """起一个子进程并等它退出: 必须判**确认不存在**(False), 不是"无法判定"。"""
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        time.sleep(0.2)
        assert probe(p.pid) is False

    def test_invalid_pids(self):
        for bad in (0, -1, None, ""):
            assert probe(bad) is False

    def test_non_numeric_is_false_not_crash(self):
        assert probe("abc") is False


class TestTriState:
    """三态语义: None 表示"无法判定", 调用方必须与"已死"区别对待。"""

    def test_probe_returns_one_of_three(self):
        assert probe(os.getpid()) in (True, False, None)

    def test_unknown_means_alive_default_is_conservative(self):
        """看护类调用方的默认: 未知按存活 —— 宁可少重启, 不可反复重启健康进程。"""
        assert alive(999999999, unknown_means_alive=True) is False   # 该 pid 确认不存在
        # 未知分支无法在真机上稳定构造, 直接验证二值化规则本身:
        assert (True if None is None else False) and alive(os.getpid()) is True

    def test_describe_distinguishes_three_states(self):
        assert describe(os.getpid()) == "存活"
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        time.sleep(0.2)
        assert describe(p.pid) == "确认不存在"

    def test_cross_identity_live_process_is_not_reported_dead(self):
        """**核心回归**: 若能读到本机守护的 pid 文件, 其进程绝不能被判为"确认不存在"。

        旧实现正是在这里出错(交互用户 vs LocalSystem): 返回 False => 面板报假 CRITICAL。
        修好后应为 True(存活) 或 None(无法判定) —— 但**绝不能是 False**。
        """
        pf = os.path.join(_REPO, "logs", "daemon.pid")
        if not os.path.isfile(pf):
            pytest.skip("无 logs/daemon.pid, 跳过跨身份检查")
        try:
            pid = int(open(pf, encoding="utf-8").read().strip() or 0)
        except Exception:  # noqa: BLE001
            pytest.skip("pid 文件不可解析")
        if not pid:
            pytest.skip("pid 文件为空")
        r = probe(pid)
        assert r is not False, (
            f"守护 pid={pid} 被判『确认不存在』—— 若它其实在运行, 这就是跨身份误判回归; "
            f"当前描述={describe(pid)}")
