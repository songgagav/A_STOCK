# -*- coding: utf-8 -*-
"""agent_tools 能力声明 (P0 清单第 7 项) 回归测试。

锁住的边界不变量:
  · **每个工具都有能力声明** —— 未分类必须被报出来, 不能靠命名习惯默认获得能力;
  · **没有工具能直接下单** —— 下单必须走 realtime_engine 的咽喉点(合规->高危->人工);
  · 声称只读的工具**真的**不含任何写/资金能力;
  · `execute(require=/forbid=)` 在**调用前**拦截, 不是调用后再报错。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import agent_tools as AT  # noqa: E402


class TestTaxonomy:
    def test_every_capability_has_a_definition(self):
        """能力值必须都在分类法里有定义 —— 否则"能力"变成自由文本, 无法校验。"""
        for name, caps in AT.CAPABILITIES.items():
            for c in caps:
                assert c in AT.CAPABILITY_TAXONOMY, f"{name}: 未定义的能力 {c}"

    def test_taxonomy_declares_the_two_directions(self):
        assert "read:market" in AT.CAPABILITY_TAXONOMY
        assert "order" in AT.CAPABILITY_TAXONOMY

    def test_write_and_money_sets_disjoint_from_reads(self):
        assert not set(AT.WRITE_CAPABILITIES) & set(AT.MONEY_CAPABILITIES)

    def test_write_tools_have_a_stated_reason(self):
        """能写盘的工具必须是**显式**列出来的(附理由), 不靠事后发现。"""
        assert set(AT.WRITE_TOOLS) <= set(AT.CAPABILITIES)
        for t, c in AT.WRITE_TOOLS.items():
            assert c in AT.CAPABILITY_TAXONOMY


class TestCapabilitiesOf:
    def test_known_tool(self):
        assert "read:kb" in AT.capabilities_of("graphrag_search")

    def test_unknown_tool_is_unclassified_not_empty(self):
        """**关键**: 未分类必须是一个显式标记, 不是空元组。
        空元组既像"什么都能做"又像"什么都不能做", 无法据此拦截。"""
        assert AT.capabilities_of("no_such_tool") == ("unclassified",)

    def test_has_capability(self):
        assert AT.has_capability("spc_check", "veto") is True
        assert AT.has_capability("spc_check", "order") is False

    def test_read_only_definition(self):
        assert AT.is_read_only("graphrag_search") is True
        assert AT.is_read_only("ic_backtest") is False      # 会落 IC 缓存
        assert AT.is_read_only("risk_first_check") is False  # 有 veto


class TestAudit:
    def test_audit_is_clean(self):
        a = AT.audit_capabilities()
        assert a["ok"] is True, f"未分类={a['unclassified']} 下单工具={a['order_tools']}"

    def test_no_unclassified_tools_registered(self):
        """注册表里每个工具都必须在能力表里有归类。新增工具忘了归类会在这里红。"""
        assert AT.audit_capabilities()["unclassified"] == []

    def test_no_order_capable_tools(self):
        """**边界不变量**: Agent 工具不得直接下单。
        新增 order 工具必须同时接进 pretrade_compliance 的三段式, 否则等于在
        闸门外开了一条通道 —— 这条断言就是那道提醒。"""
        assert AT.audit_capabilities()["order_tools"] == []
        assert AT.ORDER_TOOLS == ()

    def test_writers_are_enumerated(self):
        assert set(AT.audit_capabilities()["writers"]) == set(AT.WRITE_TOOLS)

    def test_every_registered_tool_is_classified(self):
        for t in AT.list_tools():
            assert t["capabilities"], t["name"]
            assert "unclassified" not in t["capabilities"], t["name"]


class TestListAndSchemas:
    def test_list_tools_includes_capabilities(self):
        d = {t["name"]: t for t in AT.list_tools()}
        assert "capabilities" in d["ic_backtest"]
        assert "write:artifact" in d["ic_backtest"]["capabilities"]

    def test_list_tools_can_omit_capabilities(self):
        for t in AT.list_tools(include_capabilities=False):
            assert "capabilities" not in t

    def test_schemas_include_capabilities(self):
        d = {t["name"]: t for t in AT.get_tool_schemas()}
        assert "capabilities" in d["spc_check"]
        assert "input_schema" in d["spc_check"]

    def test_schemas_can_omit_capabilities(self):
        for t in AT.get_tool_schemas(include_capabilities=False):
            assert "capabilities" not in t

    def test_tool_count_stable(self):
        assert len(AT.list_tools()) == len(AT.CAPABILITIES)


class TestExecuteGate:
    def test_unknown_tool_still_reported(self):
        r = AT.execute("no_such_tool", {})
        assert r["ok"] is False and "未知工具" in r["error"]

    def test_forbid_blocks_before_calling(self, monkeypatch):
        """拦截必须发生在**调用之前** —— 调用后再报错等于写已经发生了。"""
        called = {"n": 0}

        def spy(**kw):
            called["n"] += 1
            return {"ok": True}

        tool = AT.Tool("spy_writer", "d", {}, spy, capabilities=("write:artifact",))
        monkeypatch.setitem(AT._TOOLS, "spy_writer", tool)
        r = AT.execute("spy_writer", {}, forbid=("write:artifact",))
        assert r["ok"] is False and r["error"] == "capability_denied"
        assert called["n"] == 0

    def test_require_blocks_before_calling(self, monkeypatch):
        called = {"n": 0}

        def spy(**kw):
            called["n"] += 1
            return {"ok": True}

        tool = AT.Tool("spy_reader", "d", {}, spy, capabilities=("read:market",))
        monkeypatch.setitem(AT._TOOLS, "spy_reader", tool)
        r = AT.execute("spy_reader", {}, require=("veto",))
        assert r["ok"] is False and "缺少所需能力" in r["reasons"][0]
        assert called["n"] == 0

    def test_require_pass_through_when_satisfied(self, monkeypatch):
        tool = AT.Tool("spy_veto", "d", {}, lambda **kw: {"ok": True},
                       capabilities=("veto",))
        monkeypatch.setitem(AT._TOOLS, "spy_veto", tool)
        r = AT.execute("spy_veto", {}, require=("veto",))
        assert r["ok"] is True

    def test_forbid_pass_through_when_absent(self, monkeypatch):
        tool = AT.Tool("spy_ro", "d", {}, lambda **kw: {"ok": True},
                       capabilities=("read:market",))
        monkeypatch.setitem(AT._TOOLS, "spy_ro", tool)
        assert AT.execute("spy_ro", {}, forbid=("order",))["ok"] is True

    def test_denied_result_is_json_serialisable(self, monkeypatch):
        import json
        tool = AT.Tool("spy_w2", "d", {}, lambda **kw: {"ok": True},
                       capabilities=("write:ledger",))
        monkeypatch.setitem(AT._TOOLS, "spy_w2", tool)
        json.dumps(AT.execute("spy_w2", {}, forbid=("write:ledger",)), ensure_ascii=False)

    def test_both_checks_can_combine(self, monkeypatch):
        tool = AT.Tool("spy_both", "d", {}, lambda **kw: {"ok": True},
                       capabilities=("read:market", "write:artifact"))
        monkeypatch.setitem(AT._TOOLS, "spy_both", tool)
        assert AT.execute("spy_both", {}, require=("read:market",),
                          forbid=("order",))["ok"] is True
        assert AT.execute("spy_both", {}, require=("order",))["ok"] is False


class TestToolClass:
    def test_explicit_capabilities_win(self):
        t = AT.Tool("x", "d", {}, lambda: None, capabilities=("compute",))
        assert t.capabilities == ("compute",)

    def test_looked_up_when_not_given(self):
        t = AT.Tool("spc_check", "d", {}, lambda: None)
        assert "veto" in t.capabilities

    def test_unknown_name_gets_unclassified(self):
        t = AT.Tool("zzz", "d", {}, lambda: None)
        assert t.capabilities == ("unclassified",)

    def test_slots_include_capabilities(self):
        assert "capabilities" in AT.Tool.__slots__
