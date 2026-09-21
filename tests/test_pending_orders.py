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
