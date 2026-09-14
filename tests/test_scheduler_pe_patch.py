# -*- coding: utf-8 -*-
"""盘后 pe_ttm 兜底步骤的编排逻辑 (scheduler_entry._pe_patch_and_sentinel).

要点: 哨兵正常 -> 不补; 哨兵报缺口 -> 补; PE_PATCH_AUTO=0 -> 只体检不补;
任何异常都不得抛出(不能阻断收盘管道)。
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))


class _FakeProc:
    def __init__(self, rc=0, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


@pytest.fixture()
def se(monkeypatch):
    import scheduler_entry as mod
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)   # 不污染 logs/run_daily.log
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "valuation_coverage_sentinel" in " ".join(cmd):
            return _FakeProc(rc=0, out="pe_ttm 中位 0.9  CRITICAL 0 / WARN 0")
        return _FakeProc(rc=0, out="缺 pe_ttm 的 symbol 数: 0\n完成")
    monkeypatch.setattr("subprocess.run", fake_run)
    return mod, calls, monkeypatch


def test_sentinel_ok_skips_backfill(se):
    mod, calls, _ = se
    r = mod._pe_patch_and_sentinel()
    assert r["sentinel"]["rc"] == 0
    assert "无需兜底" in r["backfill"]["skipped"]
    assert len(calls) == 1, "覆盖率正常时不应调用补丁脚本"


def test_gap_triggers_backfill_with_resume(se):
    mod, calls, monkeypatch = se

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "valuation_coverage_sentinel" in " ".join(cmd):
            return _FakeProc(rc=1, out="pe_ttm 中位 0.0  CRITICAL 0 / WARN 8")
        return _FakeProc(rc=0, out="a\nb")
    monkeypatch.setattr("subprocess.run", fake_run)
    r = mod._pe_patch_and_sentinel(days=7)
    assert r["sentinel"]["rc"] == 1
    assert r["backfill"]["rc"] == 0
    bf_cmd = " ".join(calls[-1])
    assert "backfill_pe_ttm" in bf_cmd and "--resume" in bf_cmd and "--days 7" in bf_cmd


def test_auto_off_only_checks(se):
    mod, calls, monkeypatch = se
    monkeypatch.setenv("PE_PATCH_AUTO", "0")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _FakeProc(rc=1, out="gap")
    monkeypatch.setattr("subprocess.run", fake_run)
    r = mod._pe_patch_and_sentinel()
    assert "PE_PATCH_AUTO=0" in r["backfill"]["skipped"]
    assert len(calls) == 1


def test_exception_is_swallowed(se):
    """哨兵子进程异常时不得抛出(否则会阻断收盘管道后续步骤)."""
    mod, calls, monkeypatch = se

    def boom(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr("subprocess.run", boom)
    r = mod._pe_patch_and_sentinel()
    assert r["sentinel"]["rc"] == -1
    assert "OSError" in r["sentinel"]["error"]


def test_module_import_does_not_chdir():
    """模块必须可被安全导入: import 不得改变 CWD."""
    cwd = os.getcwd()
    import importlib
    import scheduler_entry
    importlib.reload(scheduler_entry)
    assert os.getcwd() == cwd
