# -*- coding: utf-8 -*-
"""degrade_inputs 回归测试 (P2-DEGRADE-INPUTS)。

锁住的核心: **没有样本时必须如实说"没有样本"**, 不能给一张空表让人误读成
"无异常"; 也**不能把常量成本 7bps 当成实测滑点分布**。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import degrade_inputs as DI  # noqa: E402


def _audit(tmp_path, rows):
    p = tmp_path / "a.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


class TestRejectionStats:
    def test_missing_file_reports_no_sample(self, tmp_path):
        r = DI.rejection_stats(str(tmp_path / "none.jsonl"))
        assert r["n_total"] == 0 and r["rate"] is None
        assert "无订单审计记录" in r["note"]

    def test_rate_counts_reject_and_pending(self, tmp_path):
        fp = _audit(tmp_path, [
            {"action": "execute", "ts": "2026-09-22 09:31:00"},
            {"action": "reject", "ts": "2026-09-22 09:31:05", "reasons": ["非整手"]},
            {"action": "pending_approval", "ts": "2026-09-22 09:31:10",
             "reasons": ["超槽位"]},
            {"action": "execute", "ts": "2026-09-22 09:32:00"},
        ])
        r = DI.rejection_stats(fp)
        assert r["n_total"] == 4 and r["n_blocked"] == 2
        assert r["rate"] == pytest.approx(0.5)
        assert r["by_action"]["execute"] == 2

    def test_reject_and_pending_are_listed_separately(self, tmp_path):
        fp = _audit(tmp_path, [
            {"action": "reject", "ts": "2026-09-22 09:31:00"},
            {"action": "pending_approval", "ts": "2026-09-22 09:31:00"},
        ])
        row = DI.rejection_stats(fp)["by_day"][0]
        assert row["reject"] == 1 and row["pending"] == 1

    def test_by_day_buckets(self, tmp_path):
        fp = _audit(tmp_path, [
            {"action": "reject", "ts": "2026-09-22 09:31:00"},
            {"action": "execute", "ts": "2026-09-23 09:31:00"},
        ])
        r = DI.rejection_stats(fp)
        assert [b["day"] for b in r["by_day"]] == ["2026-09-22", "2026-09-23"]
        assert r["n_days"] == 2

    def test_top_reasons_ranked(self, tmp_path):
        fp = _audit(tmp_path, [
            {"action": "reject", "ts": "2026-09-22 09:31:00", "reasons": ["A"]},
            {"action": "reject", "ts": "2026-09-22 09:32:00", "reasons": ["A"]},
            {"action": "reject", "ts": "2026-09-22 09:33:00", "reasons": ["B"]},
        ])
        top = DI.rejection_stats(fp)["top_reasons"]
        assert top[0][0] == "A" and top[0][1] == 2

    def test_not_calibration_ready_below_min_days(self, tmp_path):
        fp = _audit(tmp_path, [{"action": "reject", "ts": "2026-09-22 09:31:00"}])
        r = DI.rejection_stats(fp)
        assert r["calibration_ready"] is False
        assert "不足以标定" in r["note"]

    def test_calibration_ready_at_min_days(self, tmp_path):
        rows = [{"action": "execute", "ts": f"2026-09-{d:02d} 09:31:00"}
                for d in range(1, DI.MIN_DAYS_FOR_CALIBRATION + 1)]
        r = DI.rejection_stats(_audit(tmp_path, rows))
        assert r["calibration_ready"] is True

    def test_corrupt_lines_skipped(self, tmp_path):
        p = tmp_path / "a.jsonl"
        p.write_text('{"action":"execute","ts":"2026-09-22 09:31:00"}\nbroken\n',
                     encoding="utf-8")
        assert DI.rejection_stats(str(p))["n_total"] == 1

    def test_unknown_action_counted_but_not_blocked(self, tmp_path):
        fp = _audit(tmp_path, [{"action": "weird", "ts": "2026-09-22 09:31:00"}])
        r = DI.rejection_stats(fp)
        assert r["n_total"] == 1 and r["n_blocked"] == 0


class TestSlippageStats:
    def test_no_trades_reports_reason_not_zeros(self):
        s = DI.slippage_stats(trades=[])
        assert s["n_with_slippage"] == 0
        assert s["mean_bps"] is None
        assert "不得把常量 7bps 当作实测分布引用" in s["note"]

    def test_trades_without_decomposition_are_flagged(self):
        """成交存在但没产生滑点分解 => 必须说清原因(常量分支), 而不是给空分布。"""
        s = DI.slippage_stats(trades=[{"price": 10.0, "qty": 100},
                                      {"price": 10.0, "qty": 200}])
        assert s["n_trades"] == 2 and s["n_with_slippage"] == 0
        assert "常量成本分支" in s["note"]

    def test_distribution_from_synthetic_decomposition(self):
        trades = [{"day": f"2026-09-{d:02d}", "price": 10.0, "qty": 1000,
                   "impact_bps": 5.0 + d, "exec_risk_bps": 1.0}
                  for d in range(1, 13)]
        s = DI.slippage_stats(trades=trades)
        assert s["n_with_slippage"] == 12
        assert s["mean_bps"] is not None and s["p50_bps"] is not None
        assert s["p95_bps"] >= s["p50_bps"]
        assert s["calibration_ready"] is True

    def test_impact_or_exec_risk_alone_counts(self):
        s = DI.slippage_stats(trades=[{"day": "2026-09-22", "price": 10.0,
                                       "qty": 1, "exec_risk_bps": 3.0}])
        assert s["n_with_slippage"] == 1 and s["mean_bps"] == pytest.approx(3.0)

    def test_constant_rate_reported(self):
        assert DI.slippage_stats(trades=[])["constant_rate_bps"] == pytest.approx(7.0)

    def test_by_day_grouping(self):
        trades = [{"day": "2026-09-22", "price": 1, "qty": 1, "impact_bps": 2.0},
                  {"day": "2026-09-22", "price": 1, "qty": 1, "impact_bps": 4.0},
                  {"day": "2026-09-23", "price": 1, "qty": 1, "impact_bps": 6.0}]
        s = DI.slippage_stats(trades=trades)
        assert [b["day"] for b in s["by_day"]] == ["2026-09-22", "2026-09-23"]
        assert s["by_day"][0]["mean_bps"] == pytest.approx(3.0)


class TestParticipationStats:
    def test_no_samples(self):
        p = DI.participation_stats(trades=[])
        assert p["n_with_adv"] == 0 and "无样本" in p["note"]

    def test_pairs_trade_with_adv(self):
        trades = [{"canon": "600000.SH", "price": 10.0, "qty": 10_000}]
        p = DI.participation_stats(trades=trades, adv={"600000.SH": 1e8})
        assert p["n_with_adv"] == 1
        # 名义额 10万 / ADV 1亿 = 1e-3
        assert p["participation_p50"] == pytest.approx(1e-3)
        assert p["max_notional"] == pytest.approx(100_000.0)

    def test_unmatched_symbols_skipped(self):
        trades = [{"canon": "999999.SZ", "price": 10.0, "qty": 100}]
        p = DI.participation_stats(trades=trades, adv={"600000.SH": 1e8})
        assert p["n_with_adv"] == 0

    def test_cap_reference_from_config(self):
        assert DI.participation_stats(trades=[])["cap_reference"] is not None


class TestReport:
    def test_report_shape_and_verdict(self, tmp_path):
        r = DI.report(audit=str(tmp_path / "none.jsonl"))
        assert set(r) >= {"rejection", "slippage", "participation", "verdict",
                          "calibration_ready", "why_not_wired"}
        assert r["calibration_ready"] is False
        assert "只采集不判定" in r["verdict"]

    def test_report_is_json_serialisable(self, tmp_path):
        json.dumps(DI.report(audit=str(tmp_path / "none.jsonl")), ensure_ascii=False)

    def test_not_wired_by_default(self):
        """**回归锁**: 这两个量当前**不得**参与任何拦截(阈值未标定)。"""
        assert DI.report()["wired_into_state_machine"] is False

    def test_min_days_declared(self):
        assert DI.MIN_DAYS_FOR_CALIBRATION >= 5

    def test_rejection_ready_requires_both(self, tmp_path):
        rows = [{"action": "execute", "ts": f"2026-09-{d:02d} 09:31:00"}
                for d in range(1, DI.MIN_DAYS_FOR_CALIBRATION + 1)]
        r = DI.report(audit=_audit(tmp_path, rows))
        # 拒单率够天数了, 但滑点仍无样本 => 整体不可标定
        assert r["rejection"]["calibration_ready"] is True
        assert r["slippage"]["calibration_ready"] is False
        assert r["calibration_ready"] is False
