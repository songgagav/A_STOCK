# -*- coding: utf-8 -*-
"""micro_cost / exec_strategy / exec_gate 回归测试 (路线图 ⑰)。

本文件锁住本批次最重要的两条结论:
  1. **成本恒等**: 在常量成本口径下, 拆 N 档与一次性下单的成本完全相同
     —— 这条一旦被改坏(例如又引入一个"随规模变化的费率"), 拆单就会重新变成
     一个"凭空省钱"的假动作。
  2. **闸门只推迟、从不取消**: 被限掉的量必须出现在 `deferred` 里, 而不是消失。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import exec_gate as EG  # noqa: E402
import exec_strategy as ES  # noqa: E402
import micro_cost as M  # noqa: E402


class TestConstantRate:
    def test_matches_paper(self):
        from config import PAPER
        assert M.constant_rate() == pytest.approx(
            PAPER["slippage"] + PAPER["impact_cost"])

    def test_is_seven_bps(self):
        assert M.constant_rate() * 1e4 == pytest.approx(7.0)


class TestCostIdentity:
    """**核心断言**: 拆单不改变成本。"""

    @pytest.mark.parametrize("notional", [10_000, 250_000, 20_000_000, 100_000_000])
    @pytest.mark.parametrize("n", [1, 2, 5, 20])
    def test_identical_for_all_combinations(self, notional, n):
        r = M.split_cost_identity(notional, n)
        assert r["identical"] is True
        assert r["delta_cost"] == pytest.approx(0.0, abs=1e-9)
        assert r["split_cost"] == pytest.approx(r["unsplit_cost"])

    def test_single_slice_is_trivially_identical(self):
        r = M.split_cost_identity(10_000, 1)
        assert r["unsplit_cost"] == r["split_cost"] == pytest.approx(7.0)

    def test_cost_equals_rate_times_notional(self):
        r = M.split_cost_identity(1_000_000, 7)
        assert r["unsplit_cost"] == pytest.approx(0.0007 * 1_000_000)

    def test_zero_notional(self):
        r = M.split_cost_identity(0, 5)
        assert r["unsplit_cost"] == 0.0 and r["identical"] is True

    def test_zero_slices_clamped_to_one(self):
        assert M.split_cost_identity(10_000, 0)["n_slices"] == 1

    def test_proof_text_mentions_the_reason(self):
        assert "r·" in M.split_cost_identity(10_000, 2)["proof"]

    def test_explicit_rate_override(self):
        r = M.split_cost_identity(10_000, 4, rate=0.001)
        assert r["unsplit_cost"] == pytest.approx(10.0)


class TestParticipationCapQty:
    def test_basic_calculation(self):
        # ADV 1 亿 * cap 10% = 1000 万元; 价 10 元 => 100 万股
        assert M.participation_cap_qty(adv=1e8, participation_cap=0.10,
                                       price=10.0) == 1_000_000

    def test_rounds_down_to_lot(self):
        # 1000万 / 3 元 = 333.33 万股 -> 333.33 万手取整到 100 股
        q = M.participation_cap_qty(adv=1e8, participation_cap=0.10, price=3.0)
        assert q % 100 == 0 and q <= 10_000_000 / 3.0

    def test_lot_one(self):
        q = M.participation_cap_qty(adv=1e6, participation_cap=0.1, price=7.0,
                                    lot=1)
        assert q == int((1e6 * 0.1) / 7.0)

    @pytest.mark.parametrize("adv,cap,price", [
        (None, 0.1, 10.0), (0, 0.1, 10.0), (-1, 0.1, 10.0),
        (1e8, None, 10.0), (1e8, 0, 10.0), (1e8, 0.1, 0), (1e8, 0.1, None),
    ])
    def test_returns_none_when_undeterminable(self, adv, cap, price):
        """拿不到输入时返回 None => 调用方**不设限**(保持既有行为), 不据此拒单。"""
        assert M.participation_cap_qty(adv=adv, participation_cap=cap,
                                       price=price) is None


class TestObservedCostTable:
    def test_isolated_source_is_empty(self):
        """**隔离开关**: `use_default_source=False` 时完全不读生产台账。

        原用例断言的是"生产无成交"(n==0), 那等于把"虚拟盘今天有没有成交"变成测试
        断言的一部分 —— 2026-09-22 虚拟盘真的成交 3 笔后它就失效了。
        现改为断言**隔离行为**本身, 与生产状态解耦。
        """
        ct = M.observed_cost_table(use_default_source=False)
        assert ct["n"] == 0
        assert ct["regression_ready"] is False
        assert ct["source"] == "isolated(none)"
        assert "无法标定" in ct["note"]

    def test_production_trades_without_decomposition_yield_no_buckets(self):
        """**回归锁**: 生产成交不含 impact_bps(常量分支) => 分桶必为空。

        这条比"n==0"更有意义: 即使虚拟盘开始成交, 只要撮合仍走常量分支, 就没有
        可用于回归规模弹性的数据 —— 而"拆单省不省冲击成本"正是靠它回答的。
        """
        ct = M.observed_cost_table()
        assert ct["buckets"] == []
        assert ct["regression_ready"] is False

    def test_with_synthetic_trades(self):
        trades = [{"price": 10.0, "qty": 1000, "impact_bps": 7},
                  {"price": 10.0, "qty": 100_000, "impact_bps": 70},
                  {"price": 10.0, "qty": 1_000_000, "impact_bps": 700}]
        ct = M.observed_cost_table(trades)
        assert ct["n"] == 3
        assert ct["regression_ready"] is True
        assert len(ct["buckets"]) >= 3

    def test_ignores_rows_without_impact(self):
        ct = M.observed_cost_table([{"price": 10.0, "qty": 100}],
                                   use_default_source=False)
        assert ct["buckets"] == []


class TestDecideSplit:
    def test_no_adv(self):
        d = ES.decide_split(notional=1e6, adv=None, participation_cap=0.1)
        assert d["should_split"] is False and "无 ADV" in d["reason"]

    def test_below_cap_no_split(self):
        d = ES.decide_split(notional=10_000, adv=1e8, participation_cap=0.10)
        assert d["should_split"] is False and d["slices"] == 1

    def test_above_cap_splits(self):
        d = ES.decide_split(notional=20_000_000, adv=1e8, participation_cap=0.10)
        assert d["should_split"] is True and d["slices"] == 2

    def test_capped_by_max_slices_flags_multiday(self):
        d = ES.decide_split(notional=1e9, adv=1e8, participation_cap=0.10,
                            max_slices=5)
        assert d["capped_by_max_slices"] is True
        assert d["needs_multiday"] is True
        assert d["slices"] == 5

    def test_zero_cap_no_split(self):
        d = ES.decide_split(notional=1e9, adv=1e8, participation_cap=0)
        assert d["should_split"] is False


class TestThrottleQty:
    def test_not_capped_when_small(self):
        r = ES.throttle_qty(1000, adv=1e8, participation_cap=0.10, price=10.0)
        assert r["qty"] == 1000 and r["deferred"] == 0 and r["capped"] is False

    def test_capped_defers_remainder(self):
        """**核心断言**: 限掉的量必须出现在 deferred 里, 不能消失。"""
        r = ES.throttle_qty(2_000_000, adv=1e8, participation_cap=0.10, price=10.0)
        assert r["capped"] is True
        assert r["qty"] == 1_000_000
        assert r["deferred"] == 1_000_000
        assert r["qty"] + r["deferred"] == 2_000_000

    def test_never_cancels(self):
        for want in (100, 10_000, 1_000_000, 50_000_000):
            r = ES.throttle_qty(want, adv=1e8, participation_cap=0.10, price=10.0)
            assert r["qty"] + r["deferred"] == want

    def test_no_adv_no_limit(self):
        r = ES.throttle_qty(9_999_999, adv=None, participation_cap=0.10, price=10.0)
        assert r["qty"] == 9_999_999 and r["capped"] is False

    def test_zero_qty(self):
        assert ES.throttle_qty(0, adv=1e8, participation_cap=0.1, price=10.0)["qty"] == 0


class TestMakePlan:
    def _plan(self, **kw):
        base = dict(canon="600000.SH", side="buy", total_qty=25_000, price=10.0,
                    adv=1e8, participation_cap=0.10)
        base.update(kw)
        return ES.make_plan(**base)

    def test_no_split_for_typical_order(self):
        p = self._plan()
        assert p["n_slices"] == 1 and "无需拆单" in p["split_reason"]

    def test_qty_sum_equals_total(self):
        p = self._plan(total_qty=25_000)
        assert sum(p["qtys"]) == p["total_qty"]

    def test_split_qty_sum_still_equals_total(self):
        p = self._plan(total_qty=3_000_000, price=10.0, adv=1e6,
                       participation_cap=0.05)
        assert sum(p["qtys"]) == p["total_qty"]
        assert all(q % 100 == 0 for q in p["qtys"])

    def test_cost_identical_recorded(self):
        p = self._plan()
        assert p["cost_identical"] is True
        assert p["cost_unsplit"] == pytest.approx(p["cost_split"])

    def test_no_adv_keeps_single_slice(self):
        p = self._plan(adv=None)
        assert p["n_slices"] == 1 and "不拆" in p["split_reason"]

    def test_odd_lot_reported_not_silently_dropped(self):
        p = self._plan(total_qty=25_050)
        assert p["odd_lot_dropped"] == 50

    def test_plan_has_audit_fields(self):
        p = self._plan()
        for k in ("created_at", "status", "participation", "adv",
                  "participation_cap", "tick_minutes"):
            assert k in p


class TestSliceAdvance:
    def test_next_slice_walks_the_plan(self):
        p = ES.make_plan("x", "buy", 300, 10.0, adv=1e8, participation_cap=0.1)
        p["qtys"] = [100, 100, 100]
        assert ES.next_slice(p)["index"] == 0
        ES.apply_fill(p, 100)
        assert ES.next_slice(p)["index"] == 1
        ES.apply_fill(p, 200)
        assert p["status"] == ES.ST_FILLED
        assert ES.next_slice(p) is None

    def test_partial_fill_moves_within_slice(self):
        p = {"qtys": [100, 100], "filled_qty": 40, "total_qty": 200}
        assert ES.next_slice(p) == {"index": 0, "qty": 60}

    def test_apply_fill_ignores_nonpositive(self):
        p = {"qtys": [100], "filled_qty": 0, "total_qty": 100}
        ES.apply_fill(p, 0)
        assert p["filled_qty"] == 0


class TestComparisonLedger:
    def test_record_and_read(self, tmp_path):
        led = str(tmp_path / "c.jsonl")
        p = ES.make_plan("600000.SH", "buy", 25_000, 10.0, adv=1e8,
                         participation_cap=0.1, day="2026-09-22")
        r = ES.record_comparison(p, ledger=led)
        assert r["ok"] is True
        rows = ES.read_comparison(led)
        assert len(rows) == 1
        assert rows[0]["canon"] == "600000.SH"
        assert rows[0]["cost_identical"] is True

    def test_read_missing_file(self, tmp_path):
        assert ES.read_comparison(str(tmp_path / "none.jsonl")) == []

    def test_read_skips_corrupt_lines(self, tmp_path):
        p = tmp_path / "c.jsonl"
        p.write_text('{"canon":"a"}\nbroken\n[1,2]\n{"canon":"b"}\n', encoding="utf-8")
        rows = ES.read_comparison(str(p))
        assert [r["canon"] for r in rows] == ["a", "b"]

    def test_record_never_raises_on_bad_path(self):
        r = ES.record_comparison({"canon": "x"}, ledger="\0bad\0")
        assert isinstance(r, dict) and "ok" in r

    def test_record_is_json_serialisable(self, tmp_path):
        p = ES.make_plan("x", "buy", 100, 10.0, adv=1e8, participation_cap=0.1)
        r = ES.record_comparison(p, ledger=str(tmp_path / "c.jsonl"))
        json.dumps(r["record"], ensure_ascii=False)


class TestSplitVsUnsplit:
    def test_costs_are_identical(self):
        v = ES.split_vs_unsplit(notional=20_000_000, n_slices=4)
        assert v["identical"] is True
        assert v["delta_bps"] == pytest.approx(0.0)

    def test_verdict_is_explicit(self):
        v = ES.split_vs_unsplit(notional=1e6, n_slices=3)
        assert "完全相同" in v["verdict"]

    def test_says_where_the_difference_is(self):
        v = ES.split_vs_unsplit(notional=1e6, n_slices=3)
        assert "参与率" in v["where_the_difference_is"]


class TestExecGate:
    def test_enabled_reflects_config(self):
        from config import PAPER
        assert EG.enabled() == bool(PAPER.get("exec_split") and
                                    PAPER.get("participation_cap"))

    def test_throttle_passthrough_when_small(self):
        r = EG.throttle("600000.SH", 1000, 10.0, adv=1e8)
        assert r["qty"] == 1000 and r["capped"] is False

    def test_throttle_caps_and_defers(self):
        r = EG.throttle("600000.SH", 2_000_000, 10.0, adv=1e8)
        assert r["capped"] is True
        assert r["qty"] + r["deferred"] == 2_000_000

    def test_throttle_without_adv_is_noop(self):
        r = EG.throttle("600000.SH", 5_000_000, 10.0, adv=None)
        assert r["qty"] == 5_000_000 and r["capped"] is False

    def test_disabled_means_noop(self, monkeypatch):
        import config
        monkeypatch.setitem(config.PAPER, "exec_split", False)
        r = EG.throttle("600000.SH", 5_000_000, 10.0, adv=1e8)
        assert r["qty"] == 5_000_000 and r["capped"] is False

    def test_fetch_adv_returns_dict(self):
        assert isinstance(EG.fetch_adv([]), dict)

    def test_record_skips_uncapped(self):
        """未被限速的单不写账本 —— 每 tick 都写会把对照淹没。"""
        r = EG.record(canon="x", side="buy", wanted=100,
                      throttled={"capped": False}, price=10.0)
        assert r.get("skipped") == "not_capped"

    def test_record_writes_when_capped(self, tmp_path, monkeypatch):
        led = str(tmp_path / "c.jsonl")
        r = EG.record(canon="600000.SH", side="buy", wanted=2_000_000,
                      throttled={"capped": True, "qty": 1_000_000,
                                 "deferred": 1_000_000, "cap_qty": 1_000_000,
                                 "participation": 0.2},
                      price=10.0, adv=1e8, day="2026-09-22", ledger=led)
        assert r["ok"] is True
        assert r["record"]["extra"]["deferred_qty"] == 1_000_000

    def test_record_never_raises(self):
        r = EG.record(canon="x", side="buy", wanted=1,
                      throttled={"capped": True}, price=0.0, adv=None)
        assert isinstance(r, dict)

    def test_state_path_under_data(self):
        assert EG.state_path().replace("\\", "/").endswith("data/exec_deferred.json")
