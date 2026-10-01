# -*- coding: utf-8 -*-
"""一次性验收: `consume_day` 改动前后, **实盘取池路径**是否等价 + 第 1 档是否真命中.

## 为什么必须做"旧 vs 新"对照, 而不是只看新逻辑通不通

本次改动动的是**实时取池**这条关键路径。只看"新的能跑通"是不够的 ——
真正的风险是**目标池变了**(那会直接影响持仓)。所以本脚本在同一份真实磁盘状态上
分别跑**旧逻辑**与**新逻辑**, 逐日比对:

  · 池成员集合是否**完全一致**;
  · 落在梯子**哪一档**(这一项**预期会变**: 原来记成 `drl_cross_day` 的,
    现在应记成 `drl_same_day` —— 那正是这次修复要修的"档位留痕失真");
  · 来源目录。

## 旧/新如何在同一进程里切换

`_plan_is_formal` 里"字段优先"那一层由 `_plan_consume_ok` 承担。把后者临时替换为
`lambda *_: None`(等价于"所有产物都没有 consume_day")即可复现**改动前**的行为,
因为改动前正是"一律走 generated_at 窗口"。这样两侧用的是**同一份**代码与数据,
差别只剩这一个开关 —— 比另写一份旧实现更可信。

**只读**, 不改任何产物。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from unittest import mock

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import realtime_engine as re  # noqa: E402

DRL = os.path.join(_BASE, "data", "drl")
DAILY = os.path.join(_BASE, "data", "daily")


def _prev_trade(day: str):
    try:
        return re._prev_trade_day(day)
    except Exception:
        return None


def _pool_of(top_n):
    out = []
    for it in (top_n or []):
        c = it.get("canon") if isinstance(it, dict) else it
        if c:
            out.append(str(c))
    return sorted(out)


def _select(day: str, new_style: bool):
    """在 `day` 上跑一遍 DRL 取池。返回 (来源目录, rung, 池)。

    `new_style=False` 时把消费日判据关掉, 复现改动前"只按时间戳窗口"的行为。
    """
    d = day.replace("-", "")
    prev = _prev_trade(day)

    def run():
        # 1) 第 1 档
        src, top_n, _info = re._load_daily_plan_for_consume_day(d, day, prev)
        if top_n:
            return src, "drl_same_day", _pool_of(top_n)
        # 3) 跨日回退 DRL (与线上同序: 全目录倒序, 跳过当日)
        try:
            dirs = [x for x in os.listdir(DRL)
                    if x.isdigit() and len(x) == 8]
            dirs.sort(reverse=True)
            for cand in dirs:
                if cand == d:
                    continue
                top_n, _info = re._try_load_daily_plan(
                    cand, day, prev_trade_day=prev)
                if top_n:
                    return cand, "drl_cross_day", _pool_of(top_n)
        except Exception:
            pass
        return None, "(无 DRL plan)", []

    if new_style:
        return run()
    with mock.patch.object(re, "_plan_consume_ok", lambda *a, **k: None):
        return run()


def main() -> int:
    # 收集所有出现过 plan 的目录日, 作为"潜在消费日"的候选来源
    days = sorted(x for x in os.listdir(DRL)
                  if x.isdigit() and len(x) == 8
                  and os.path.exists(os.path.join(DRL, x, "target_plan.json")))
    # 消费日 = 每个目录日的**下一个交易日**(用日历), 这正是该 plan 应被消费的那天
    try:
        import trading_calendar as tc
    except Exception:
        print("ERROR: trading_calendar 不可用")
        return 2

    def _next_td(d8):
        d = datetime.date(int(d8[:4]), int(d8[4:6]), int(d8[6:8]))
        for _ in range(30):
            d += datetime.timedelta(days=1)
            try:
                if tc.is_trading_day(d):
                    return d.isoformat()
            except Exception:
                return None
        return None

    consume_days = sorted({c for c in (_next_td(x) for x in days) if c})

    print("=" * 100)
    print("旧 vs 新 取池对照 (真实磁盘状态, 只读)")
    print("=" * 100)
    print("%-12s | %-18s %-14s %-4s | %-18s %-14s %-4s | %s"
          % ("消费日", "旧: 来源", "旧: 档位", "只", "新: 来源", "新: 档位", "只", "池一致?"))
    print("-" * 100)

    n_same, n_diff, n_tier1 = 0, 0, 0
    diffs = []
    for day in consume_days:
        old_src, old_rung, old_pool = _select(day, new_style=False)
        new_src, new_rung, new_pool = _select(day, new_style=True)
        same = old_pool == new_pool
        n_same += int(same)
        if not same:
            n_diff += 1
            diffs.append((day, old_src, old_pool, new_src, new_pool))
        if new_rung == "drl_same_day":
            n_tier1 += 1
        print("%-12s | %-18s %-14s %-4d | %-18s %-14s %-4d | %s"
              % (day, str(old_src), old_rung, len(old_pool),
                 str(new_src), new_rung, len(new_pool),
                 "OK" if same else "**差异**"))

    print("-" * 100)
    print("消费日总数            : %d" % len(consume_days))
    print("池**完全一致**的消费日 : %d" % n_same)
    print("池**有差异**的消费日   : %d" % n_diff)
    print("新逻辑下第 1 档命中    : %d" % n_tier1)

    if diffs:
        print()
        print("!! 池差异明细(必须逐条查明, 不得当作可接受) !!")
        for day, osrc, opool, nsrc, npool in diffs:
            only_old = sorted(set(opool) - set(npool))
            only_new = sorted(set(npool) - set(opool))
            print("  消费日 %s: 旧=%s(%d只) 新=%s(%d只) 仅旧有=%s 仅新有=%s"
                  % (day, osrc, len(opool), nsrc, len(npool), only_old, only_new))
    print("=" * 100)
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
