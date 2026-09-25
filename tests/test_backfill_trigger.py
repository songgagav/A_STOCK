# -*- coding: utf-8 -*-
"""回填**自动触发**判据的守卫 (2026-09-25, 用户清单第 1、2 项)。

## 判据(用户指定): A 且 B

| 判据 | 含义 |
|---|---|
| **A** 引擎缺口 | `engine_day < expected_day` |
| **B** 存储缺口 | `h5i_watermark < expected_day` |

**两个单条件各自都会误触发**, 故必须**同时**成立:

- 只有 A(引擎缺口但 h5i 已补过) ⇒ **已经补过了, 再补是重复动作**。
  这正是 2026-09-25 补完 09-23/09-24 之后的状态, **也是用户给的验收条件**。
- 只有 B(h5i 落后但引擎已追平) ⇒ 那是**摄入链路**的问题,
  拿替代源补会**掩盖主源故障** —— 该修的是摄入, 不是换个源把水位推上去。

## 另外两条刻意的"不触发"

- **默认关闭**(`BACKFILL_ENABLED=0`): 回填写生产行情库, 属不可逆动作;
- **弱日历 ⇒ 不可判定**: 日历强度非 `official` 时, "最后一个已收盘交易日"会退化成
  "最后一个有数据的日子" ⇒ 落后恒为 0 ⇒ A 恒假、永不触发, **而报告一切正常**。
  这是 DISC-2 ⑥ 的形状, 故显式判为"不可判定"而不是安静地返回"不需要补"。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import backfill_trigger as BT  # noqa: E402


class TestTheAcceptanceCriterion:
    """用户给的验收: `engine_covered_days=09-22, h5i=09-24` 时**不触发**。"""

    def test_already_backfilled_does_not_trigger(self):
        """**核心验收**: A 真(引擎 09-22)但 B 假(h5i 已 09-24) ⇒ 不触发。

        若判据写成"只要引擎有缺口就补", 这里会每轮盘后都去拉一次全市场
        5000+ 只(约 25 分钟), 纯属浪费且反复覆盖同一批数据。
        """
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-24",
                      expected_day="2026-09-24", enabled=True)
        assert r["triggered"] is False
        assert r["action"] == "no_gap"
        assert r["criteria"] == {"A_engine_gap": True, "B_h5i_gap": False}
        assert any("已经补过了" in x for x in r["reasons"]), r["reasons"]

    def test_both_gaps_trigger(self):
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", enabled=True)
        assert r["triggered"] is True and r["action"] == "trigger"
        assert r["criteria"] == {"A_engine_gap": True, "B_h5i_gap": True}
        assert r["missing_days"] == ["20260923", "20260924"], r["missing_days"]

    def test_only_storage_gap_does_not_trigger(self):
        """B 真 A 假 ⇒ 不触发 —— 那是摄入链路问题, 补了会掩盖主源故障。"""
        r = BT.decide(engine_day="2026-09-24", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", enabled=True)
        assert r["triggered"] is False
        assert any("摄入链路" in x for x in r["reasons"]), r["reasons"]

    def test_caught_up_does_not_trigger(self):
        r = BT.decide(engine_day="2026-09-24", h5i_watermark="2026-09-24",
                      expected_day="2026-09-24", enabled=True)
        assert r["triggered"] is False and r["action"] == "no_gap"


class TestDefaultOff:
    def test_disabled_by_default(self):
        """**默认关闭**, 且关闭时不做任何推断(disabled 是第一个分支)。"""
        assert BT.is_enabled({}) is False, "默认必须是关闭"
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", enabled=False)
        assert r["action"] == "disabled" and r["triggered"] is False
        assert r["missing_days"] == [], "关闭时不该算出待补日"

    @pytest.mark.parametrize("val,expect", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("", False), ("no", False),
        (" 1 ", True),          # 前后空白应容忍
        ("2", False),           # 只认显式的正数标志, 不认任意真值
    ])
    def test_env_parsing_is_strict(self, val, expect):
        assert BT.is_enabled({BT.ENABLED_ENV: val}) is expect, val

    def test_lookback_parsing_and_bad_values(self):
        assert BT.lookback_days({BT.LOOKBACK_ENV: "3"}) == 3
        # 坏值不该炸, 也不该变成无穷大
        assert BT.lookback_days({BT.LOOKBACK_ENV: "abc"}) == BT.DEFAULT_LOOKBACK_DAYS
        assert BT.lookback_days({BT.LOOKBACK_ENV: "-5"}) == 0


class TestWeakCalendarIsUndecidable:
    """弱日历必须判为**不可判定**, 而不是安静地"不需要补"。"""

    def test_non_official_calendar_refuses(self):
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", calendar_strength="data_derived")
        assert r["action"] == "refuse_weak_calendar"
        assert r["triggered"] is False
        txt = " ".join(r["reasons"])
        assert "不可判定" in txt, "必须点明这是**不可判定**, 不是不需要补"
        assert "退化" in txt or "恒为 0" in txt, "应说清机理: 落后会恒为 0"

    def test_official_calendar_proceeds(self):
        r = BT.decide(engine_day="2026-09-22", h5i_watermark="2026-09-22",
                      expected_day="2026-09-24", calendar_strength="official")
        assert r["action"] == "trigger"


class TestMissingValuesAreNotGuessed:
    """缺信息时**不补**(不据缺失做动作) —— 与 DISC-1「宁可 None 不猜」同一立场。"""

    @pytest.mark.parametrize("engine_day,h5i_wm,expected", [
        (None, "2026-09-24", "2026-09-24"),
        ("2026-09-22", None, "2026-09-24"),
        ("2026-09-22", "2026-09-24", None),
        ("bad", "2026-09-24", "2026-09-24"),           # 格式不对也算取不到
        ("2026-09-22", "2026-09-24", "not-a-date"),
    ])
    def test_cannot_decide(self, engine_day, h5i_wm, expected):
        r = BT.decide(engine_day=engine_day, h5i_watermark=h5i_wm,
                      expected_day=expected, enabled=True)
        assert r["action"] == "cannot_decide", (engine_day, h5i_wm, expected)
        assert r["triggered"] is False

    def test_d8_normalizes_and_rejects(self):
        assert BT._d8("2026-09-24") == "20260924"
        assert BT._d8("2026/09/24") == "20260924"
        assert BT._d8("20260924") == "20260924"
        assert BT._d8(None) is None
        assert BT._d8("bad") is None
        assert BT._d8("2026-09") is None


class TestMissingDaysUsesTheCalendarNotGuessing:
    """待补交易日必须**取自官方日历**, 不猜。"""

    def test_returns_trading_days_between_watermark_and_expected(self):
        """真实日历上, 09-22 -> 09-24 之间应是 09-23 与 09-24。"""
        got = BT.missing_days("2026-09-22", "2026-09-24")
        assert got == ["20260923", "20260924"], got

    def test_watermark_equals_expected_returns_empty(self):
        assert BT.missing_days("2026-09-24", "2026-09-24") == []

    def test_watermark_ahead_returns_empty(self):
        assert BT.missing_days("2026-09-25", "2026-09-24") == []

    def test_bad_inputs_return_empty_not_raise(self):
        assert BT.missing_days(None, "2026-09-24") == []
        assert BT.missing_days("2026-09-22", None) == []
        assert BT.missing_days("bad", "2026-09-24") == []

    def test_lookback_zero_returns_empty(self):
        """`lookback=0` 表示不回溯 —— 返回空(而不是忽略该限制)。"""
        assert BT.missing_days("2026-09-22", "2026-09-24", lookback=0) == []

    def test_lookback_limits_span(self):
        """很大的 lookback 应把很久以前的空洞都算进来; 很小的只留最近几天。"""
        wide = BT.missing_days("2026-09-01", "2026-09-24", lookback=365)
        narrow = BT.missing_days("2026-09-01", "2026-09-24", lookback=3,
                                 today="2026-09-25")
        assert len(wide) > len(narrow), (len(wide), len(narrow))
        assert all(d >= "20260922" for d in narrow), narrow


class TestRunDailyWiring:
    """接线必须存在, 且**默认关闭**这个语义要在源码里看得到。"""

    def test_run_daily_has_the_step(self):
        src = open(os.path.join(_SRC, "run_daily.py"), encoding="utf-8").read()
        assert 'report["steps"]["backfill_trigger"]' in src, (
            "run_daily 没有把判定结果写进回执 —— 那它就不可见")
        assert "import backfill_trigger" in src
        assert "BACKFILL_ENABLED" in src or "_BT.is_enabled()" in src, (
            "必须走 is_enabled(), 即默认关闭")

    def test_run_daily_uses_engine_bars_sync_baseline(self):
        """判据 B 的 h5i 水位应复用 `engine_bars_sync` 刚报的 `h5i_max_before`。

        为什么强调这一点: 那是**本次摄入前**的水位, 语义正好是判据 B 要的
        "存储缺口"; 若改用摄入**之后**的水位, 会把刚补上的算成"已追平"而漏触发。
        """
        src = open(os.path.join(_SRC, "run_daily.py"), encoding="utf-8").read()
        i = src.find("backfill_trigger")
        assert i > 0
        block = src[max(0, i - 200):i + 2600]
        assert "h5i_max_before" in block, "未复用 h5i_max_before 作判据 B 的输入"

    def test_gate_halt_skips_backfill(self):
        """门禁 HALT 时**不补** —— 数据源不可信时换个源再抓只会灌进不可信数据。"""
        src = open(os.path.join(_SRC, "run_daily.py"), encoding="utf-8").read()
        i = src.find("_bf[\"run\"]")
        assert i > 0
        block = src[max(0, i - 500):i]
        assert "ds_allow" in block, "补数没有被门禁 HALT 拦住"

    def test_interpreter_probe_is_by_import_not_by_path(self):
        """找取数解释器必须**真的 import 一次**, 不能只看文件存在。

        只看路径正是本仓 DISC-1 禁止的"看着像就算" —— 一个存在的解释器未必装了
        baostock, 而失败会以"取数中途报错"的形式出现, 比"找不到解释器"难查得多。
        """
        import inspect
        src = inspect.getsource(BT.__dict__.get("__name__") and __import__(
            "run_daily")._backfill_interpreter)
        assert "import baostock" in src, "应按 import 实测, 而不是按路径判"
        assert "returncode" in src, "应检查 import 的返回码"
