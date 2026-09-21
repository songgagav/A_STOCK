# -*- coding: utf-8 -*-
"""09:25 信号冻结看门狗的回归测试（P0-FREEZE-0925 纯告警版）.

锁三件事: ① 三项要求各自的告警真的会触发; ② **正常路径绝不误报**(否则又是狼来了);
③ 纯告警版**不改变任何信号**(本模块只返回告警, 无任何丢弃/改写逻辑)。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from signal_freeze_watch import (  # noqa: E402
    BUDGET_S, DEADLINE, evaluate, events_path, observe, recent,
)


def _codes(r):
    return sorted(a["code"] for a in r["alerts"])


def _at(h, m, s=0):
    return datetime(2026, 9, 22, h, m, s)


class TestHealthyPath:
    def test_same_day_fast_pool_is_quiet(self):
        """**正常路径必须零告警** —— 池命中的日子里这是亚秒级且同源, 报它就是噪音。"""
        r = evaluate("selection_same_day", pool_size=10, elapsed_s=0.4, finished_at=_at(8, 31))
        assert r["alerts"] == [] and r["worst"] == "OK"

    def test_drl_same_day_is_quiet(self):
        assert evaluate("drl_same_day", pool_size=10, elapsed_s=1.2, finished_at=_at(9, 0))["worst"] == "OK"


class TestRequirement1PoolGuarantee:
    """要求①: 池必达 —— 落进第⑤档或空池必须有人知道。"""

    def test_onsite_select_alerts(self):
        r = evaluate("onsite_select", pool_size=8, elapsed_s=120, finished_at=_at(8, 40))
        assert _codes(r) == ["pool_fallback"]
        assert "现场选股" in r["alerts"][0]["detail"]

    def test_cross_day_fallback_alerts(self):
        assert _codes(evaluate("selection_cross_day", pool_size=10, elapsed_s=0.5,
                               finished_at=_at(8, 31))) == ["pool_fallback"]

    def test_empty_pool_is_critical(self):
        r = evaluate("onsite_select", pool_size=0, elapsed_s=10, finished_at=_at(8, 40))
        assert r["worst"] == "CRITICAL"
        assert "pool_empty" in _codes(r)


class TestRequirement2Budget:
    """要求②: 耗时预算。阈值依据 = 用户验收构造 5.8min 须告警 + 本仓实测健康带 30–160s。"""

    def test_user_acceptance_case_5p8min_alerts(self):
        """用户明确给定的验收: 『5.8min 未完成』须告警。348s > 300s。"""
        r = evaluate("onsite_select", pool_size=8, elapsed_s=348, finished_at=_at(8, 45))
        assert "over_budget" in _codes(r)
        assert "348" in [a["detail"] for a in r["alerts"] if a["code"] == "over_budget"][0]

    def test_measured_slow_path_does_not_alert(self):
        """第⑤档实测 30–160s, 仍在预算内 => 不报(预算不是拿来喊正常慢的)。"""
        assert "over_budget" not in _codes(
            evaluate("onsite_select", pool_size=8, elapsed_s=160, finished_at=_at(8, 40)))

    def test_boundary_is_strictly_greater(self):
        assert "over_budget" not in _codes(evaluate("selection_same_day", pool_size=5,
                                                    elapsed_s=BUDGET_S, finished_at=_at(8, 30)))
        assert "over_budget" in _codes(evaluate("selection_same_day", pool_size=5,
                                                elapsed_s=BUDGET_S + 0.1, finished_at=_at(8, 30)))

    def test_missing_elapsed_does_not_alert(self):
        assert "over_budget" not in _codes(evaluate("selection_same_day", pool_size=5))


class TestRequirement3Deadline:
    """要求③: 逾期告警(仅报告, 不丢弃/不改写信号)。"""

    def test_after_deadline_alerts(self):
        r = evaluate("selection_same_day", pool_size=10, elapsed_s=1, finished_at=_at(9, 26))
        assert r["past_deadline"] is True
        assert "past_deadline" in _codes(r)

    def test_before_deadline_quiet(self):
        assert evaluate("selection_same_day", pool_size=10, elapsed_s=1,
                        finished_at=_at(9, 24))["past_deadline"] is False

    def test_exactly_at_deadline_is_not_late(self):
        assert evaluate("selection_same_day", pool_size=10, elapsed_s=1,
                        finished_at=_at(DEADLINE.hour, DEADLINE.minute))["past_deadline"] is False

    def test_alert_says_signal_is_not_discarded(self):
        """让读者确信: 这条告警**不会**害他丢掉本次信号。"""
        r = evaluate("selection_same_day", pool_size=10, elapsed_s=1, finished_at=_at(9, 30))
        d = [a["detail"] for a in r["alerts"] if a["code"] == "past_deadline"][0]
        assert "不丢弃" in d and "不改写" in d


class TestObservationLedger:
    def test_observe_records_to_chained_ledger(self, tmp_path):
        fp = str(tmp_path / "ev.jsonl")
        r = observe("onsite_select", pool_size=8, elapsed_s=348,
                    finished_at=_at(8, 45), path=fp, now=_at(8, 45))
        assert "over_budget" in _codes(r)
        rows = recent(path=fp)
        assert len(rows) == 1 and rows[0]["rung"] == "onsite_select"
        rec = json.loads(open(fp, encoding="utf-8").read().splitlines()[0])
        assert "hash" in rec and "prev" in rec          # 走 #6 哈希链

    def test_observe_never_raises(self, tmp_path):
        r = observe(None, pool_size="abc", elapsed_s="xyz",
                    path=str(tmp_path / "no" / "dir" / "x.jsonl"))
        assert isinstance(r, dict)

    def test_recent_on_missing_file_is_empty(self, tmp_path):
        assert recent(path=str(tmp_path / "absent.jsonl")) == []

    def test_default_path_under_data(self):
        assert events_path().replace("\\", "/").endswith("data/signal_freeze_events.jsonl")


class TestNoBehaviorChange:
    """纯告警版的**本质**: 结果里只有告警与观测值, 没有任何"该怎么处置信号"的指令。"""

    def test_result_has_no_directive_fields(self):
        r = evaluate("onsite_select", pool_size=0, elapsed_s=999, finished_at=_at(9, 30))
        assert set(r) == {"alerts", "worst", "rung", "pool_size", "elapsed_s",
                          "past_deadline", "finished_at"}
        for a in r["alerts"]:
            assert set(a) == {"code", "severity", "detail"}
