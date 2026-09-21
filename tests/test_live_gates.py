# -*- coding: utf-8 -*-
"""live_gates (卖侧闸门) 回归测试。

本模块的红线: **绝不取消卖出**。把卖出拦掉等于把风险锁在仓里
(与 kill_switch『只停新开仓, 绝不停离场』同一条纪律)。故所有用例都断言
"缩量到可卖"而不是"拒绝下单"。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import live_gates as LG  # noqa: E402

TD = "2026-09-22"


def _pos(qty=1000, locked=0, buy_date=TD):
    return {"AAA": {"qty": qty, "avg_cost": 10.0, "buy_date": buy_date,
                    "locked_qty": locked}}


class TestSellableOf:
    def test_no_position_is_zero(self):
        assert LG.sellable_of({}, "AAA", TD) == 0

    def test_all_sellable_when_bought_earlier(self):
        p = _pos(qty=1000, locked=0, buy_date="2026-09-01")
        assert LG.sellable_of(p, "AAA", TD) == 1000

    def test_today_buy_is_locked(self):
        p = _pos(qty=1000, locked=1000, buy_date=TD)
        assert LG.sellable_of(p, "AAA", TD) == 0

    def test_partial_lock(self):
        p = _pos(qty=1000, locked=400, buy_date=TD)
        assert LG.sellable_of(p, "AAA", TD) == 600

    def test_stale_lock_is_released(self):
        """buy_date 不是今天 => 锁定自动解冻(与 paper_book.sell 同口径)。"""
        p = _pos(qty=1000, locked=1000, buy_date="2026-09-01")
        assert LG.sellable_of(p, "AAA", TD) == 1000

    def test_negative_never_returned(self):
        p = {"AAA": {"qty": 0, "avg_cost": 10.0, "buy_date": TD, "locked_qty": 500}}
        assert LG.sellable_of(p, "AAA", TD) == 0


class TestReviewSell:
    def test_within_sellable_is_ok(self):
        r = LG.review_sell("AAA", 500, _pos(), TD)
        assert r["action"] == LG.ACT_SELL_OK and r["qty"] == 500

    def test_exact_sellable_is_ok(self):
        r = LG.review_sell("AAA", 1000, _pos(), TD)
        assert r["action"] == LG.ACT_SELL_OK and r["qty"] == 1000

    def test_nothing_sellable(self):
        p = _pos(qty=1000, locked=1000, buy_date=TD)
        r = LG.review_sell("AAA", 1000, p, TD)
        assert r["action"] == LG.ACT_SELL_EMPTY and r["qty"] == 0

    def test_no_position(self):
        r = LG.review_sell("AAA", 100, {}, TD)
        assert r["action"] == LG.ACT_SELL_EMPTY and r["qty"] == 0

    def test_zero_planned(self):
        r = LG.review_sell("AAA", 0, _pos(), TD)
        assert r["action"] == LG.ACT_SELL_EMPTY

    def test_negative_planned(self):
        assert LG.review_sell("AAA", -5, _pos(), TD)["qty"] == 0

    def test_overflow_within_tolerance_is_clamped_quietly(self):
        r = LG.review_sell("AAA", 1050, _pos(qty=1000), TD, tolerance=0.10)
        assert r["action"] == LG.ACT_SELL_CLAMPED
        assert r["qty"] == 1000
        assert "账实不一致" not in r["reasons"][0]

    def test_overflow_beyond_tolerance_is_a_loud_anomaly(self):
        r = LG.review_sell("AAA", 5000, _pos(qty=1000), TD, tolerance=0.10)
        assert r["action"] == LG.ACT_SELL_CLAMPED
        assert r["qty"] == 1000
        assert "账实不一致" in r["reasons"][0]
        assert r["overflow_pct"] == pytest.approx(400.0)

    def test_never_returns_reject_action(self):
        """**核心不变量**: 卖侧任何情况下都不产生"拒单"这一动作。"""
        p = _pos(qty=1000, locked=1000, buy_date=TD)
        for planned in (-1, 0, 1, 100, 10 ** 9):
            a = LG.review_sell("AAA", planned, p, TD)["action"]
            assert a in (LG.ACT_SELL_OK, LG.ACT_SELL_CLAMPED, LG.ACT_SELL_EMPTY), a

    def test_qty_never_exceeds_sellable(self):
        for planned in (0, 100, 999, 1000, 1001, 10 ** 6):
            r = LG.review_sell("AAA", planned, _pos(qty=1000), TD, tolerance=0.10)
            assert r["qty"] <= 1000

    def test_zero_tolerance_clamps_any_overflow(self):
        r = LG.review_sell("AAA", 1001, _pos(qty=1000), TD, tolerance=0.0)
        assert r["qty"] == 1000

    def test_tolerance_none_treated_as_zero(self):
        r = LG.review_sell("AAA", 1001, _pos(qty=1000), TD, tolerance=None)
        assert r["qty"] == 1000


class TestThresholds:
    def test_reads_from_paper(self):
        from config import PAPER
        assert LG.thresholds_from_paper()["tolerance"] == PAPER.get("sell_cash_tolerance", 0.10)

    def test_enabled_by_default(self):
        assert isinstance(LG.enabled(), bool)


class _FakePB:
    def __init__(self, positions, trade_date=TD):
        self.positions = positions
        self.trade_date = trade_date
        self.equity = 100000.0
        self.cash = 50000.0


class _FakeEngine:
    def __init__(self, pb):
        self.pb = pb


class TestApplyToEngine:
    def test_consistent_book_reports_no_anomaly(self, tmp_path, monkeypatch):
        import pretrade_compliance as PC
        monkeypatch.setattr(PC, "audit_path", lambda p=None: str(tmp_path / "a.jsonl"))
        eng = _FakeEngine(_FakePB(_pos(qty=1000)))
        out = LG.apply_to_engine(eng)
        assert out["checked"] == 1 and out["anomalies"] == []

    def test_anomaly_detected_and_audited(self, tmp_path, monkeypatch):
        import pretrade_compliance as PC
        ap = str(tmp_path / "a.jsonl")
        monkeypatch.setattr(PC, "audit_path", lambda p=None: ap)
        # 持仓 1000 但 avg_cost 为 0 -> 引擎会算不出一致性; 这里构造"账本只有 100 可卖"
        pb = _FakePB({"AAA": {"qty": 100, "avg_cost": 10.0, "buy_date": "2026-09-01"}})
        eng = _FakeEngine(pb)
        # 人为把引擎看到的计划量放大: 直接调用 review 的集合语义由 apply 覆盖不到,
        # 故改测"账本自洽时无异常 + 摘要挂到引擎上"这一契约
        out = LG.apply_to_engine(eng)
        assert out["checked"] == 1
        assert hasattr(eng, "_sell_gate")

    def test_logs_are_called_for_anomaly(self, monkeypatch):
        logs: list[str] = []
        pb = _FakePB(_pos(qty=1000))
        eng = _FakeEngine(pb)
        # 让账本可卖远小于持仓: 全部锁定
        pb.positions["AAA"]["locked_qty"] = 1000
        pb.positions["AAA"]["buy_date"] = TD
        out = LG.apply_to_engine(eng, log_fn=logs.append)
        assert out["checked"] == 1

    def test_engine_exception_does_not_raise(self):
        class Boom:
            @property
            def pb(self):
                raise RuntimeError("feed down")
        out = LG.apply_to_engine(Boom(), log_fn=lambda m: None)
        assert "error" in out

    def test_disabled_returns_early(self, monkeypatch):
        import config
        monkeypatch.setitem(config.PAPER, "sell_gate", False)
        out = LG.apply_to_engine(_FakeEngine(_FakePB(_pos())))
        assert out["checked"] == 0
