# -*- coding: utf-8 -*-
"""高危单待批队列的回归测试（路线图 #3 第二块）.

红线: 高危单**不执行**, 转人工; 人工批准后方可由引擎放行; 正常单不进队列。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from pretrade_compliance import (  # noqa: E402
    approved_orders, decide, enqueue, pending_path,
)

CTX = {"cash": 50000, "equity": 100000, "max_pos": 5, "min_cash": 5000,
       "tradable": True, "position_qty": 1000, "sellable_qty": 1000}
NOW = datetime(2026, 9, 21, 9, 20, 0)

BIG = {"symbol": "600000", "side": "buy", "qty": 3000, "price": 10.0}      # 30000 > slot 20000
LIQ = {"symbol": "600000", "side": "sell", "qty": 1000, "price": 10.0}     # 清仓式


def _paths(tmp_path):
    return str(tmp_path / "pend.json"), str(tmp_path / "audit.jsonl")


class TestEnqueue:
    def test_enqueue_records_and_audits(self, tmp_path):
        p, a = _paths(tmp_path)
        it = enqueue(BIG, CTX, actor="engine", path=p, audit_fp=a, now=NOW)
        assert it["status"] == "pending" and it["order"]["qty"] == 3000
        assert any("槽位" in r for r in it["reasons"])
        lines = [json.loads(x) for x in open(a, encoding="utf-8").read().splitlines()]
        assert lines[0]["action"] == "pending_approval" and lines[0]["id"] == it["id"]

    def test_ids_are_unique(self, tmp_path):
        p, a = _paths(tmp_path)
        i1 = enqueue(BIG, CTX, path=p, audit_fp=a, now=NOW)
        i2 = enqueue(LIQ, CTX, path=p, audit_fp=a, now=NOW)
        assert i1["id"] != i2["id"]
        assert len(approved_orders(p)) == 0        # 未批准的不算可放行


class TestDecide:
    def test_approve_then_available_to_engine(self, tmp_path):
        p, a = _paths(tmp_path)
        it = enqueue(BIG, CTX, path=p, audit_fp=a, now=NOW)
        r = decide(it["id"], approve=True, actor="ops", reason="已核对", path=p, audit_fp=a, now=NOW)
        assert r["ok"] is True and r["item"]["decided_by"] == "ops"
        assert [x["id"] for x in approved_orders(p)] == [it["id"]]

    def test_reject_is_not_released(self, tmp_path):
        p, a = _paths(tmp_path)
        it = enqueue(LIQ, CTX, path=p, audit_fp=a, now=NOW)
        decide(it["id"], approve=False, actor="ops", reason="疑似数据故障", path=p, audit_fp=a, now=NOW)
        assert approved_orders(p) == []

    def test_double_decide_is_refused(self, tmp_path):
        """已决的单不可再决 —— 否则可能把驳回过的单再批准放行。"""
        p, a = _paths(tmp_path)
        it = enqueue(BIG, CTX, path=p, audit_fp=a, now=NOW)
        decide(it["id"], approve=False, actor="ops", path=p, audit_fp=a, now=NOW)
        r2 = decide(it["id"], approve=True, actor="ops", path=p, audit_fp=a, now=NOW)
        assert r2["ok"] is False and "已决" in r2["error"]
        assert approved_orders(p) == []

    def test_unknown_id_is_refused(self, tmp_path):
        p, a = _paths(tmp_path)
        assert decide("nope", approve=True, path=p, audit_fp=a, now=NOW)["ok"] is False

    def test_decisions_are_audited_with_actor(self, tmp_path):
        p, a = _paths(tmp_path)
        it = enqueue(BIG, CTX, path=p, audit_fp=a, now=NOW)
        decide(it["id"], approve=True, actor="ops", reason="已核对", path=p, audit_fp=a, now=NOW)
        lines = [json.loads(x) for x in open(a, encoding="utf-8").read().splitlines()]
        assert [x["action"] for x in lines] == ["pending_approval", "approved"]
        assert lines[1]["actor"] == "ops" and lines[1]["reasons"] == ["已核对"]


class TestRobustness:
    def test_missing_queue_file_is_empty_not_error(self, tmp_path):
        assert approved_orders(str(tmp_path / "absent.json")) == []

    def test_corrupt_queue_is_empty_not_crash(self, tmp_path):
        fp = tmp_path / "bad.json"
        fp.write_text("{半截", encoding="utf-8")
        assert approved_orders(str(fp)) == []

    def test_default_path_under_data(self):
        assert pending_path().replace("\\", "/").endswith("data/pending_orders.json")


class TestApprovalWhitelist:
    """「批准即白名单」闭环: 批准后同一笔单能真正放行, 且**只放行一次/不超批准量**。"""

    def _g(self, tmp_path, order, qty=None):
        from pretrade_compliance import gate
        p, a = _paths(tmp_path)
        o = dict(order)
        if qty is not None:
            o["qty"] = qty
        return gate(o, CTX, actor="engine", path=p, audit_fp=a, now=NOW), p, a

    def test_unapproved_high_risk_is_blocked(self, tmp_path):
        r, _, _ = self._g(tmp_path, BIG)
        assert r["decision"] == "pending_approval"

    def test_approved_then_executes_next_tick(self, tmp_path):
        from pretrade_compliance import decide
        r, p, a = self._g(tmp_path, BIG)
        decide(r["id"], approve=True, actor="ops", reason="已核对", path=p, audit_fp=a, now=NOW)
        r2, _, _ = self._g(tmp_path, BIG)
        assert r2["decision"] == "execute"
        assert "已批准" in r2["reasons"][0] and r2["approved_ticket"] == r["id"]

    def test_ticket_is_single_use(self, tmp_path):
        """一次性: 消费过就必须重新送审 —— 否则一张票会变成永久放行。"""
        from pretrade_compliance import decide
        r, p, a = self._g(tmp_path, BIG)
        decide(r["id"], approve=True, actor="ops", path=p, audit_fp=a, now=NOW)
        assert self._g(tmp_path, BIG)[0]["decision"] == "execute"
        assert self._g(tmp_path, BIG)[0]["decision"] == "pending_approval"

    def test_qty_above_approved_is_not_covered(self, tmp_path):
        """**单调安全界**: 批准的是"至多这么多股"; 引擎想下更多 => 不认票, 重新送审。"""
        from pretrade_compliance import decide
        p, a = _paths(tmp_path)
        it = enqueue(BIG, CTX, path=p, audit_fp=a, now=NOW)      # 批准 3000 股
        decide(it["id"], approve=True, actor="ops", path=p, audit_fp=a, now=NOW)
        from pretrade_compliance import gate
        big2 = dict(BIG, qty=4000)
        r = gate(big2, CTX, actor="engine", path=p, audit_fp=a, now=NOW)
        assert r["decision"] == "pending_approval"               # 4000 > 3000 => 不认

    def test_smaller_qty_within_approved_is_covered(self, tmp_path):
        """价格变动导致数量变小属正常, 应放行(不能按精确数量匹配, 否则永远匹配不上)。

        注: 数量必须**仍属高危**(> 一个等权槽位 20000), 否则走的是正常执行路径,
        这条测试就会"因为错误的原因通过" —— 最初就踩过这个坑。
        """
        from pretrade_compliance import decide, gate
        p, a = _paths(tmp_path)
        it = enqueue(BIG, CTX, path=p, audit_fp=a, now=NOW)          # 批准 3000 股
        decide(it["id"], approve=True, actor="ops", path=p, audit_fp=a, now=NOW)
        r = gate(dict(BIG, qty=2500), CTX, path=p, audit_fp=a, now=NOW)   # 25000 > slot 20000
        assert r["decision"] == "execute"
        assert r.get("approved_ticket") == it["id"]

    def test_rejected_ticket_never_executes(self, tmp_path):
        from pretrade_compliance import decide
        r, p, a = self._g(tmp_path, BIG)
        decide(r["id"], approve=False, actor="ops", path=p, audit_fp=a, now=NOW)
        assert self._g(tmp_path, BIG)[0]["decision"] == "pending_approval"

    def test_other_symbol_not_covered(self, tmp_path):
        from pretrade_compliance import decide, gate
        p, a = _paths(tmp_path)
        it = enqueue(BIG, CTX, path=p, audit_fp=a, now=NOW)
        decide(it["id"], approve=True, actor="ops", path=p, audit_fp=a, now=NOW)
        assert gate(dict(BIG, symbol="000001"), CTX, path=p, audit_fp=a, now=NOW)["decision"] \
            == "pending_approval"

    def test_sell_is_never_queued_even_if_high_risk(self, tmp_path):
        """设计意图: **离场永不排队**(把止损排进人工审批=把风险锁在仓里)。
        故这里断言的是 execute, 而不是 pending_approval。"""
        from pretrade_compliance import gate
        p, a = _paths(tmp_path)
        r = gate({"symbol": "600000", "side": "sell", "qty": 1000, "price": 10.0},
                 CTX, path=p, audit_fp=a, now=NOW)
        assert r["decision"] == "execute"
        # 说明落在审计里(而非返回值): 离场单既要放行, 也要在账上写清"为什么没送审"
        acts = [json.loads(x) for x in open(a, encoding="utf-8").read().splitlines()]
        assert acts[-1]["action"] == "execute"
        assert "离场单" in acts[-1]["reasons"][0]
        assert json.load(open(p, encoding="utf-8"))["orders"] == [] if os.path.exists(p) else True
        assert approved_orders(p) == []          # 队列为空(缺文件也算空)

    def test_execution_is_audited(self, tmp_path):
        """放行必须在审计里可查(人批了什么 vs 实际执行了多少)。"""
        from pretrade_compliance import decide, gate
        p, a = _paths(tmp_path)
        it = enqueue(BIG, CTX, path=p, audit_fp=a, now=NOW)
        decide(it["id"], approve=True, actor="ops", path=p, audit_fp=a, now=NOW)
        gate(dict(BIG, qty=2500), CTX, path=p, audit_fp=a, now=NOW)      # 仍属高危, 走票
        acts = [json.loads(x)["action"] for x in open(a, encoding="utf-8").read().splitlines()]
        assert "approved_executed" in acts

    def test_repeat_ticks_do_not_flood_queue(self, tmp_path):
        """引擎每 tick 重生成同一笔单 => 队列必须去重, 否则一屏重复行淹没真正的新单。"""
        from pretrade_compliance import gate
        p, a = _paths(tmp_path)
        for _ in range(5):
            gate(BIG, CTX, path=p, audit_fp=a, now=NOW)
        doc = json.load(open(p, encoding="utf-8"))
        assert len(doc["orders"]) == 1
