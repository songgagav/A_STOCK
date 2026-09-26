# -*- coding: utf-8 -*-
"""行情引擎(stockdb.exe)运行期巡检的回归测试 (P0-DATASRC-STOCKDB)。

背景(2026-09-22 真实事故): `ops/start_daemon.ps1` 早就有**启动**闸门(探不到引擎就
exit 4), 所以"启动时"挡得住。当天的问题是**没有任何东西在运行期发现引擎掉了** ——
11:31 引擎失联后, 守护即使活着也只会继续看护一个注定拿不到数据的引擎, 直到收盘
才发现"今天没有新数据", 而这与"今天是节假日"**无法区分**。

本文件锁住三件事:
  1. 探活判据本身(端口开/关 -> listening 真假, 且不抛);
  2. 巡检的**降噪**行为(结论不变时不重复刷屏) —— 否则日志被淹没, 告警失效;
  3. 巡检**绝不擅自重启**引擎(与 `_check_flow` 同纪律)。
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

from doc_section import code_block_bounds  # noqa: E402  按**结构**定界, 取代 src[i:i+N] 的魔数窗口

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import daemon as D  # noqa: E402


class TestProbeStockdbPort:
    def test_returns_contract_keys(self):
        r = D._probe_stockdb_port(timeout=0.2)
        assert set(r) >= {"listening", "endpoint", "error"}
        assert isinstance(r["listening"], bool)

    def test_endpoint_is_the_sdk_port(self):
        """端点必须是 127.0.0.1:7899 —— 与 stockdb.conf / 探针 / 启动闸门同一端口。"""
        assert D._probe_stockdb_port(timeout=0.2)["endpoint"] == "127.0.0.1:7899"

    def test_listening_true_when_someone_listens(self):
        """起一个本地监听 -> 必须判为在听。"""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 7899))
        srv.listen(1)
        try:
            # 直接调被测函数会连到**真的 7899**; 这里改端口不现实(它是常量),
            # 故改为验证 socket 语义: 若 7899 已被占用则本机必然在听。
            r = D._probe_stockdb_port(timeout=0.5)
            assert r["listening"] is True
        finally:
            srv.close()

    def test_listening_false_and_no_raise_when_refused(self, monkeypatch):
        def boom(addr, timeout=None):
            raise ConnectionRefusedError("模拟引擎掉线")
        monkeypatch.setattr(socket, "create_connection", boom)
        r = D._probe_stockdb_port(timeout=0.2)
        assert r["listening"] is False
        assert "ConnectionRefusedError" in (r["error"] or "")

    def test_never_raises_on_odd_errors(self, monkeypatch):
        def boom(addr, timeout=None):
            raise OSError("whatever")
        monkeypatch.setattr(socket, "create_connection", boom)
        r = D._probe_stockdb_port(timeout=0.2)
        assert r["listening"] is False and r["error"]


class TestEnsureStockdb:
    def _logs(self, monkeypatch):
        lines: list[str] = []
        monkeypatch.setattr(D, "_log", lambda txt, sub=None: lines.append(str(txt)))
        # 每轮独立: 清掉跨用例记忆, 否则降噪会吃掉本用例该看到的告警
        D._STOCKDB_LAST.clear()
        return lines

    def test_silent_when_listening(self, monkeypatch):
        """引擎正常时**不得**产生任何日志 —— 否则每 5 分钟一行, 真告警会被淹没。"""
        lines = self._logs(monkeypatch)
        monkeypatch.setattr(D, "_probe_stockdb_port",
                            lambda timeout=1.5: {"listening": True,
                                                 "endpoint": "127.0.0.1:7899",
                                                 "error": None})
        D._ensure_stockdb()
        assert lines == []
        assert D._STOCKDB_LAST.get("level") == "OK"

    def test_critical_and_actionable_when_down(self, monkeypatch):
        lines = self._logs(monkeypatch)
        monkeypatch.setattr(D, "_probe_stockdb_port",
                            lambda timeout=1.5: {"listening": False,
                                                 "endpoint": "127.0.0.1:7899",
                                                 "error": "ConnectionRefusedError: x"})
        D._ensure_stockdb()
        joined = "\n".join(lines)
        assert "[CRITICAL]" in joined
        assert "不在监听" in joined
        # 后果与处置都要写清楚 —— 只说"引擎挂了"没法行动
        assert "无法区分" in joined          # 后果: 与"今天没数据"无法区分
        assert "AStockStockdb" in joined     # 处置: 检查哪个服务
        assert "log.txt" in joined           # 更深一层: 引擎自身日志

    def test_does_not_repeat_every_call(self, monkeypatch):
        """**降噪**: 结论未变且未到 5 分钟, 不重复记录(否则刷屏使告警失效)。"""
        lines = self._logs(monkeypatch)
        monkeypatch.setattr(D, "_probe_stockdb_port",
                            lambda timeout=1.5: {"listening": False,
                                                 "endpoint": "127.0.0.1:7899",
                                                 "error": "x"})
        D._ensure_stockdb()
        first = len(lines)
        assert first > 0
        D._ensure_stockdb()
        D._ensure_stockdb()
        assert len(lines) == first, "结论未变时重复刷屏"

    def test_reports_recovery(self, monkeypatch):
        """从掉线恢复时要记一条 —— 否则运维无法确认"已经好了"。"""
        lines = self._logs(monkeypatch)
        state = {"up": False}
        monkeypatch.setattr(D, "_probe_stockdb_port",
                            lambda timeout=1.5: {"listening": state["up"],
                                                 "endpoint": "127.0.0.1:7899",
                                                 "error": None if state["up"] else "x"})
        D._ensure_stockdb()                       # 掉线 -> CRITICAL
        assert any("[CRITICAL]" in x for x in lines)
        n = len(lines)
        state["up"] = True
        D._ensure_stockdb()                       # 恢复 -> 一条恢复日志
        assert len(lines) > n
        assert any("已恢复监听" in x for x in lines)

    def test_never_restarts_the_engine(self, monkeypatch):
        """**核心纪律**: 巡检只报告, 绝不擅自拉起(与 `_check_flow` 同)。

        理由更强: 引擎掉线通常意味着 leveldb 坏了或更新器没跑, 重启只会得到
        一台能连上但取不到数的引擎(P1-ENGINEDEP)。机器重启后的自动拉起由 SCM
        负责(AStockStockdb 是 SERVICE_AUTO_START + AppExit Restart)。
        """
        self._logs(monkeypatch)
        monkeypatch.setattr(D, "_probe_stockdb_port",
                            lambda timeout=1.5: {"listening": False,
                                                 "endpoint": "127.0.0.1:7899",
                                                 "error": "x"})
        called = {"n": 0}

        def spy(*a, **k):
            called["n"] += 1
            raise AssertionError("巡检不得拉起引擎")

        for name in ("subprocess", "Popen"):
            if hasattr(D, name):
                monkeypatch.setattr(getattr(D, name), "Popen", spy, raising=False)
        D._ensure_stockdb()
        assert called["n"] == 0

    def test_probe_exception_does_not_break_loop(self, monkeypatch):
        lines = self._logs(monkeypatch)

        def boom(timeout=1.5):
            raise RuntimeError("探针自身炸了")
        monkeypatch.setattr(D, "_probe_stockdb_port", boom)
        D._ensure_stockdb()          # 不得抛出
        assert any("巡检异常" in x for x in lines)


class TestPatrolIsWired:
    """接线存在性: 光有函数不被调用等于没有。"""

    def test_called_in_the_periodic_patrol_block(self):
        import inspect
        src = inspect.getsource(D)
        i = src.find("dash_tick >= 20")
        assert i > 0, "找不到 5 分钟看护块"
        block = src[i:code_block_bounds(src, i)]
        assert "_ensure_stockdb()" in block, "行情引擎巡检未接进周期看护"
        assert "_ensure_dashboard()" in block and "_ensure_obs_stack()" in block

    def test_engine_port_matches_the_repo_constant(self):
        """端口必须与仓内其它地方一致(写错端口=巡检永远报掉线)。"""
        import engine_bars_sync as E
        import inspect
        src = inspect.getsource(E)
        assert "7899" in src, "engine_bars_sync 里没出现 7899, 端口约定可能变了"
