# -*- coding: utf-8 -*-
"""可解释查询的回归测试（路线图 #7）.

重点锁三件事:
  1. 每条"为什么"都带**来源**（不能只给结论不给出处）;
  2. 缺来源时进 `not_judged`（**不猜**）—— 把盲区藏在结论里比没结论更危险;
  3. 引用账本时**报它的链校验状态**（与 #6 衔接: 账本被篡改则解释不可信）。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from explain import (  # noqa: E402
    build_explanation, explain_events, find_order_events,
)


class TestBuildExplanation:
    def test_every_why_carries_a_source(self):
        r = build_explanation(state="DEGRADED", reasons=["tick 延迟异常: p50=11888ms (>1000ms)"],
                              flow={"level": "OK", "cause": "idle", "reason": "非盘中"},
                              ks={"blocked": False}, pending_count=0)
        assert all(w.get("source") for w in r["whys"])
        srcs = {w["source"] for w in r["whys"]}
        assert srcs == {"health_state", "flow_watchdog", "kill_switch", "pretrade_compliance"}

    def test_reason_text_preserved_verbatim(self):
        """原始原因文本必须原样带上(含阈值与实测值) —— 不能在本层改写。"""
        r = build_explanation(state="DEGRADED", reasons=["tick 延迟异常: p50=11888ms (>1000ms)"])
        assert any("p50=11888ms" in w["detail"] and "(>1000ms)" in w["detail"] for w in r["whys"])

    def test_missing_sources_go_to_not_judged_not_guessed(self):
        r = build_explanation()      # 什么都不给
        assert r["whys"] == []
        what = {n["what"] for n in r["not_judged"]}
        assert "当前健康状态" in what
        assert "数据是否仍在流动" in what
        assert "是否被 kill switch 拦截" in what
        assert r["state"] is None

    def test_blocked_reports_layers(self):
        r = build_explanation(state="NORMAL",
                              ks={"blocked": True,
                                  "reasons": ["人工总闸已拉下(ops @2026-09-21 09:00:00): 数据待查"]})
        assert r["blocked"] is True
        assert any("人工总闸" in w["detail"] and w["kind"] == "layer_block" for w in r["whys"])

    def test_not_blocked_is_stated_explicitly(self):
        """『没被拦』也要写出来 —— 否则读者无法区分『没拦』和『没查』。"""
        r = build_explanation(state="NORMAL", ks={"blocked": False})
        assert any("三层均未拦截" in w["detail"] for w in r["whys"])

    def test_fail_closed_is_surfaced_as_blind_spot(self):
        r = build_explanation(state="HALTED",
                              ks={"blocked": True, "fail_closed": True,
                                  "reasons": ["开关状态文件不可解析 => 按最严处理"]})
        assert any("GLOBAL 层真实状态" == n["what"] for n in r["not_judged"])

    def test_pending_queue_count_explained(self):
        r = build_explanation(state="DEGRADED", pending_count=3)
        assert any("待批高危单 3 笔" in w["detail"] for w in r["whys"])

    def test_stale_snapshot_is_declared(self):
        """**关键**: 基于陈旧快照的结论必须自报时效, 否则会被读成"此刻"。"""
        r = build_explanation(state="DEGRADED", reasons=["x"], snapshot_ts="2026-09-21 15:02:45",
                              snapshot_age_s=8 * 3600, stale_after_s=1800)
        notes = [n for n in r["not_judged"] if "此刻" in n["what"]]
        assert notes, "陈旧快照必须自报时效"
        assert "28800" in notes[0]["why"] and "15:02:45" in notes[0]["why"]

    def test_fresh_snapshot_has_no_staleness_note(self):
        """新鲜快照不得产生时效说明 —— 但"没去查的来源"仍要如实列出(两件事不能混)。"""
        r = build_explanation(state="NORMAL", flow={"level": "OK", "cause": "idle", "reason": "非盘中"},
                              ks={"blocked": False}, snapshot_age_s=30.0, stale_after_s=1800)
        assert not [n for n in r["not_judged"] if "此刻" in n["what"]]


class TestOrderEvents:
    E = [
        {"ts": "t1", "action": "execute", "symbol": "600000", "side": "buy", "qty": 500,
         "price": 10.0, "actor": "engine", "reasons": []},
        {"ts": "t2", "action": "pending_approval", "symbol": "600000", "side": "buy", "qty": 3000,
         "price": 10.0, "actor": "engine", "reasons": ["单笔金额 30000 > 一个完整等权槽位 20000"]},
        {"ts": "t3", "action": "approved_executed", "symbol": "600000", "side": "buy", "qty": 2500,
         "price": 10.0, "actor": "engine", "reasons": ["人工已批准(票 x, 批准数量 3000)"]},
        {"ts": "t4", "action": "reject", "symbol": "000001", "side": "buy", "qty": 150,
         "price": 9.0, "actor": "engine", "reasons": ["非整手: qty=150"]},
    ]

    def test_filter_by_symbol(self):
        ev = find_order_events(self.E, symbol="600000")
        assert [e["ts"] for e in ev] == ["t1", "t2", "t3"]

    def test_filter_by_symbol_and_side(self):
        assert find_order_events(self.E, symbol="600000", side="sell") == []

    def test_limit_keeps_most_recent(self):
        assert [e["ts"] for e in find_order_events(self.E, symbol="600000", limit=2)] == ["t2", "t3"]

    def test_explanation_pairs_action_with_why(self):
        r = explain_events(find_order_events(self.E, symbol="600000"))
        assert r["n"] == 3
        pend = [x for x in r["events"] if x["action"] == "pending_approval"][0]
        assert "等权槽位" in pend["why"]
        ex = [x for x in r["events"] if x["action"] == "approved_executed"][0]
        assert "人工已批准" in ex["why"]

    def test_event_without_reason_says_so_rather_than_blank(self):
        r = explain_events([{"action": "execute", "symbol": "x"}])
        assert r["events"][0]["why"] == "(无原因记录)"

    def test_tampered_ledger_marks_explanation_untrustworthy(self):
        """与 #6 衔接: 账本链校验失败 => 解释本身要标不可信。"""
        r = explain_events(self.E, ledger_ok=False, ledger_reason="第 2 行内容与自身 hash 不符")
        assert "链校验失败" in r["warning"] and "不可信" in r["warning"]

    def test_verified_ledger_has_no_warning(self):
        assert "warning" not in explain_events(self.E, ledger_ok=True)

    def test_empty_history_is_empty_not_error(self):
        r = explain_events([])
        assert r == {"events": [], "n": 0}
