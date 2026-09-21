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


class TestFreshnessAttribution:
    """**归因纪律**（2026-09-21 修）：探针失败不得冒充"厂商未发布数据"。

    实测踩到的坑: 本机 shell 没继承 User 级 STOCKDB_ROOT，`stock_sdk` 导入失败，
    第一增量把它装配成"厂商未发布当日数据" —— 范畴错误，运维会去等厂商。
    """

    def test_config_error_not_blamed_on_vendor(self):
        r = assemble(_snap(
            freshness_ok=False,
            engine_error="厂商 SDK 不可用: ModuleNotFoundError: No module named 'stock_sdk'",
            probe_error_kind="config"))
        assert r["state"] == "DEGRADED"          # 监测盲区仍要可见
        txt = " ".join(r["reasons"])
        assert "无法判定" in txt
        assert "厂商未发布" not in txt            # 核心: 不冒充滞后
        assert "STOCKDB_ROOT" in txt              # 且给出可行动信息

    def test_unreachable_error_reason(self):
        r = assemble(_snap(
            freshness_ok=False,
            engine_error="引擎连接失败(127.0.0.1:7899): ConnectionError",
            probe_error_kind="unreachable"))
        assert r["state"] == "DEGRADED"
        assert any("不可达" in x for x in r["reasons"])
        assert not any("厂商未发布" in x for x in r["reasons"])

    def test_unknown_error_reported_verbatim(self):
        r = assemble(_snap(engine_error="WeirdError: boom", probe_error_kind="unknown"))
        assert any("WeirdError" in x for x in r["reasons"])

    def test_genuine_lag_still_blames_vendor(self):
        """**不能因为修归因就丢掉真滞后**: 探针成功但没追平 => 仍说"厂商未发布"。"""
        r = assemble(_snap(freshness_ok=False))
        assert r["state"] == "DEGRADED"
        assert "厂商未发布" in " ".join(r["reasons"])

    def test_kind_inferred_when_absent(self):
        """旧快照/外部写入没带 kind 时也要能分类。"""
        r = assemble(_snap(freshness_ok=False, engine_error="No module named 'stock_sdk'"))
        assert "STOCKDB_ROOT" in " ".join(r["reasons"])

    def test_classify_helper(self):
        from health_state import _classify_probe_error as C
        assert C("厂商 SDK 不可用: ModuleNotFoundError: No module named 'stock_sdk'") == "config"
        assert C("引擎连接失败(127.0.0.1:7899): ConnectionError") == "unreachable"
        assert C("随机错误") == "unknown"
        assert C("") == "unknown"
        assert C(None) == "unknown"


class TestPublishRead:
    """发布者/读取者分离：读取方必须**快**且**不编造状态**。

    读取方（面板每 3 秒轮询）绝不能跑引擎探针（实测 1.71s，最坏数十秒）。
    """

    def test_missing_snapshot_is_not_invented(self, tmp_path):
        from health_state import read_published
        r = read_published(str(tmp_path / "nope.json"))
        assert r["available"] is False
        assert r["state"] is None                 # 核心: 不凭空报 NORMAL
        assert "不存在" in r["error"]

    def test_publish_is_atomic_and_leaves_no_tmp(self, tmp_path):
        """注入载荷走真实写盘路径：os.replace 原子替换 + 不留 .tmp 垃圾。"""
        from health_state import publish, read_published
        fp = tmp_path / "state.json"
        publish(str(fp), payload={"state": "NORMAL", "reasons": [],
                                  "ts": "2026-09-21 10:00:00", "observed": {"x": 1}})
        assert fp.exists()
        assert not (tmp_path / "state.json.tmp").exists()
        # 覆盖写不得残留旧内容
        publish(str(fp), payload={"state": "HALTED", "reasons": ["L3"], "ts": "2026-09-21 10:05:00"})
        r = read_published(str(fp), now=__import__("datetime").datetime(2026, 9, 21, 10, 6, 0))
        assert r["state"] == "HALTED" and r["reasons"] == ["L3"]
        assert 30 < r["age_s"] < 120 and r["stale"] is False

    def test_age_and_stale_boundary(self, tmp_path):
        from datetime import datetime, timedelta
        from health_state import publish, read_published
        fp = tmp_path / "state.json"
        now = datetime(2026, 9, 21, 12, 0, 0)
        publish(str(fp), payload={"state": "DEGRADED", "reasons": [],
                                  "ts": (now - timedelta(seconds=1800)).strftime("%Y-%m-%d %H:%M:%S")})
        assert read_published(str(fp), max_age_s=1800, now=now)["stale"] is False   # 恰好等于不算陈旧
        assert read_published(str(fp), max_age_s=1799, now=now)["stale"] is True
        # stale 只做标记, **不得改写状态语义**（"停流多久算事故"归 #4 看门狗）
        assert read_published(str(fp), max_age_s=1, now=now)["state"] == "DEGRADED"

    def test_corrupt_snapshot_does_not_crash(self, tmp_path):
        from health_state import read_published
        fp = tmp_path / "bad.json"
        fp.write_text("{ 半截 json", encoding="utf-8")
        r = read_published(str(fp))
        assert r["available"] is False and r["state"] is None
        assert "不可解析" in r["error"]

    def test_unparseable_ts_is_stale_not_fresh(self, tmp_path):
        """时间戳读不出来时**不能声称新鲜** —— 未知一律按陈旧, 并说明原因。"""
        import json
        from health_state import read_published
        fp = tmp_path / "state.json"
        fp.write_text(json.dumps({"state": "NORMAL", "ts": "??"}), encoding="utf-8")
        r = read_published(str(fp))
        assert r["available"] is True and r["state"] == "NORMAL"
        assert r["stale"] is True and r["age_s"] is None
        assert "时间戳" in r["error"]

    def test_reader_needs_no_engine_env(self, tmp_path, monkeypatch):
        """读取方**不依赖任何环境变量** —— 这是发布/读取分离的存在理由。"""
        from health_state import publish, read_published
        monkeypatch.delenv("STOCKDB_ROOT", raising=False)
        fp = tmp_path / "state.json"
        publish(str(fp), payload={"state": "NORMAL", "reasons": [], "ts": "2026-09-21 10:00:00"})
        assert read_published(str(fp), now=__import__("datetime").datetime(2026, 9, 21, 10, 1, 0))["state"] == "NORMAL"
