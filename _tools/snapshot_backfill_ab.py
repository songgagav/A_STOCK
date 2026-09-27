# -*- coding: utf-8 -*-
"""一次性快照: 走**真实判据入口**看自动回填的 A/B 条件.

## 为什么不能自己算

`backfill_trigger` 的 A/B 判据有明确的语义约定(见其模块 docstring):
引擎探针读的是**厂商引擎**, 而 h5i 水位可能因**回填**而前进 ——
于是"引擎落后"与"h5i 落后"是**两件不同的事**。若我自己去库里取"最后一个有数据的日子",
会把两者混为一谈(该模块 docstring 第 31 行专门警告过这个陷阱: 两者会**同时停在那天,
落后恒为 0**)。故必须调它们的既有入口。

本脚本调用:
  · `engine_bars_sync.freshness()`  -> engine_day / expected_day
  · `backfill_trigger` 的 h5i 水位与 `decide()`

**只读**, 不触发任何回填。
"""

from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))


def main() -> int:
    print("=" * 76)
    print("① engine_bars_sync.freshness()  (厂商引擎探针)")
    print("=" * 76)
    eng = expected = None
    try:
        import engine_bars_sync as E
        f = E.freshness()
        print("  返回类型:", type(f).__name__)
        if isinstance(f, dict):
            for k in sorted(f.keys()):
                print("    %-24s = %s" % (k, f[k]))
            eng = f.get("engine_day")
            expected = f.get("expected_day")
        else:
            print("   ", f)
    except Exception as e:                       # noqa: BLE001
        print("  失败: %s: %s" % (type(e).__name__, e))

    print()
    print("=" * 76)
    print("② backfill_trigger 的 h5i 水位与判据")
    print("=" * 76)
    try:
        import backfill_trigger as BT
        # 优先用模块自带的取数入口, 避免自己拼 SQL 造成口径偏差
        got = None
        for name in ("_probe_all", "_collect", "probe", "_probe"):
            fn = getattr(BT, name, None)
            if callable(fn):
                try:
                    got = fn()
                    print("  用入口 %s() 取数" % name)
                    break
                except Exception as e:           # noqa: BLE001
                    print("  %s() 失败: %s" % (name, e))
        if isinstance(got, dict):
            for k in sorted(got.keys()):
                print("    %-24s = %s" % (k, got[k]))
        print()
        print("  decide() 需要的三个量: engine_day / h5i_watermark / expected_day")
        print("    engine_day     = %s" % eng)
        print("    expected_day   = %s" % expected)
    except Exception as e:                       # noqa: BLE001
        print("  失败: %s: %s" % (type(e).__name__, e))

    print()
    print("=" * 76)
    print("③ 与清单给定值的对照")
    print("=" * 76)
    print("  清单写的是: engine=20260922, h5i=20260924, expected=20260928")
    print("  实测 engine_day = %s / expected_day = %s" % (eng, expected))
    print("  注: `expected` 由**日历**决定(今天之前最近一个交易日)。今天是 2026-09-28(周一),")
    print("      故 expected 应为 09-25 或更早; 清单里的 20260928 只有在**收盘之后**才成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
