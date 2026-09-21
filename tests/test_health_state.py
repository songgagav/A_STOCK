# -*- coding: utf-8 -*-
"""health_state 装配器的回归测试（路线图 #2 第一增量）.

阈值证据（2026-09-21 实测, 双峰）:
  健康 tick:  p50 ≈ 7.3ms / p95 ≈ 20ms   (13:08 采样)
  退化 tick:  p50 ≈ 11.9s / p95 ≈ 14.4s  (15:02 采样, AtlasCore 抖动)
  取 p50 > 1s 或 p95 > 2s, 落在两峰之间的空旷地带。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from health_state import assemble  # noqa: E402


def _snap(**kw):
    s = {"tick_ms": None, "freshness_ok": None, "live_source": None, "l3_today": 0}
    s.update(kw)
    return s


class TestNormal:
    def test_all_clean_is_normal(self):
        r = assemble(_snap(tick_ms={"p50": 7.3, "p95": 20.0}, freshness_ok=True,
                           live_source="akshare_spot", l3_today=0))
        assert r == {"state": "NORMAL", "reasons": []}

    def test_none_observables_are_normal(self):
        """全 None（尚未观测到）不得误报 —— 装配器只管"已知的坏", 不管"未知"。"""
        assert assemble({}) == {"state": "NORMAL", "reasons": []}

    def test_unknown_keys_ignored(self):
        r = assemble(_snap(mystery=123, tick_ms={"p50": 5.0}))
        assert r["state"] == "NORMAL"


class TestBoundaries:
    """阈值边界必须精确: 等于上限不算违规(取严格大于)。"""

    def test_p50_at_limit_not_flagged(self):
        assert assemble(_snap(tick_ms={"p50": 1000.0}))["state"] == "NORMAL"

    def test_p50_just_over_limit_flagged(self):
        r = assemble(_snap(tick_ms={"p50": 1000.5}))
        assert r["state"] == "DEGRADED"
        assert any("p50" in x for x in r["reasons"])

    def test_p95_boundary(self):
        assert assemble(_snap(tick_ms={"p95": 2000.0}))["state"] == "NORMAL"
        assert assemble(_snap(tick_ms={"p95": 2000.1}))["state"] == "DEGRADED"


class TestDegradedReasons:
    def test_healthy_fraction_slow_tail_is_flagged(self):
        """**双峰的核心场景**: p50 健康但 p95 卡在慢峰(13:08 实测 p50=7.3/p95=10349)。"""
        r = assemble(_snap(tick_ms={"p50": 7.3, "p95": 10349.3}))
        assert r["state"] == "DEGRADED"
        assert any("p95" in x for x in r["reasons"])

    def test_uniformly_slow_ticks_flagged(self):
        """15:02 实测 p50≈11.9s —— 均匀变慢也必须被抓住。"""
        r = assemble(_snap(tick_ms={"p50": 11887.7, "p95": 14356.4}))
        assert r["state"] == "DEGRADED"
        assert any("p50" in x for x in r["reasons"])

    def test_stale_data_flagged(self):
        r = assemble(_snap(freshness_ok=False))
        assert r["state"] == "DEGRADED"
        assert any("未追平" in x for x in r["reasons"])

    def test_held_static_price_flagged(self):
        r = assemble(_snap(live_source="duckdb_reference_held"))
        assert r["state"] == "DEGRADED"
        assert any("静态" in x for x in r["reasons"])

    def test_pool_missing_is_not_flagged(self):
        """**P2-LIVESRC 语义**: 仅候选池缺价不影响账户估值, 不得判为降级。"""
        assert assemble(_snap(live_source="duckdb_reference_pool"))["state"] == "NORMAL"

    def test_multiple_reasons_accumulate(self):
        r = assemble(_snap(tick_ms={"p50": 5000.0}, freshness_ok=False,
                           live_source="duckdb_reference_held"))
        assert r["state"] == "DEGRADED"
        assert len(r["reasons"]) == 3


class TestHalted:
    def test_l3_forces_halted(self):
        r = assemble(_snap(l3_today=1))
        assert r["state"] == "HALTED"
        assert any("L3" in x for x in r["reasons"])

    def test_l3_precedence_over_degraded(self):
        """HALTED 是最高权重 —— 即使同时有延迟/滞后, 也必须报 HALTED。"""
        r = assemble(_snap(tick_ms={"p50": 9000.0}, freshness_ok=False, l3_today=2))
        assert r["state"] == "HALTED"
        assert len(r["reasons"]) >= 3  # L3 + 两个 DEGRADED 原因都在

    def test_l3_count_in_reason(self):
        r = assemble(_snap(l3_today=3))
        assert "3 个 L3" in r["reasons"][0]


class TestNonNumericDefense:
    def test_string_tick_ms_ignored(self):
        """脏输入不得让装配器崩溃或误判。"""
        assert assemble(_snap(tick_ms={"p50": "abc"}))["state"] == "NORMAL"

    def test_nan_ignored(self):
        import math
        assert assemble(_snap(tick_ms={"p50": float("nan")}))["state"] == "NORMAL"
