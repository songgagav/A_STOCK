# -*- coding: utf-8 -*-
"""组合**建仓节奏**守卫 (2026-09-23 新增, 用户决策「方案 B: 分 2-3 天建仓 + 显式记录」)。

## 为什么需要这个文件

2026-09-23 实测: 虚拟盘收盘只有 **4/10** 个目标持仓、现金占比 **68.98%**,
而回执里**没有任何字段**说这件事。根因是两条:

1. **交易侧**: `realtime_engine._rebalance()` 末段的推进判据只有
   `gate_open and self._to_used > 0` —— 即「当天有过成交」就锁定调仓间隔。
   于是当天花掉 20% 换手预算买进 2 个槽位后, 窗口被推进、锁 3 天;
   此后全天 **670 个 tick** 全部只走 `调仓间隔未到, 本轮仅止损/风控`。
   **用一次未完成的建仓换来了 3 天静默期。**
2. **可见性**: 建仓进度不进回执 ⇒ 「半仓」与「满仓」在回执上**长得一样**,
   会被误读成"策略弱", 而实际是"节奏被预算与间隔门夹住"。

故本文件锁两件事: **窗口何时算办结**(`_construction_incomplete`),
以及**进度必须被记录**(`run_daily.portfolio_construction`)。

## 这些用例刻意不碰真实进程

`realtime_engine` 的 `__init__` 需要 vnpy 等重依赖; 而被测方法只用到
`self.targets / self.pb.positions / self._cooled` 三个属性, 故用**桩对象**绑定
未绑定方法直接调 —— 这样测试既快又不依赖交易环境, 且失败原因单一。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import realtime_engine as RE          # noqa: E402
import run_daily as RD                # noqa: E402


class _EngineStub:
    """只带被测方法所需属性的最小桩。"""

    _construction_incomplete = RE.RealtimeEngine._construction_incomplete
    _rebal_window_decision = RE.RealtimeEngine._rebal_window_decision

    def __init__(self, targets, held, cooled=()):
        self.targets = [{"canon": c} for c in targets]
        self.pb = type("PB", (), {"positions": {c: {} for c in held}})()
        self._cooled = set(cooled)
        self._construction_stall = 0
        self._construction_last_filled = -1


class TestConstructionIncomplete:
    def test_reports_missing_when_under_built(self):
        """未建满必须报出**待买清单**, 且原因里带得出可核对的数字。

        用 2026-09-23 的真实情形: 目标 10 只、实际持有 4 只(其中 2 只已不在目标池)。
        """
        s = _EngineStub(
            targets=["301520.SZ", "600127.SH", "600721.SH", "603248.SH", "600448.SH",
                     "300642.SZ", "603082.SH", "000823.SZ", "002589.SZ", "600630.SH"],
            held=["301520.SZ", "600127.SH", "002902.SZ", "002172.SZ"],
        )
        why = s._construction_incomplete()
        assert why, "半仓却判为已建满 —— 窗口会被错误推进"
        assert "目标 10 只" in why and "待买 8 只" in why
        # 不在目标池的持仓不能被算作"已建"的一部分
        assert "持有 2 只" in why, why

    def test_none_when_fully_built(self):
        s = _EngineStub(targets=["A", "B", "C"], held=["A", "B", "C"])
        assert s._construction_incomplete() is None

    def test_all_cooled_counts_as_done(self):
        """待买的**全部**在冷却中 => 视为办结。

        冷却(`_cooled`)是"当日刻意不回补"的风控, 不是"建仓没做完"。
        若把它算作未完成, 窗口会一直敞着 —— 那是把风控当成了拖延。
        """
        s = _EngineStub(targets=["A", "B", "C"], held=["A"], cooled=("B", "C"))
        assert s._construction_incomplete() is None

    def test_partially_cooled_still_incomplete(self):
        """只有**部分**待买在冷却 => 仍算未完成(还有能买的没买)。"""
        s = _EngineStub(targets=["A", "B", "C"], held=["A"], cooled=("B",))
        assert s._construction_incomplete() is not None

    def test_missing_data_does_not_block_progress(self):
        """取不到数据 => 返回 None(按已建满处理), **不**无限期敞着窗口。

        判据来自本仓纪律「缺字段 = 无信息, 不据此拒单」: 不能因为算不出来
        就把调仓间隔形同虚设。这里用"没有 pb 属性"造出异常路径。
        """
        s = _EngineStub(targets=["A"], held=["A"])
        del s.pb
        assert s._construction_incomplete() is None

    def test_empty_targets_does_not_block(self):
        s = _EngineStub(targets=[], held=["A"])
        assert s._construction_incomplete() is None


class TestStallGuard:
    """**防止窗口永远敞着** (用户 2026-09-23 要求)。

    用户的原话: 「如果 `_construction_incomplete()` 因某只标的一直在冷却中而永远
    返回 True, 系统会反复尝试买入、反复失败、永不锁 3 天。这比『提前锁 3 天』更危险。」

    ## 实现与该设想有一处**刻意的差异**, 必须说明

    用户举的场景是「待买**全部**在冷却中」—— 而那种情形 `_construction_incomplete()`
    **已经返回 None**(冷却属风控, 不是建仓卡住), 所以从设计上就不会落入停滞分支。
    **真正的停滞场景是**: 待买标的**可买却一直买不进**(被等权槽位上限 / 小单门槛 /
    涨停 等挡住), 此时窗口会一直敞着。

    ## 判据用"尝试次数"而非"日历天数" —— 这一点决定了保护是否真的有效

    用户建议写的是「连续 N **天**未推进」。但 `self._last_rebal_day` 是**进程态**
    (初始化为 None), 引擎每天 08:30 由守护重启 ⇒ 任何跨日计数器都会归零,
    "5 天"保护永远攒不满 5 天(每天都是第 1 天), **形同虚设**。
    故改为**进程内连续失败尝试数**, 与 `FAILS_TO_HALT`「连续 N 次」同一条思路。
    """

    def test_forced_advance_after_limit(self):
        """连续 N 次"无进展"后必须强制推进(这是用户要的验收)。"""
        s = _EngineStub(targets=["A", "B", "C"], held=["A"])
        limit = RE.CONSTRUCTION_STALL_LIMIT
        seen = []
        for _ in range(limit + 3):
            adv, why = s._rebal_window_decision(True, s._construction_incomplete(), 3)
            seen.append(adv)
        assert seen[0] is False, "第一次就推进 —— 保护会退化回原行为"
        # 最后一次必然已强制推进(计数已超限)
        assert seen[-1] is True, "达到停滞上限仍未强制推进 —— 窗口会永远敞着"
        assert any(a is True for a in seen), seen
        assert "强制推进" in why

    def test_progress_resets_the_stall_counter(self):
        """中途有进展必须把计数归零 —— 否则正常的分批建仓会被误判为停滞。

        这一条是**保护本身的安全阀**: 方案 B 下组合要 2-3 天建满, 每天都在进展;
        若计数不归零, 正常节奏也会被强制推进。
        """
        s = _EngineStub(targets=["A", "B", "C"], held=["A"])
        for _ in range(3):
            s._rebal_window_decision(True, s._construction_incomplete(), 3)
        assert s._construction_stall > 0, "前置状态没造出来: 计数未累积"
        s.pb.positions["B"] = {}          # 买进一只 = 有进展
        adv, _ = s._rebal_window_decision(True, s._construction_incomplete(), 3)
        assert s._construction_stall == 0, "有进展后计数未归零, 正常建仓会被误判停滞"
        assert adv is False, "才建了一半就不该推进"

    def test_all_cooled_is_not_a_stall(self):
        """「待买全部在冷却中」**不是**停滞 ---- 直接按已办结处理。

        用户设想的正是这个场景; 实现上它在 `_construction_incomplete()` 就已返回 None,
        故 `advance=True` 且**不需要**等 N 次。这比"等 5 次再推"更干净:
        冷却当天本来就不该回补, 窗口没必要为它开着。
        """
        s = _EngineStub(targets=["A", "B", "C"], held=["A"], cooled=("B", "C"))
        assert s._construction_incomplete() is None
        adv, why = s._rebal_window_decision(True, None, 3)
        assert adv is True and "已建到目标" in why
        assert s._construction_stall == 0, "这种情形不该消耗停滞计数"

    def test_gate_closed_never_advances(self):
        s = _EngineStub(targets=["A", "B"], held=["A"])
        adv, why = s._rebal_window_decision(False, "x", 3)
        assert adv is False and "间隔未到" in why

    def test_fully_built_advances(self):
        s = _EngineStub(targets=["A", "B"], held=["A", "B"])
        adv, _ = s._rebal_window_decision(True, s._construction_incomplete(), 3)
        assert adv is True

    def test_limit_is_documented_and_reachable(self):
        """上限必须是**可达的小整数**, 且理由写在源码里(不是魔法值)。"""
        assert isinstance(RE.CONSTRUCTION_STALL_LIMIT, int)
        assert 2 <= RE.CONSTRUCTION_STALL_LIMIT <= 20, RE.CONSTRUCTION_STALL_LIMIT
        src = open(os.path.join(_SRC, "realtime_engine.py"), encoding="utf-8").read()
        assert "CONSTRUCTION_STALL_LIMIT = 5" in src
        assert "跨日计数" in src, "必须写明为何不用日历天数(否则后人会'顺手'改成天数)"


class TestPortfolioConstructionReceipt:
    """`run_daily.portfolio_construction()` 必须把建仓进度**如实**写进回执。"""

    def test_reads_real_live_state_and_reports_progress(self):
        """对**真实** live_state.json 跑一次, 断言字段齐全且互相自洽。

        为什么不造桩数据: 这个函数的价值就是"把线上真实状态说清楚",
        桩数据只能验证我自己的假设。真实文件在 CI 里可能不存在, 故缺文件时 skip
        —— 但 skip 会掩盖问题, 所以**同时**断言: 文件存在时字段必须齐全。
        """
        lv = os.path.join(_REPO, "data", "live_state.json")
        if not os.path.isfile(lv):
            pytest.skip("本机没有 data/live_state.json(非生产 checkout)")
        r = RD.portfolio_construction()
        assert r.get("ok") is True, r
        for k in ("target_count", "current_count", "pending_buys",
                  "daily_turnover_budget", "deployment_ratio"):
            assert k in r, f"缺字段 {k}"
        # 自洽性: 目标 = 已建 + 待买; 部署率与现金率互补
        assert r["target_count"] == r["current_count"] + r["pending_buys"]
        assert r["deployment_ratio"] + float(r["cash_ratio"] or 0) == pytest.approx(1.0, abs=0.02)
        if r["pending_buys"]:
            assert r["reason"], "有未建满却没有 reason —— 半仓会变成不可解释的状态"

    def test_does_not_invent_numbers_when_source_missing(self, monkeypatch):
        """取不到数据时必须返回 `ok=False` + error, **不许**编造 0。"""
        monkeypatch.setattr(RD.os.path, "isfile", lambda p: False)
        r = RD.portfolio_construction()
        assert r.get("ok") is False and r.get("error"), r

    def test_is_wired_into_the_receipt(self):
        """必须真的接进回执 —— 写了函数但没接线等于没做。"""
        src = open(os.path.join(_SRC, "run_daily.py"), encoding="utf-8").read()
        assert 'report["steps"]["portfolio_construction"]' in src, (
            "portfolio_construction 没有写进 report['steps'] —— "
            "那它就不会出现在 daily_summary.json 里")
