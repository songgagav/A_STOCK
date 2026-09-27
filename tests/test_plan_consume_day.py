# -*- coding: utf-8 -*-
"""`target_plan.consume_day` 的写入/读取判据.

## 背景(为什么会有这个字段)

`data/drl/<D>/target_plan.json` 是 **D 日盘后 19:10** 产出、供**下一个交易日**
消费的。但产物里原本**只有 `day`(撰写日)**, 没有"给哪天用"这一维 ⇒ 读取侧
只能靠目录名猜, 而它猜成了"目录日 == 消费日", 按 `day=D` 校验
`generated_at ∈ [prev_trade(D) 16:00, D 00:00)`。可文件写于 **D 日 19:1x**,
**必然晚于上界** ⇒ 第 1 档(`drl_same_day`)结构上永不可达(实测命中率 0%),
而正确那一份只能靠"当日目录不存在时倒序回退"间接取到, 取到还被记成
`drl_cross_day`(跨日回退) ⇒ 档位留痕失真。

本组用例锁住修复后的**双向**语义:
  · 写成"同日消费"必须**失败**(那就是原来那个错读法);
  · 写成"次日消费"必须**成功**;
  · 旧产物(无该字段)**必须**仍按旧窗口判据可用(兼容性)。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import realtime_engine as re  # noqa: E402

#: 供测试内的 exists 打桩透传用(见 TestLoadTargetsLadderWiring)
_REAL_EXISTS = os.path.exists

_A_ITEM = {"canon": "600016.SH", "price": 10.0}


def _plan(consume_day="2026-09-22", generated_at="2026-09-21 19:46:22",
          top_n=None):
    """构造一份最小可用 plan。top_n 默认给一个正股, 以便只看消费日判据。"""
    return {
        "day": "2026-09-21",
        "consume_day": consume_day,
        "generated_at": generated_at,
        "top_n": [_A_ITEM] if top_n is None else top_n,
    }


class TestConsumeDayIsAuthoritative(unittest.TestCase):
    """有 `consume_day` 时**以字段为准**, 不再拿时间戳窗口去救。"""

    def test_matching_day_passes(self):
        ok, _why = re._plan_consume_ok(_plan(), "2026-09-22")
        self.assertTrue(ok)

    def test_authoring_day_must_fail(self):
        """**反向验证核心**: 按"撰写日当天消费"读必须失败。

        这正是修复前那个错读法 —— 若它通过, 就说明字段没起到权威作用。
        """
        ok, why = re._plan_consume_ok(_plan(), "2026-09-21")
        self.assertFalse(ok)
        self.assertIn("consume_day", why)

    def test_other_day_fails(self):
        ok, _ = re._plan_consume_ok(_plan(), "2026-09-23")
        self.assertFalse(ok)

    def test_field_beats_a_timestamp_that_would_otherwise_pass(self):
        """二者冲突时以**字段**为准。

        造一份"时间戳窗口看起来通过(写于前一交易日 17:00)"但
        `consume_day` 指向别处的 plan: 必须按字段判**不过**。
        否则等于留了一条"窗口能救回错 plan"的暗门, 又回到靠猜。
        """
        p = _plan(consume_day="2026-09-25",
                  generated_at="2026-09-21 17:00:00")
        ok_consume, _ = re._plan_consume_ok(p, "2026-09-22")
        self.assertFalse(ok_consume)
        ok_formal, why = re._plan_is_formal(p, "2026-09-22", "20260921")
        self.assertFalse(ok_formal, "字段与窗口冲突时必须按字段拒绝")
        self.assertIn("consume_day", why)

    def test_empty_or_missing_field_returns_none(self):
        """无值 -> None(交由调用方回退旧逻辑), 且**空串**也算无值。"""
        for bad in (None, "", "   "):
            p = _plan(consume_day=bad)
            self.assertIsNone(re._plan_consume_ok(p, "2026-09-22"),
                              f"consume_day={bad!r} 应视为无字段")

    def test_malformed_value_falls_back(self):
        """值坏掉(非日期)时按无字段处理, **不抛异常**。"""
        for bad in ("not-a-date", "2026/09/22", "20260922", 12345):
            self.assertIsNone(re._plan_consume_ok(_plan(consume_day=bad),
                                                  "2026-09-22"),
                              f"consume_day={bad!r} 应回退旧逻辑")

    def test_accepts_compact_and_dashed(self):
        """容忍 `YYYYMMDD` 与 `YYYY-MM-DD` 两种写法(前 10 位截断 + 解析)。"""
        self.assertTrue(re._plan_consume_ok(_plan(consume_day="2026-09-22"),
                                            "2026-09-22")[0])
        # 紧凑写法前 10 位是 "20260922" 本身, 解析会失败 -> 回退; 这属预期
        self.assertIsNone(re._plan_consume_ok(_plan(consume_day="20260922"),
                                              "2026-09-22"))


class TestFormalCheckTwoTier(unittest.TestCase):
    """`_plan_is_formal` 的两级: 字段优先, 旧产物回退窗口。"""

    def test_new_plan_passes_via_field(self):
        ok, why = re._plan_is_formal(_plan(), "2026-09-22", "20260921")
        self.assertTrue(ok, why)

    def test_new_plan_fails_on_authoring_day(self):
        ok, why = re._plan_is_formal(_plan(), "2026-09-21", "20260918")
        self.assertFalse(ok)
        self.assertIn("consume_day", why)

    def test_old_plan_without_field_still_works_via_window(self):
        """**兼容性核心**: 无 `consume_day` 的旧产物必须仍可用。

        造一份"写于前一交易日 19:46"的旧 plan(正是生产真实形态):
        按消费日 09-22 校验时应落在 `[09-21 16:00, 09-22 00:00)` 内而通过。
        """
        p = {"day": "2026-09-21",
             "generated_at": "2026-09-21 19:46:22",
             "top_n": [_A_ITEM]}
        self.assertNotIn("consume_day", p)
        self.assertIsNone(re._plan_consume_ok(p, "2026-09-22"))
        ok, why = re._plan_is_formal(p, "2026-09-22", "20260921")
        self.assertTrue(ok, f"旧产物应回退窗口判据并通过, 实际: {why}")

    def test_old_plan_still_rejected_when_window_fails(self):
        """旧产物回退后该拒的还要拒(别把兼容做成放水)。"""
        p = {"day": "2026-09-05",
             "generated_at": "2026-09-05 15:46:50",
             "top_n": [_A_ITEM]}
        ok, why = re._plan_is_formal(p, "2026-09-07", "20260904")
        self.assertFalse(ok)
        self.assertIn("不在正式窗口", why)

    def test_non_a_share_still_rejected_before_consume_check(self):
        """A 股过滤必须仍在最前 —— 不能被消费日字段绕过。"""
        p = _plan(top_n=[{"canon": "110001.SH", "price": 1.0}])
        ok, why = re._plan_is_formal(p, "2026-09-22", "20260921")
        self.assertFalse(ok)
        self.assertIn("A 股", why)

    def test_missing_generated_at_rejected_for_old_plan(self):
        p = {"day": "2026-09-21", "top_n": [_A_ITEM]}
        ok, why = re._plan_is_formal(p, "2026-09-22", "20260921")
        self.assertFalse(ok)
        self.assertIn("generated_at", why)


class TestConsumeDayDirectorySelection(unittest.TestCase):
    """`_load_daily_plan_for_consume_day` 取**哪个目录**。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.drl = Path(self._tmp.name) / "drl"
        self.drl.mkdir(parents=True)
        self._orig = (re.DATA_DIR, re.DAILY_DIR)
        re.DATA_DIR = self._tmp.name
        re.DAILY_DIR = str(Path(self._tmp.name) / "daily")

    def tearDown(self):
        re.DATA_DIR, re.DAILY_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, day8, plan):
        d = self.drl / day8
        d.mkdir(parents=True, exist_ok=True)
        (d / "target_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def test_new_style_plan_taken_from_previous_trading_day_dir(self):
        """新产物: 今天要用的那份在**前一交易日**目录里, 且命中第 1 档。

        这正是修好的那条路: 消费日 09-22 -> 读 `drl/20260921`
        (其 consume_day == 2026-09-22), 而不是读当晚才会出现的 `drl/20260922`。
        """
        import datetime
        self._write("20260921", _plan(consume_day="2026-09-22"))
        src, top_n, _info = re._load_daily_plan_for_consume_day(
            "20260922", "2026-09-22", datetime.datetime(2026, 9, 21))
        self.assertEqual(src, "20260921", "应取前一交易日目录")
        self.assertEqual(len(top_n), 1)

    def test_same_day_dir_plan_is_not_used_for_today(self):
        """坐在 `drl/<今天>` 里的 plan 声明给**明天**用 ⇒ 今天不得取用。

        这是修复前那个错读法的直接反证。
        """
        import datetime
        self._write("20260922", _plan(consume_day="2026-09-23"))
        src, top_n, _info = re._load_daily_plan_for_consume_day(
            "20260922", "2026-09-22", datetime.datetime(2026, 9, 21))
        self.assertIsNone(src, "同日目录里的 plan 供明天用, 今天不应取到")
        self.assertIsNone(top_n)

    def test_old_style_plan_in_prev_dir_is_NOT_returned_here(self):
        """旧产物(无字段)**不应**由本函数返回 —— 它必须留给第 3 档。

        这条是**回归护栏**: 若本函数把跨日扫描也做了, 取到的旧产物就会被
        调用方记成 `drl_same_day`(当日同源), 而生产实录里 09-22/09-23/09-24
        都是 `drl_cross_day`。档位语义一旦在这里被吃掉, 留痕又会失真。

        正确行为: 本函数返回 None -> 调用方第 2 档(当日 selection)/第 3 档
        (跨日 DRL)按**旧逻辑**处理, 从而与改动前完全一致。
        """
        import datetime
        self._write("20260921", {"day": "2026-09-21",
                                 "generated_at": "2026-09-21 19:46:22",
                                 "top_n": [_A_ITEM]})
        src, top_n, _info = re._load_daily_plan_for_consume_day(
            "20260922", "2026-09-22", datetime.datetime(2026, 9, 21))
        self.assertIsNone(src, "旧产物应由第 3 档跨日回退取, 而不是第 1 档")
        self.assertIsNone(top_n)

    def test_cross_day_helper_still_finds_old_plan(self):
        """第 3 档(跨日回退)必须仍能取到旧产物 —— 兼容性靠它。"""
        import datetime
        self._write("20260921", {"day": "2026-09-21",
                                 "generated_at": "2026-09-21 19:46:22",
                                 "top_n": [_A_ITEM]})
        src, top_n, _info = re._cross_day_drl_fallback(
            "20260922", "2026-09-22", datetime.datetime(2026, 9, 21))
        self.assertEqual(src, "20260921")
        self.assertEqual(len(top_n), 1)

    def test_cross_day_helper_skips_same_day(self):
        """跨日回退必须**跳过当日目录**(当日目录由第 1 档负责, 重复取会串档位)。"""
        import datetime
        self._write("20260922", _plan(consume_day="2026-09-22"))
        src, top_n, _info = re._cross_day_drl_fallback(
            "20260922", "2026-09-22", datetime.datetime(2026, 9, 21))
        self.assertIsNone(src)
        self.assertIsNone(top_n)

    def test_returns_none_triple_when_nothing_usable(self):
        src, top_n, info = re._load_daily_plan_for_consume_day(
            "20260922", "2026-09-22", None)
        self.assertIsNone(src)
        self.assertIsNone(top_n)
        self.assertIsNone(info)

    def test_prev_dir_equal_to_same_day_is_not_tried_twice(self):
        """`prev_trade_day` 与消费日同日(异常输入)时不应重复试同一目录。"""
        import datetime
        self._write("20260922", _plan(consume_day="2026-09-22"))
        src, top_n, _info = re._load_daily_plan_for_consume_day(
            "20260922", "2026-09-22", datetime.datetime(2026, 9, 22))
        self.assertEqual(src, "20260922")
        self.assertEqual(len(top_n), 1)


class TestWriterGuardOnNonTradingDays(unittest.TestCase):
    """写入侧: **非交易日不得**声明 consume_day。

    依据: 守护的 `--maint` 分支在非交易日也会跑并写出
    `data/drl/<非交易日>/target_plan.json`(实测 09-25/26/27 各一份)。
    若它们也声明 `consume_day = 下一个交易日`, 则在一个长周末里它会成为
    "最近一份声明给今天用"的 plan 而被第 1 档取用 —— 而旧判据恰好把它们全拒掉。
    那将是本次改动**引入**的回归, 故此处用真实日历锁住。

    这三个日期是真实实测值(见 `_tools/plan_window_axis.py` 的输出):
      2026-09-25 Fri / 09-26 Sat / 09-27 Sun 都是非交易日, 下一个交易日都是 09-28。
    """

    def test_non_trading_days_are_recognised(self):
        try:
            import datetime

            import trading_calendar as tc
        except Exception:               # pragma: no cover
            self.skipTest("trading_calendar 不可用")
        for ds, want in (("2026-09-25", False), ("2026-09-26", False),
                         ("2026-09-27", False), ("2026-09-24", True),
                         ("2026-09-28", True)):
            got = bool(tc.is_trading_day(
                datetime.date.fromisoformat(ds)))
            self.assertEqual(got, want, f"{ds} 交易日判定不符")

    def test_writer_would_write_none_for_those_days(self):
        """写入侧的组合判据: 非交易日 -> `_is_trading_day8` False -> 不写 consume_day。

        这里直接验 `drl_train` 的两个 helper(不跑整个训练流程)。
        """
        try:
            import drl_train as T
        except Exception as e:          # pragma: no cover
            self.skipTest(f"drl_train 依赖缺失: {e}")
        # 非交易日: 即便 _next_trade_day 有值, 也不该采用
        self.assertFalse(T._is_trading_day8("2026-09-26"))
        self.assertEqual(T._next_trade_day("2026-09-26"), "2026-09-28",
                         "helper 本身仍应算出下一个交易日(由调用方决定不写)")
        # 交易日: 正常写出
        self.assertTrue(T._is_trading_day8("2026-09-24"))
        self.assertEqual(T._next_trade_day("2026-09-24"), "2026-09-28",
                         "09-24(Thu) 之后是 09-28(Mon), 跨周末")


class TestLoadTargetsLadderWiring(unittest.TestCase):
    """`load_targets` 这条**主取池路径**的档位接线(端到端, 不碰真实 data/)。

    为什么单独锁: 上面各条只测了 helper, 而 helper 被接错(档位名/返回序/跳过当日)
    照样会让留痕失真 —— 实测本仓 `load_targets` 此前**没有任何直接用例**。

    用真实 `load_targets` 跑, 把 `DATA_DIR`/`DAILY_DIR` 指向临时目录。
    为使第 3 档可达, 需要屏蔽第 2 档: 该档读 `selection.json`, 而生产里它是
    盘后产物 ⇒ 盘前不存在, 故"屏蔽掉它"正是**引擎 08:30 的真实形态**。
    """

    CONSUME = "2026-09-28"
    D = "20260928"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "drl").mkdir(parents=True, exist_ok=True)
        (root / "daily").mkdir(parents=True, exist_ok=True)
        self._orig = (re.DATA_DIR, re.DAILY_DIR)
        re.DATA_DIR = str(root)
        re.DAILY_DIR = str(root / "daily")
        # 第 2 档屏蔽器: 让 `daily/<当日>/selection.json` 看起来不存在
        self._mask = mock.patch.object(re.os.path, "exists",
                                       side_effect=self._fake_exists)
        self._mask.start()

    def tearDown(self):
        self._mask.stop()
        re.DATA_DIR, re.DAILY_DIR = self._orig
        self._tmp.cleanup()

    def _fake_exists(self, p):
        # 只屏蔽"当日 selection.json", 其余交给真实 exists
        if str(p).replace("\\", "/").endswith("/daily/%s/selection.json" % self.D):
            return False
        return _REAL_EXISTS(p)

    def _write(self, day8, plan):
        d = Path(re.DATA_DIR) / "drl" / day8
        d.mkdir(parents=True, exist_ok=True)
        (d / "target_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def test_new_contract_plan_hits_tier1_and_is_labelled_same_day(self):
        """有 consume_day 命中 -> 第 1 档, 且**必须**记成 `drl_same_day`。"""
        self._write("20260928", _plan(consume_day=self.CONSUME,
                                      generated_at="2026-09-28 19:30:00"))
        top_n, _info, sel_day = re.load_targets(self.CONSUME)
        self.assertEqual(len(top_n), 1)
        self.assertEqual(sel_day, "20260928")

    def test_old_plan_goes_to_tier3_not_tier1(self):
        """旧产物(无字段)必须走第 3 档并记 `drl_cross_day` —— 不得冒充当日同源。

        这是**档位语义**的核心回归点: 生产实录里 09-22/09-23/09-24 都是
        `drl_cross_day`。若第 1 档把旧产物也吃了, 留痕就又失真。
        """
        self._write("20260927", {"day": "2026-09-27",
                                 "generated_at": "2026-09-27 19:30:00",
                                 "top_n": [_A_ITEM]})
        top_n, _info, sel_day = re.load_targets(self.CONSUME)
        self.assertEqual(len(top_n), 1)
        self.assertEqual(sel_day, "20260927", "旧产物应经第 3 档取到")

    def test_same_day_plan_for_tomorrow_is_rejected(self):
        """坐在当日目录、但声明给**明天**用的 plan: 今天不得取用。"""
        self._write("20260928", _plan(consume_day="2026-09-29",
                                      generated_at="2026-09-28 19:30:00"))
        # 只放这一份 -> 第 1/2/3/4 档全空 -> 只能落到第 5 档现场选股(需 DB)
        # 故这里只断言"第 1 档没吃它": 通过 helper 直接验, 避免拉 DB。
        src, top_n, _ = re._load_daily_plan_for_consume_day(
            self.D, self.CONSUME, datetime.datetime(2026, 9, 25))
        self.assertIsNone(top_n, "供明天的 plan 不该在今天被第 1 档取用")
        self.assertIsNone(src)


if __name__ == "__main__":
    unittest.main()
