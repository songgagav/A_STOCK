# -*- coding: utf-8 -*-
"""哨兵(b)「供应商未发布」的日历判据守卫 (2026-09-22 P0 批次)。

## 缺陷本体

引擎的「交易日历」**不是日历, 而是它自己数据覆盖的日期集**。实测 2026-09-22
(引擎数据停在 09-18):

```
trading_days: [20260915 .. 20260918]
non_trading : [20260919, 20260920, 20260921, 20260922]
```

而本仓官方日历说 09-21/09-22 **是**交易日(周一/周二, 无法定假日)。
它把「我没有那天的数据」说成了「那天不是交易日」。数据补齐后同一调用立刻恢复
正常 —— **证实它就是"覆盖", 不是"日历"**。

## 三层缺陷(本文件逐层锁住)

1. **命名诱导误用**: `engine_trading_days()` 这个名字让人把它当日历用。
   已改名 `engine_covered_days`(保留旧名兼容), 并在 CLI 里把键名改成
   `engine_days_with_data` / `missing_data_on_trading_days`, 让"没数据"与
   "非交易日"一眼可辨。
2. **日历强度不可见**: `expected_day` 由三层回退链给出(官方 -> daily_bars 派生 ->
   仅周末)。降级到中间层时, "最后一个已收盘交易日"退化成"最后一个有数据的日子",
   于是 `engine_day` 与 `expected_day` **同时**停住 ⇒ **落后恒为 0** ⇒
   哨兵**静默失效**, 而报告上是个漂亮的 0。这是"把日历降级说成刚好追平",
   与引擎"把无数据说成非交易日"是同一族的镜像。
3. **追平被报成不可判定**: `freshness()` 只在"落后"分支赋 `lag_trading_days`,
   追平时该键留 `None`; 而 `None` 在 `classify_engine` 的语义是"**判不出来**"
   ⇒ 一次完全正常的探针被判 `freshness_undeterminable`。实测踩到。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import datasource_gate as G  # noqa: E402
import engine_bars_sync as E  # noqa: E402


def _fresh(**kw):
    """直接调 `freshness`, 不碰网络/引擎。"""
    return E.freshness(**kw)


class TestCalendarProvenance:
    """日历强度必须可判, 且判断依据不能是被覆写过的文件级 `source`。"""

    def test_provenance_reports_strength(self):
        import trading_calendar as TC
        p = TC.calendar_provenance()
        assert p["strength"] in ("official", "data_derived", "weekday_only")
        assert "future_days" in p

    def test_future_days_marker_identifies_official(self, tmp_path, monkeypatch):
        """**核心不变量**: 含未来交易日的日历必然是官方的。

        为什么不能只看文件里的 `source` 字段: 该文件有**两个写入方**,
        导出脚本覆写时把**文件级** `source` 写成了 `h5i:daily_bars.trading_days`
        —— 那是 `trading_days` 键的来源, 不是 `days` 键的来源。
        首版按 `source` 判, 把**官方日历误判成降级**, 会把闸门永久卡在
        `freshness_undeterminable`(实测踩到)。
        """
        import trading_calendar as TC
        p = TC.calendar_provenance()
        if p["future_days"] > 0:
            assert p["strength"] == "official", (
                f"含 {p['future_days']} 个未来交易日, 却判为 {p['strength']!r} —— "
                "数据派生的日历不可能含未来日期")

    def test_data_derived_cannot_have_future_days(self):
        """反证: 若某日历不含未来日期, 它才可能是数据派生的。"""
        import trading_calendar as TC
        p = TC.calendar_provenance()
        if p["strength"] == "data_derived":
            assert p["future_days"] == 0, "数据派生却含未来日期 —— 判据自相矛盾"


class TestFreshnessContract:
    """`freshness()` 的数值契约: 有结论就给数, 没结论才给 None。"""

    def test_caught_up_yields_zero_not_none(self):
        """**追平 => lag=0, 不是 None**。

        原实现只在"落后"分支赋值, 追平时留 `None`; 而 `None` 在消费方语义是
        "判不出来", 于是一次完全正常的探针被判 `freshness_undeterminable`。
        "追平"是**有结论**的(结论就是 0 天)。
        """
        # 用一个必然早于今天的虚构日做"引擎日", 使其 >= expected ⇒ 追平
        import datetime as _dt
        today = _dt.date(2026, 9, 22)
        r = _fresh(engine_day="20991231", today=today)
        assert r["ok"] is True
        assert r["lag_trading_days"] == 0, f"追平应给 0, 实得 {r['lag_trading_days']!r}"

    def test_lagging_yields_positive(self):
        import datetime as _dt
        r = _fresh(engine_day="20200101", today=_dt.date(2026, 9, 22))
        assert r["ok"] is False
        assert isinstance(r["lag_trading_days"], int) and r["lag_trading_days"] > 0

    def test_reports_calendar_provenance(self):
        import datetime as _dt
        r = _fresh(engine_day="20260922", today=_dt.date(2026, 9, 22))
        assert r["calendar_strength"] in ("official", "data_derived", "weekday_only")
        assert "calendar_source" in r


class TestGateRejectsDegradedCalendar:
    """**P0 核心**: 日历降级时不得给出"看起来很健康"的落后天数。"""

    def _probe(self, strength, lag=0):
        return {"ok": True, "day": "20260922", "freshness": {
            "ok": True, "engine_day": "20260922", "expected_day": "20260921",
            "lag_trading_days": lag, "calendar_strength": strength,
            "calendar_source": "test"}}

    def test_official_calendar_passes(self):
        r = G.classify_engine(self._probe("official"))
        assert r["ok"] is True, r

    def test_degraded_calendar_is_undeterminable_not_healthy(self):
        """降级层算出的落后天数**恒偏小**(因为它与 engine_day 同源), 不可信。"""
        for s in ("data_derived", "weekday_only"):
            r = G.classify_engine(self._probe(s))
            assert r["ok"] is False, f"{s} 被判为健康 —— 哨兵又会静默失效"
            assert r["kind"] == "freshness_undeterminable"

    def test_missing_strength_is_backward_compatible(self):
        """老探针没有 `calendar_strength` 字段 —— 不得因此判失败(向后兼容)。"""
        p = {"ok": True, "day": "20260922",
             "freshness": {"ok": True, "engine_day": "20260922",
                           "expected_day": "20260921", "lag_trading_days": 0}}
        r = G.classify_engine(p)
        assert r["ok"] is True, "老格式探针被新判据拒了 —— 破坏向后兼容"

    def test_freshness_undeterminable_is_not_healthy(self):
        """缺 freshness 仍是不可判定, 不能当健康。"""
        r = G.classify_engine({"ok": True, "day": "20260922"})
        assert r["ok"] is False and r["kind"] == "freshness_undeterminable"


class TestNamingStopsImpersonatingACalendar:
    """命名本身是缺陷的一部分 —— 它诱导调用方把"覆盖"当日历。"""

    def test_covered_days_is_the_real_name(self):
        assert hasattr(E, "engine_covered_days")
        assert callable(E.engine_covered_days)

    def test_deprecated_alias_still_works(self):
        """旧名保留(兼容既有调用点/外部脚本), 但必须标注已弃用。"""
        assert hasattr(E, "engine_trading_days")
        doc = (E.engine_trading_days.__doc__ or "")
        assert "弃用" in doc or "deprecated" in doc.lower(), \
            "旧名没标注弃用 —— 后人会继续按名字误解"
        assert "engine_covered_days" in doc

    def test_cli_keys_say_what_they_mean(self):
        """CLI 输出必须能把"没数据"与"非交易日"分开。

        原键名 `trading_days` / `non_trading` 会被读成"这些天是交易日/那些天不是",
        而实测它把引擎缺的 09-21/09-22 列进 `non_trading` —— 真实故障被读成"放假"。
        """
        src = open(os.path.join(_REPO, "src", "engine_bars_sync.py"),
                   encoding="utf-8").read()
        for key in ("engine_days_with_data", "engine_days_without_data",
                    "official_trading_days_in_range", "missing_data_on_trading_days"):
            assert key in src, f"CLI 缺少说实话的键 {key}"
        assert "不可读作" in src, "缺少对旧键名误导性的说明"
