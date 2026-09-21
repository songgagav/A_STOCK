# -*- coding: utf-8 -*-
"""pretrade_gates 回归测试 (路线图 #15).

本文件锁住两件容易被"顺手改坏"的事:

1. **缺字段 => skip, 不是 fail**。清单是新加的一道闸门, 若它在拿不到字段时
   默认拒单, 那么任何一次上游字段改名都会让系统**静默停手** —— 这比不设闸门
   危险得多。
2. **`data_lag_days` 是自然日, 不是交易日差**。本仓脚本已实测写明
   (09-18 -> 09-21 = 3)。若按 "== 0" 直接判定, 每个周一/节后第一天都会拒单。
   这条有真实生产数据佐证, 见 `test_real_plan_lag_is_recorded_not_failed`。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import pretrade_gates as PG  # noqa: E402

ORDER = {"symbol": "600000", "side": "buy", "qty": 700, "price": 10.0}
CTX = {"equity": 100000.0, "cash": 20000.0, "peak_equity": 100000.0,
       "day_start_equity": 100000.0}
TH = {"max_position_pct": 0.08, "max_drawdown": 0.08,
      "max_daily_loss": 0.08, "max_data_lag_days": None}


def _by_name(res, name):
    return next(c for c in res["checks"] if c["name"] == name)


class TestPositionGate:
    def test_within_limit_passes(self):
        r = PG.evaluate(ORDER, CTX, **TH)          # 7000 / 100000 = 7%
        assert _by_name(r, PG.CHK_POSITION)["status"] == PG.PASS

    def test_exactly_at_limit_passes(self):
        o = dict(ORDER, qty=800)                   # 恰好 8.00%
        assert _by_name(PG.evaluate(o, CTX, **TH), PG.CHK_POSITION)["status"] == PG.PASS

    def test_over_limit_fails(self):
        o = dict(ORDER, qty=900)                   # 9%
        r = PG.evaluate(o, CTX, **TH)
        assert _by_name(r, PG.CHK_POSITION)["status"] == PG.FAIL
        assert r["ok"] is False

    def test_float_boundary_tolerance(self):
        """目标权重恰好等于上限时不得因浮点误差被拒。"""
        o = {"symbol": "x", "side": "buy", "qty": 0.1, "price": 80000.0}  # 8.0000000%
        assert _by_name(PG.evaluate(o, CTX, **TH), PG.CHK_POSITION)["status"] == PG.PASS

    def test_default_threshold_is_none_so_not_judged(self):
        """**回归锁**: 默认不得拿 `max_single_weight`(=8% 压重线)当下单前硬上限。
        用 8% 当硬上限会拒掉正常补仓 —— 目标池不足 9 只时等权 band 就 >8%,
        而 `target_weighting` 给单票的目标上限本就是 11.5%。"""
        assert PG.thresholds_from_paper()["max_position_pct"] is None

    def test_big_order_is_measured_but_not_rejected_by_default(self):
        """3000 股 @10 = 30% 权益: 属"高危单转人工"的量级, 不是"硬合规拒绝"。
        默认必须记为 skip(记数), 把裁决权留给 classify_risk 与人工审批 ——
        本仓红线是"高危单不执行, 转人工", 不是"直接拒"。"""
        o = dict(ORDER, qty=3000)
        r = PG.evaluate(o, CTX, **PG.thresholds_from_paper())
        assert r["ok"] is True
        c = _by_name(r, PG.CHK_POSITION)
        assert c["status"] == PG.SKIP
        assert c["measured"] == 30.0
        assert "30.00%" in c["detail"]

    def test_optional_threshold_forces_the_check(self, monkeypatch):
        monkeypatch.setenv("PRETRADE_MAX_POSITION_PCT", "0.30")
        th = PG.thresholds_from_paper()
        assert th["max_position_pct"] == 0.30
        # 30% 恰好不超 => 通过; 40% 则拒
        assert _by_name(PG.evaluate(dict(ORDER, qty=3000), CTX, **th),
                        PG.CHK_POSITION)["status"] == PG.PASS
        assert _by_name(PG.evaluate(dict(ORDER, qty=4000), CTX, **th),
                        PG.CHK_POSITION)["status"] == PG.FAIL

    def test_bad_env_value_falls_back_to_not_judged(self, monkeypatch):
        monkeypatch.setenv("PRETRADE_MAX_POSITION_PCT", "abc")
        assert PG.thresholds_from_paper()["max_position_pct"] is None

    def test_no_threshold_skips(self):
        th = dict(TH, max_position_pct=None)
        assert _by_name(PG.evaluate(ORDER, CTX, **th), PG.CHK_POSITION)["status"] == PG.SKIP

    def test_missing_equity_skips(self):
        ctx = {k: v for k, v in CTX.items() if k != "equity"}
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_POSITION)["status"] == PG.SKIP

    def test_zero_equity_skips_not_divides_by_zero(self):
        ctx = dict(CTX, equity=0.0)
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_POSITION)["status"] == PG.SKIP

    def test_bool_equity_is_not_a_number(self):
        """bool 是 int 的子类; True 混进来会被当成 1 元权益而拒绝一切。"""
        ctx = dict(CTX, equity=True)
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_POSITION)["status"] == PG.SKIP


class TestDrawdownGate:
    def test_no_drawdown_passes(self):
        assert _by_name(PG.evaluate(ORDER, CTX, **TH), PG.CHK_DRAWDOWN)["status"] == PG.PASS

    def test_just_under_limit_passes(self):
        ctx = dict(CTX, drawdown_pct=-7.99)
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_DRAWDOWN)["status"] == PG.PASS

    def test_at_limit_fails_strictly_less(self):
        """用户清单口径是 "< 8%", 故恰好 -8.00% 应判 fail(严格小于)。"""
        ctx = dict(CTX, drawdown_pct=-8.0)
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_DRAWDOWN)["status"] == PG.FAIL

    def test_over_limit_fails(self):
        ctx = dict(CTX, drawdown_pct=-9.5)
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_DRAWDOWN)["status"] == PG.FAIL

    def test_derived_from_peak_when_pct_absent(self):
        ctx = {"equity": 91000.0, "peak_equity": 100000.0}
        r = PG.evaluate(ORDER, ctx, **TH)
        assert _by_name(r, PG.CHK_DRAWDOWN)["status"] == PG.FAIL

    def test_missing_both_skips(self):
        r = PG.evaluate(ORDER, {"equity": 100000.0}, **TH)
        assert _by_name(r, PG.CHK_DRAWDOWN)["status"] == PG.SKIP


class TestIcGate:
    def test_normal_passes(self):
        ctx = dict(CTX, regime="normal")
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_IC_GATE)["status"] == PG.PASS

    def test_risk_fails(self):
        ctx = dict(CTX, regime="risk")
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_IC_GATE)["status"] == PG.FAIL

    def test_risk_case_insensitive(self):
        ctx = dict(CTX, regime="RISK")
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_IC_GATE)["status"] == PG.FAIL

    def test_caution_is_not_blocking(self):
        """caution 档仍按暴露系数缩量加仓, 不是冻结 —— 不得拦。"""
        ctx = dict(CTX, regime="caution")
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_IC_GATE)["status"] == PG.PASS

    def test_freeze_flag_fails_even_in_normal(self):
        ctx = dict(CTX, regime="normal", freeze_new_buys=True)
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_IC_GATE)["status"] == PG.FAIL

    def test_unavailable_regime_passes(self):
        """门控不可用时既有行为是"放行默认", 清单不得反向把它变成停手。"""
        ctx = dict(CTX, regime="unavailable")
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_IC_GATE)["status"] == PG.PASS

    def test_missing_regime_skips(self):
        assert _by_name(PG.evaluate(ORDER, CTX, **TH), PG.CHK_IC_GATE)["status"] == PG.SKIP


class TestFreshnessGate:
    def test_default_threshold_is_none_so_skipped(self):
        th = PG.thresholds_from_paper()
        assert th["max_data_lag_days"] is None

    def test_lag_zero_passes_when_strict(self):
        ctx = dict(CTX, data_lag_days=0)
        r = PG.evaluate(ORDER, ctx, **dict(TH, max_data_lag_days=0))
        assert _by_name(r, PG.CHK_FRESHNESS)["status"] == PG.PASS

    def test_lag_one_fails_when_strict(self):
        ctx = dict(CTX, data_lag_days=1)
        r = PG.evaluate(ORDER, ctx, **dict(TH, max_data_lag_days=0))
        assert _by_name(r, PG.CHK_FRESHNESS)["status"] == PG.FAIL

    def test_non_strict_records_measurement_instead_of_failing(self):
        """默认(非严格)下, 实测滞后必须**写进 detail 与 measured** —— 看得见,
        但不据此拒单。"""
        ctx = dict(CTX, data_lag_days=3)
        c = _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_FRESHNESS)
        assert c["status"] == PG.SKIP
        assert c["measured"] == 3
        assert "3" in c["detail"]

    def test_strict_mode_switchable_by_env(self, monkeypatch):
        monkeypatch.setenv("PRETRADE_STRICT_FRESHNESS", "1")
        assert PG.thresholds_from_paper()["max_data_lag_days"] == 0
        monkeypatch.delenv("PRETRADE_STRICT_FRESHNESS")
        assert PG.thresholds_from_paper()["max_data_lag_days"] is None

    def test_missing_lag_skips(self):
        assert _by_name(PG.evaluate(ORDER, CTX, **TH), PG.CHK_FRESHNESS)["status"] == PG.SKIP

    def test_real_plan_lag_is_recorded_not_failed(self):
        """**真实产物回归**: 取本机最新的 target_plan.json, 用真实 data_lag_days
        走一遍清单, 断言**不会**被拒 —— 若将来有人把该项默认改成 0, 这条会红。

        依据: scripts/check_daemon_first_day.py 明写 data_lag_days 是自然日差
        "09-18 -> 09-21 = 3, 故不会是 0/1"。"""
        drl = os.path.join(_REPO, "data", "drl")
        if not os.path.isdir(drl):
            pytest.skip("本机无 data/drl")
        plans = sorted((d for d in os.listdir(drl)
                        if d.isdigit() and os.path.isfile(
                            os.path.join(drl, d, "target_plan.json"))), reverse=True)
        if not plans:
            pytest.skip("无 target_plan.json")
        plan = json.load(open(os.path.join(drl, plans[0], "target_plan.json"),
                              encoding="utf-8"))
        lag = plan.get("data_lag_days")
        if lag is None:
            pytest.skip("该 plan 未写 data_lag_days")
        th = PG.thresholds_from_paper()
        ctx = dict(CTX, data_lag_days=lag, regime="normal")
        r = PG.evaluate(ORDER, ctx, **th)
        assert r["ok"] is True, f"自然日滞后 {lag} 被误判为停手依据: {r['failed']}"

    def test_same_lag_would_fail_if_threshold_were_naively_zero(self):
        """反向锁: 同一个自然日滞后在严格模式下**确实**会 fail —— 证明上面那条
        skip 是刻意的取舍, 而不是判据根本没生效。"""
        ctx = dict(CTX, data_lag_days=3)
        r = PG.evaluate(ORDER, ctx, **dict(TH, max_data_lag_days=0))
        assert r["ok"] is False


class TestDailyLossGate:
    def test_flat_day_passes(self):
        assert _by_name(PG.evaluate(ORDER, CTX, **TH), PG.CHK_DAILY_LOSS)["status"] == PG.PASS

    def test_derived_from_day_start_equity(self):
        ctx = dict(CTX, equity=91000.0)
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_DAILY_LOSS)["status"] == PG.FAIL

    def test_explicit_pct_used_first(self):
        ctx = dict(CTX, daily_loss_pct=-2.0, day_start_equity=1.0)
        assert _by_name(PG.evaluate(ORDER, ctx, **TH), PG.CHK_DAILY_LOSS)["status"] == PG.PASS

    def test_missing_both_skips(self):
        assert _by_name(PG.evaluate(ORDER, {"equity": 100000.0}, **TH),
                        PG.CHK_DAILY_LOSS)["status"] == PG.SKIP


class TestSellSide:
    def test_sell_all_checks_skipped(self):
        """离场单不进任何组合闸门 —— 把卖出也闸住等于把风险锁在仓里。"""
        r = PG.evaluate({"symbol": "600000", "side": "sell", "qty": 100, "price": 10.0},
                        dict(CTX, regime="risk", drawdown_pct=-50.0), **TH)
        assert r["ok"] is True
        assert len(r["skipped"]) == 5

    def test_sell_with_absurd_size_still_ok(self):
        r = PG.evaluate({"symbol": "600000", "side": "sell", "qty": 10 ** 9, "price": 10.0},
                        CTX, **TH)
        assert r["ok"] is True


class TestResultShape:
    def test_all_five_checks_always_present(self):
        r = PG.evaluate(ORDER, CTX, **TH)
        assert [c["name"] for c in r["checks"]] == [
            PG.CHK_POSITION, PG.CHK_DRAWDOWN, PG.CHK_IC_GATE,
            PG.CHK_FRESHNESS, PG.CHK_DAILY_LOSS]

    def test_ok_is_true_with_skips(self):
        r = PG.evaluate(ORDER, CTX, **TH)
        assert r["ok"] is True and r["skipped"]

    def test_reasons_only_for_failures(self):
        r = PG.evaluate(ORDER, dict(CTX, regime="risk"), **TH)
        assert len(r["reasons"]) == len(r["failed"]) == 1

    def test_empty_order_does_not_raise(self):
        r = PG.evaluate({}, {}, **TH)
        assert isinstance(r["ok"], bool)

    def test_none_ctx_does_not_raise(self):
        assert isinstance(PG.evaluate(ORDER, None, **TH)["ok"], bool)

    def test_summary_line_mentions_failed_and_skipped(self):
        r = PG.evaluate(ORDER, dict(CTX, regime="risk"), **TH)
        s = PG.summary_line(r)
        assert PG.CHK_IC_GATE in s and PG.CHK_FRESHNESS in s

    def test_summary_line_on_empty(self):
        assert PG.summary_line({}) == "清单=未执行"

    def test_json_serialisable(self):
        json.dumps(PG.evaluate(ORDER, CTX, **TH), ensure_ascii=False)


class TestThresholdsFromPaper:
    def test_reuses_existing_config_numbers_for_same_category(self):
        """回撤类阈值直接复用既有熔断线(**同范畴**: 都是组合级行为拦截)。"""
        from config import PAPER
        th = PG.thresholds_from_paper()
        assert th["max_drawdown"] == PAPER["portfolio_drawdown"]
        assert th["max_daily_loss"] == PAPER["portfolio_drawdown"]

    def test_position_threshold_is_not_taken_from_trim_line(self):
        """**范畴不能混**: `max_single_weight` 是集中度**压回线**(超标才减仓),
        不是下单前硬上限。取它当拒单线会拒掉正常补仓, 故必须为 None。"""
        from config import PAPER
        th = PG.thresholds_from_paper()
        assert th["max_position_pct"] is None
        assert PAPER["max_single_weight"] is not None   # 确认该项确实存在于配置里

    def test_keys_complete(self):
        assert set(PG.thresholds_from_paper()) == {
            "max_position_pct", "max_drawdown", "max_daily_loss", "max_data_lag_days"}
