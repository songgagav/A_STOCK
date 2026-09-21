# -*- coding: utf-8 -*-
"""pretrade_compliance 的回归测试（路线图 #3）.

红线: **正常单自动执行, 只有高风险单转人工** —— 测试必须同时锁住两个方向,
既不能把正常单误升级成人工审批(否则系统实际上停摆), 也不能漏放高危单。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from pretrade_compliance import (  # noqa: E402
    RISK_FULL_SLOT, RISK_LIQUIDATE, VIOL_CASH, VIOL_LOT, VIOL_PRICE, VIOL_QTY,
    VIOL_SELLABLE, VIOL_TRADABLE, audit_path, check_order, classify_risk,
    record_review, review,
)

CTX = {"cash": 50000, "equity": 100000, "max_pos": 5, "min_cash": 5000,
       "tradable": True, "position_qty": 1000, "sellable_qty": 1000}


def _o(**kw):
    o = {"symbol": "600000", "side": "buy", "qty": 500, "price": 10.0}
    o.update(kw)
    return o


class TestCompliance:
    def test_normal_order_passes(self):
        assert check_order(_o(), CTX)["ok"] is True

    def test_non_lot_is_rejected(self):
        r = check_order(_o(qty=150), CTX)
        assert [v["code"] for v in r["violations"]] == [VIOL_LOT]

    def test_lot_size_is_the_repo_rule_100(self):
        """整手 100 是 A 股硬规则, 也是引擎已在用的规则 —— 不得改成别的数。"""
        assert check_order(_o(qty=100), CTX)["ok"] is True
        assert check_order(_o(qty=50), CTX)["ok"] is False

    def test_sell_does_not_require_lot_multiple(self):
        """卖出允许零股(持仓可能因送股等出现非整手), 只校验可卖数量。"""
        assert check_order(_o(side="sell", qty=150), CTX)["ok"] is True

    def test_invalid_price_rejected(self):
        assert [v["code"] for v in check_order(_o(price=0), CTX)["violations"]] == [VIOL_PRICE]

    def test_invalid_qty_rejected(self):
        assert VIOL_QTY in [v["code"] for v in check_order(_o(qty=0), CTX)["violations"]]

    def test_untradable_buy_rejected(self):
        c = dict(CTX, tradable=False)
        assert VIOL_TRADABLE in [v["code"] for v in check_order(_o(), c)["violations"]]

    def test_sell_only_blocks_buy_but_not_sell(self):
        c = dict(CTX, tradable="sell_only")
        assert check_order(_o(), c)["ok"] is False
        assert check_order(_o(side="sell"), c)["ok"] is True

    def test_cash_floor(self):
        r = check_order(_o(qty=4600, price=10.0), dict(CTX, cash=50000))   # 50000-46000=4000<5000
        assert VIOL_CASH in [v["code"] for v in r["violations"]]

    def test_sell_beyond_sellable_rejected(self):
        assert VIOL_SELLABLE in [v["code"] for v in
                                 check_order(_o(side="sell", qty=2000), CTX)["violations"]]

    def test_missing_context_does_not_reject(self):
        """缺字段 = 无信息, **不得**据此拒单(否则缺个 key 就静默停手)。"""
        assert check_order(_o(), {})["ok"] is True
        assert check_order(_o(side="sell"), {})["ok"] is True


class TestRisk:
    def test_normal_order_not_high_risk(self):
        assert classify_risk(_o(), CTX)["high_risk"] is False

    def test_notional_over_one_equal_weight_slot(self):
        """等权构造下 slot = equity/max_pos = 20000; 一笔 30000 属结构性异常。"""
        r = classify_risk(_o(qty=3000, price=10.0), CTX)
        assert r["high_risk"] is True
        assert r["flags"][0]["code"] == RISK_FULL_SLOT
        assert r["slot"] == 20000

    def test_exactly_one_slot_is_not_flagged(self):
        """等于一个完整槽位不算高危(边界取严格大于) —— 否则正常建仓会被误升级。"""
        assert classify_risk(_o(qty=2000, price=10.0), CTX)["high_risk"] is False

    def test_liquidating_sell_is_high_risk(self):
        r = classify_risk(_o(side="sell", qty=1000), CTX)
        assert r["high_risk"] is True
        assert r["flags"][0]["code"] == RISK_LIQUIDATE

    def test_partial_sell_not_flagged(self):
        assert classify_risk(_o(side="sell", qty=500), CTX)["high_risk"] is False

    def test_no_slot_without_equity_or_max_pos(self):
        assert classify_risk(_o(qty=999999), {})["high_risk"] is False


class TestRedLine:
    """核心: 正常单必须**自动执行**, 只有高危单才转人工(否则等于系统停摆)。"""

    def test_normal_order_executes_automatically(self):
        r = review(_o(), CTX)
        assert r["decision"] == "execute"
        assert r["reasons"] == []

    def test_violation_is_rejected_not_pending(self):
        """不合规是**硬拒**, 不进人工队列 —— 别把规则问题丢给人做判断题。"""
        assert review(_o(qty=150), CTX)["decision"] == "reject"

    def test_high_risk_goes_to_human(self):
        assert review(_o(qty=3000, price=10.0), CTX)["decision"] == "pending_approval"

    def test_liquidating_sell_goes_to_human(self):
        assert review(_o(side="sell", qty=1000), CTX)["decision"] == "pending_approval"

    def test_reject_takes_precedence_over_pending(self):
        """既不合规又是高危 => 拒单(合规优先于风险分级)。"""
        r = review(_o(qty=3000, price=0), CTX)
        assert r["decision"] == "reject"


class TestAudit:
    def test_record_review_appends_with_actor_and_reasons(self, tmp_path):
        from datetime import datetime
        fp = str(tmp_path / "audit.jsonl")
        now = datetime(2026, 9, 21, 9, 20, 0)
        record_review(_o(), CTX, actor="engine", path=fp, now=now)
        record_review(_o(qty=3000, price=10.0), CTX, actor="engine", path=fp, now=now)
        lines = [json.loads(x) for x in open(fp, encoding="utf-8").read().splitlines()]
        assert [x["action"] for x in lines] == ["execute", "pending_approval"]
        assert lines[1]["symbol"] == "600000" and lines[1]["qty"] == 3000
        assert lines[0]["ts"] == "2026-09-21 09:20:00"

    def test_audit_failure_does_not_raise(self, tmp_path):
        from pretrade_compliance import audit
        audit({"action": "execute"}, path=str(tmp_path / "no" / "dir" / "x.jsonl"))

    def test_audit_path_default_under_data(self):
        assert audit_path().replace("\\", "/").endswith("data/order_audit.jsonl")

    def test_audit_is_separate_from_kill_switch_ledger(self):
        """订单级流水与闸门级事件分账 —— 混在一起会互相淹没。"""
        from kill_switch import ledger_path
        assert audit_path() != ledger_path()
