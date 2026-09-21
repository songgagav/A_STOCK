# -*- coding: utf-8 -*-
"""盘前健康检查两项修复的回归测试（P2-HEALTHNOISE / P1-PREMARKET-FALSEFAIL）.

1) arcticdb 三项检查: 未安装 => SKIP(退役, 不计入 FAIL/告警), 且**留痕**说明未执行。
2) daily_bars_freshness: 判据基准从"日历"改为**可达上限**(厂商是否发布)。
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from premarket_healthcheck import (  # noqa: E402
    _arcticdb_retired, _freshness_verdict, _norm_day, _retired_record,
    check_arcticdb, check_arcticdb_rw, check_strategy_diagnostics,
)

EXPECTED = date(2026, 9, 21)     # 最后一个已收盘交易日(日历口径)
VENDOR_LATE = "20260918"         # 厂商自身也只发到 09-18


class TestArcticdbRetired:
    """退役组件不存在是**预期事实**, 不是故障 —— 它曾以 3 条 FAIL 淹没真 FAIL。"""

    def test_retired_detected_when_module_absent(self):
        import importlib.util
        if importlib.util.find_spec("arcticdb") is not None:
            pytest.skip("本机装有 arcticdb, 未启用退役分支")
        assert _arcticdb_retired() is True

    @pytest.mark.parametrize("fn", [check_arcticdb, check_arcticdb_rw, check_strategy_diagnostics])
    def test_checks_report_skip_not_fail(self, fn):
        r = fn()
        assert r["status"] == "SKIP", f"{r['name']} 应降级为 SKIP, 实际 {r['status']}"
        assert r["detail"]["retired"] is True

    def test_skip_detail_says_check_did_not_run(self):
        """SKIP 不是"检查通过", 而是"没执行" —— 必须写明, 否则读者会误以为查过了。"""
        assert "本项未执行检查" in _retired_record("x", 0.0)["detail"]["note"]

    def test_skip_is_not_counted_as_failure_by_consumers(self):
        """面板的 worst 只认 FAIL/CRITICAL/WARN => SKIP 不参与告警(这是降噪的关键)。"""
        st = check_arcticdb()["status"]
        assert st not in ("FAIL", "CRITICAL", "WARN")


class TestFreshnessVerdict:
    """**核心**: 厂商没发 ≠ 我们滞后。修复前后者被报成 FAIL 并叫人去查正常的入库管道。"""

    def test_vendor_published_but_we_lag_is_fail(self):
        """真滞后必须仍然 FAIL —— 修误报不能把真故障也一起放过。"""
        s, b = _freshness_verdict(date(2026, 9, 18), EXPECTED, "20260921")
        assert s == "FAIL" and "落后可达上限" in b

    def test_vendor_also_late_is_ok_not_fail(self):
        """**本条就是修复的靶心**: 无可达数据缺失 => OK, 而不是 FAIL。"""
        s, b = _freshness_verdict(date(2026, 9, 18), EXPECTED, VENDOR_LATE)
        assert s == "OK"
        assert "厂商亦未发布" in b and "不是入库滞后" in b

    def test_probe_unavailable_is_warn_not_fail(self):
        """归因纪律: 探不到不等于厂商滞后, 也不等于我们滞后 => 不猜, 降为 WARN。"""
        s, b = _freshness_verdict(date(2026, 9, 18), EXPECTED, None, "ModuleNotFoundError: stock_sdk")
        assert s == "WARN"
        assert "无法判定可达上限" in b and "不能断定是主源滞后" in b

    def test_one_day_behind_ceiling_is_warn(self):
        assert _freshness_verdict(date(2026, 9, 18), date(2026, 9, 19), "20260919")[0] == "WARN"

    def test_in_sync_is_ok(self):
        assert _freshness_verdict(EXPECTED, EXPECTED, "20260921")[0] == "OK"

    def test_engine_never_ahead_of_calendar(self):
        """上限取 min(日历, 厂商) —— 厂商标了未来日期也不能把期望抬高。"""
        s, _ = _freshness_verdict(date(2026, 9, 21), EXPECTED, "20260930")
        assert s == "OK"

    def test_unparseable_inputs_fail_loudly(self):
        assert _freshness_verdict(None, EXPECTED, VENDOR_LATE)[0] == "FAIL"
        assert _freshness_verdict("???", EXPECTED, VENDOR_LATE)[0] == "FAIL"


class TestNormDay:
    """实测踩到: 引擎返回字符串 '20260918', 而期望是 datetime.date => min() 抛 TypeError。"""

    @pytest.mark.parametrize("raw,expect", [
        ("20260918", date(2026, 9, 18)),
        ("2026-09-18", date(2026, 9, 18)),
        (date(2026, 9, 18), date(2026, 9, 18)),
        (None, None),
        ("???", None),
        ("20261340", None),          # 非法月日
    ])
    def test_normalization(self, raw, expect):
        assert _norm_day(raw) == expect

    def test_mixed_types_do_not_raise(self):
        """这条直接锁住那个 TypeError 回归。"""
        assert _freshness_verdict("2026-09-18", EXPECTED, "20260918")[0] == "OK"
