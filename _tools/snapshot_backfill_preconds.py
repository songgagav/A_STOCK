# -*- coding: utf-8 -*-
"""一次性快照: 自动回填的前置条件(engine / h5i / expected)与开关状态.

## 为什么先看前置条件而不是直接看结果

清单里的第 1~3 项都要等**当日 19:10 收盘管道**跑完才有结果。若当前时刻还没到点,
"结果"一律是"尚未发生" —— 那是**时间问题, 不是缺陷**。此时有意义的做法是把
**前置条件**记下来, 这样等管道跑完后可以直接对照"该触发的有没有触发"。

## 三个量(与既有判据同名)

  · `engine`   —— 厂商引擎最后一个已发布交易日;
  · `h5i`      —— 本地湖最后一个已入库交易日;
  · `expected` —— **理应**达到的交易日(上一个已收盘交易日)。

现行判据: 引擎落后 > 发布宽限(1 个交易日) 即视为"疑厂商漏发"。
预期结果: `expected` 晚于 `engine` 时, A/B 条件成立、**应当**触发自动回填。

**只读**, 不改任何状态。
"""

from __future__ import annotations

import datetime
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))


def _last_h5i_day() -> str | None:
    try:
        from h5i_bar_store import H5iBarStore
        s = H5iBarStore()
        try:
            days = sorted(x for x in s.trading_days() if x)
        finally:
            s.close()
        return str(days[-1])[:10] if days else None
    except Exception as e:                       # noqa: BLE001
        return f"(查询失败: {type(e).__name__}: {e})"


def _last_engine_day() -> str | None:
    """厂商引擎的覆盖末日 —— 走 engine_bars_sync 的既有入口, 不另造协议。"""
    try:
        import engine_bars_sync as E
        for name in ("engine_last_day", "last_engine_day", "engine_coverage_end"):
            fn = getattr(E, name, None)
            if callable(fn):
                return str(fn())[:10]
        return "(engine_bars_sync 无直接查询入口)"
    except Exception as e:                       # noqa: BLE001
        return f"(查询失败: {type(e).__name__}: {e})"


def _prev_trade_day() -> str | None:
    try:
        import trading_calendar as tc
        d = datetime.date.today() - datetime.timedelta(days=1)
        for _ in range(15):
            if tc.is_trading_day(d):
                return d.isoformat()
            d -= datetime.timedelta(days=1)
    except Exception:                            # noqa: BLE001
        return None
    return None


def main() -> int:
    print("=" * 74)
    print("自动回填前置条件快照")
    print("=" * 74)
    expected = _prev_trade_day()
    print("  当前时刻        : %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %a"))
    print("  expected(应达到) : %s   (今天之前最近一个交易日)" % expected)
    print("  engine(厂商末日) : %s" % _last_engine_day())
    print("  h5i(本地湖末日)  : %s" % _last_h5i_day())

    print()
    print("=" * 74)
    print("开关与留痕")
    print("=" * 74)
    sw = os.path.join(_BASE, "data", "backfill_switch.json")
    if os.path.exists(sw):
        raw = open(sw, encoding="utf-8").read()
        print("  backfill_switch.json : %s" % raw.strip())
    else:
        print("  backfill_switch.json : (不存在)")
    pv = os.path.join(_BASE, "data", "backfill_provenance.jsonl")
    if os.path.exists(pv):
        lines = [x for x in open(pv, encoding="utf-8").read().splitlines() if x.strip()]
        print("  backfill_provenance  : %d 条" % len(lines))
        for ln in lines[-3:]:
            try:
                j = json.loads(ln)
                print("      %s  %s" % (str(j.get("at", ""))[:19],
                                        {k: v for k, v in j.items() if k != "at"}))
            except Exception:
                print("      %s" % ln[:120])
    else:
        print("  backfill_provenance  : (不存在)")

    print()
    print("=" * 74)
    print("清单第 1~3 项的当前状态")
    print("=" * 74)
    for day in ("20260928",):
        dd = os.path.join(_BASE, "data", "daily", day)
        dp = os.path.join(_BASE, "data", "drl", day, "target_plan.json")
        print("  data/daily/%s/daily_summary.json : %s"
              % (day, "存在" if os.path.exists(os.path.join(dd, "daily_summary.json"))
                 else "**不存在**"))
        print("  data/drl/%s/target_plan.json     : %s"
              % (day, "存在" if os.path.exists(dp) else "**不存在**"))
        if os.path.exists(dp):
            j = json.load(open(dp, encoding="utf-8"))
            print("      consume_day = %r   day = %r   generated_at = %r"
                  % (j.get("consume_day"), j.get("day"), j.get("generated_at")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
