# -*- coding: utf-8 -*-
"""trailing_stop 回归测试 (路线图 #10).

最重要的性质: **移动止损永不比固定止损更早砍仓**。
若这条被破坏, "锁利润"就变成了"改紧风险预算" —— 一个没人批准过的行为变化。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import trailing_stop as TS  # noqa: E402

ENTRY = 10.0
STOP = 0.03        # config.PAPER.stop_loss
GB = 0.06          # config.PAPER.trailing_giveback
FLOOR = 0.09       # config.PAPER.trailing_hard_floor


class TestEntryFloor:
    def test_fixed_line_matches_config_semantics(self):
        assert TS.entry_floor(ENTRY, STOP) == pytest.approx(ENTRY * 0.97, rel=1e-12)

    def test_hard_floor_deepens_the_line(self):
        """hard_floor 比 stop_loss 更深时, 生效的是更深的那条(即"容忍更多")。"""
        assert TS.entry_floor(ENTRY, 0.02, hard_floor=0.05) == pytest.approx(ENTRY * 0.95)

    def test_hard_floor_shallower_than_stop_is_ignored(self):
        """hard_floor 比 stop_loss 浅时不得把止损线**抬高**(那会提前砍仓)。"""
        assert TS.entry_floor(ENTRY, 0.03, hard_floor=0.01) == pytest.approx(ENTRY * 0.97)

    def test_zero_entry_returns_zero(self):
        assert TS.entry_floor(0.0, STOP) == 0.0


class TestTrailLine:
    def test_never_below_fixed_line_when_barely_profitable(self):
        """刚赚一点点时, 移动线可能低于固定线 —— 此时必须取固定线。"""
        peak = ENTRY * 1.01          # +1%
        line = TS.trail_line(ENTRY, peak, GB, stop_loss=STOP)
        assert line == pytest.approx(ENTRY * 0.97, rel=1e-12)
        assert line >= TS.entry_floor(ENTRY, STOP)

    def test_above_fixed_line_once_profit_exceeds_giveback(self):
        peak = ENTRY * 1.20          # +20%
        line = TS.trail_line(ENTRY, peak, GB, stop_loss=STOP)
        assert line == pytest.approx(ENTRY * 1.20 * 0.94, rel=1e-12)
        assert line > TS.entry_floor(ENTRY, STOP)

    def test_line_is_monotone_non_decreasing_in_peak(self):
        prev = 0.0
        for mult in (1.0, 1.02, 1.05, 1.1, 1.2, 1.5, 2.0):
            line = TS.trail_line(ENTRY, ENTRY * mult, GB, stop_loss=STOP)
            assert line >= prev - 1e-12, f"峰值 {mult} 处止损线反而下移"
            prev = line

    def test_crossover_point_is_giveback_over_one_minus_stop(self):
        """移动线超过固定线的临界峰值 = (1-stop)/(1-giveback)。
        这解释了这个参数组合到底意味着什么: 赚到该幅度之前, 移动段不起作用。"""
        peak_star = ENTRY * (1 - STOP) / (1 - GB)
        just_below = TS.trail_line(ENTRY, peak_star * 0.999, GB, stop_loss=STOP)
        just_above = TS.trail_line(ENTRY, peak_star * 1.001, GB, stop_loss=STOP)
        # 临界点下方由固定线主导, 上方由峰值线主导
        assert just_below <= just_above

    def test_peak_below_entry_falls_back_to_fixed(self):
        """数据异常(峰值低于入场价)时退回固定线, 不产生低于成本的荒谬止损线。"""
        line = TS.trail_line(ENTRY, ENTRY * 0.5, GB, stop_loss=STOP)
        assert line == pytest.approx(ENTRY * 0.97, rel=1e-12)

    def test_no_stop_loss_means_pure_trailing(self):
        peak = ENTRY * 1.20
        line = TS.trail_line(ENTRY, peak, GB, stop_loss=None)
        assert line == pytest.approx(peak * (1 - GB), rel=1e-12)

    def test_zero_giveback_and_no_stop_returns_zero(self):
        """返回 0.0 = **显式未启用**, 调用方据此继续走原固定止损判定。"""
        assert TS.trail_line(ENTRY, ENTRY * 1.2, 0.0, stop_loss=None) == 0.0

    def test_invalid_entry_returns_zero(self):
        assert TS.trail_line(0.0, 12.0, GB, stop_loss=STOP) == 0.0
        assert TS.trail_line(float("nan"), 12.0, GB, stop_loss=STOP) == 0.0

    def test_hard_floor_caps_fixed_line_before_peak_takes_over(self):
        """峰值微涨时移动线(9.55)比固定线(9.10)高, 生效的应是移动线 ——
        即地板只是**固定线的容忍上限**, 不是整条止损线的天花板。"""
        peak = ENTRY * 1.016            # 移动线 = 9.55 > 固定线 9.10
        line = TS.trail_line(ENTRY, peak, GB, stop_loss=STOP, hard_floor=FLOOR)
        assert line == pytest.approx(peak * (1 - GB), rel=1e-12)
        assert line > TS.entry_floor(ENTRY, STOP, FLOOR)

    def test_hard_floor_never_deepens_a_profit_locked_line(self):
        """**关键不变量**: 峰值线锁的是**利润**, 必须在成本之上。
        任何"容忍跌多深"的约束都不得把已经锁住的利润重新放开。"""
        line = TS.trail_line(ENTRY, ENTRY * 1.20, 0.02, stop_loss=STOP, hard_floor=FLOOR)
        assert line == pytest.approx(ENTRY * 1.20 * 0.98, rel=1e-12)
        assert line > ENTRY


class TestUpdatePeak:
    def test_first_call_initialises_with_price(self):
        assert TS.update_peak(None, 11.0, ENTRY) == 11.0

    def test_first_call_uses_max_of_price_and_entry(self):
        """建仓后当根 bar 收盘略低于含滑点成本时, peak 不应低于 entry。"""
        assert TS.update_peak(None, 9.99, ENTRY) == ENTRY

    def test_peak_is_monotone(self):
        pk = TS.update_peak(None, 11.0, ENTRY)
        assert TS.update_peak(pk, 10.5, ENTRY) == 11.0
        assert TS.update_peak(pk, 12.0, ENTRY) == 12.0

    def test_invalid_price_keeps_previous_peak(self):
        assert TS.update_peak(11.0, 0.0, ENTRY) == 11.0
        assert TS.update_peak(11.0, float("nan"), ENTRY) == 11.0
        assert TS.update_peak(11.0, -5.0, ENTRY) == 11.0

    def test_none_peak_with_invalid_price_returns_entry(self):
        assert TS.update_peak(None, 0.0, ENTRY) == ENTRY


class TestShouldExit:
    def test_fixed_stop_triggers_below_entry(self):
        v = TS.should_exit(ENTRY, ENTRY * 0.96, None, GB, stop_loss=STOP)
        assert v["exit"] is True
        assert v["trigger"] == "fixed"

    def test_no_exit_just_above_fixed_line(self):
        v = TS.should_exit(ENTRY, ENTRY * 0.975, None, GB, stop_loss=STOP)
        assert v["exit"] is False

    def test_trailing_trigger_after_giveback(self):
        """峰值 +20% 后回吐 6% => 价格跌到峰值*0.94 即出。

        锁定幅度可闭式算出: 线 = entry*1.20*(1-giveback) = entry*1.128 =>
        lock_pct = +12.8%(在 +20% 的峰值上回吐 6pp, 仍锁住 12.8pp)。
        """
        peak = ENTRY * 1.20
        price = peak * 0.94
        v = TS.should_exit(ENTRY, price, peak, GB, stop_loss=STOP)
        assert v["exit"] is True
        assert v["trigger"] == "trailing"
        assert v["lock_pct"] == pytest.approx(12.8, abs=1e-6)
        assert v["lock_pct"] > 0        # 锁住的是利润, 不是亏损

    def test_trailing_not_triggered_at_exactly_one_bar_above(self):
        peak = ENTRY * 1.20
        v = TS.should_exit(ENTRY, peak * 0.94 + 1e-6, peak, GB, stop_loss=STOP)
        assert v["exit"] is False

    def test_drawdown_from_peak_reported(self):
        peak = ENTRY * 1.20
        v = TS.should_exit(ENTRY, peak * 0.94, peak, GB, stop_loss=STOP)
        assert v["drawdown_from_peak"] == pytest.approx(-6.0, abs=0.01)

    def test_trailing_never_exits_earlier_than_fixed(self):
        """核心不变量: 对同一价格序列, 移动止损的触发价 >= 固定止损的触发价。"""
        for peak_mult in (1.0, 1.01, 1.03, 1.05, 1.1, 1.3, 2.0):
            peak = ENTRY * peak_mult
            line = TS.trail_line(ENTRY, peak, GB, stop_loss=STOP)
            assert line >= TS.entry_floor(ENTRY, STOP) - 1e-12

    def test_zero_price_no_exit(self):
        assert TS.should_exit(ENTRY, 0.0, ENTRY * 1.2, GB, stop_loss=STOP)["exit"] is False

    def test_zero_entry_no_exit(self):
        assert TS.should_exit(0.0, 10.0, None, GB, stop_loss=STOP)["exit"] is False

    def test_lock_pct_negative_when_still_underwater(self):
        """固定段触发时锁住的是**亏损**, lock_pct 必须是负数(不许粉饰成 0)。"""
        v = TS.should_exit(ENTRY, ENTRY * 0.96, None, GB, stop_loss=STOP)
        assert v["lock_pct"] < 0

    def test_peak_reported(self):
        peak = ENTRY * 1.2
        v = TS.should_exit(ENTRY, ENTRY * 1.1, peak, GB, stop_loss=STOP)
        assert v["peak"] == pytest.approx(peak)


class TestApplyToPositions:
    def _book(self, price_map, entry=ENTRY):
        return ({"AAA": {"qty": 1000, "avg_cost": entry, "buy_date": "2026-09-01"}},
                lambda c: price_map.get(c, 0.0))

    def test_peak_written_back_monotonically(self):
        pos, px = self._book({"AAA": 11.0})
        TS.apply_to_positions(pos, px, GB, stop_loss=STOP)
        assert pos["AAA"]["peak_price"] == 11.0
        px2 = lambda c: 10.2                      # 回落
        TS.apply_to_positions(pos, px2, GB, stop_loss=STOP)
        assert pos["AAA"]["peak_price"] == 11.0   # 极值不回退

    def test_exit_reported_with_attribution(self):
        pos, px = self._book({"AAA": ENTRY * 1.20 * 0.94})
        pos["AAA"]["peak_price"] = ENTRY * 1.20
        hits = TS.apply_to_positions(pos, px, GB, stop_loss=STOP)
        assert len(hits) == 1
        assert hits[0]["canon"] == "AAA"
        assert hits[0]["trigger"] == "trailing"

    def test_non_exit_not_reported_but_peak_advanced(self):
        pos, px = self._book({"AAA": 10.5})
        hits = TS.apply_to_positions(pos, px, GB, stop_loss=STOP)
        assert hits == []
        assert pos["AAA"]["peak_price"] == 10.5

    def test_missing_price_skipped(self):
        pos, px = self._book({})                   # 无价(停牌)
        assert TS.apply_to_positions(pos, px, GB, stop_loss=STOP) == []

    def test_price_provider_exception_does_not_raise(self):
        pos = {"AAA": {"qty": 1000, "avg_cost": ENTRY, "buy_date": "2026-09-01"}}

        def boom(c):
            raise RuntimeError("feed down")

        assert TS.apply_to_positions(pos, boom, GB, stop_loss=STOP) == []

    def test_empty_positions(self):
        assert TS.apply_to_positions({}, lambda c: 1.0, GB) == []

    def test_zero_avg_cost_skipped(self):
        pos = {"AAA": {"qty": 1000, "avg_cost": 0.0, "buy_date": "2026-09-01"}}
        assert TS.apply_to_positions(pos, lambda c: 10.0, GB) == []

    def test_custom_peak_key(self):
        pos = {"AAA": {"qty": 1, "avg_cost": ENTRY, "peak": 11.0}}
        TS.apply_to_positions(pos, lambda c: 10.5, GB, stop_loss=STOP, peak_key="peak")
        assert pos["AAA"]["peak"] == 11.0

    def test_only_decides_never_sells(self):
        """本模块**只判定不卖出** —— 卖出由 PaperBook/引擎执行, 以便它们各自保留
        T+1/跌停/冷却集等既有约束。故持仓数量在任何情况下都不得被本模块改动。"""
        pos, px = self._book({"AAA": ENTRY * 1.20 * 0.94})
        pos["AAA"]["peak_price"] = ENTRY * 1.20
        before = pos["AAA"]["qty"]
        TS.apply_to_positions(pos, px, GB, stop_loss=STOP)
        assert pos["AAA"]["qty"] == before
