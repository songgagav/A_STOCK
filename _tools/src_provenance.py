"""一次性诊断: `data/targets_source.jsonl` 里**哪些记录来自实盘**, 哪些来自一次性回放.

## 为什么要分

`targets_source.jsonl` 是 `_trace_targets` 的留痕, 而 `_trace_targets` 被
**实盘引擎**与**回放/回测**共同调用。于是这份文件**混着两种来源**, 直接
按 `rung` 统计会得出错误的"实盘档位命中率":

  · 若某档位只在**回放**里出现, 就不能说"实盘从没命中过这一档";
  · 反之若实盘多次落到跨日回退, 才是真问题。

判据: `at` 字段是记录写入时刻。实盘记录应落在**盘中/盘后交易时段**,
且**同日多条**; 一次性回放会把**几十天**的记录在**几分钟内**写完 ——
表现为 `at` 高度集中、但 `consume_day` 跨越很长区间。

## 输出

1. `at` 的日期分布(看是否集中在少数几天);
2. 按 `at` 日期分组的 (consume_day 跨度, 记录数, 档位分布);
3. 实盘候选(记录数少、consume_day 跨度小的那些天)单列。

**只读**。
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FP = os.path.join(_BASE, "data", "targets_source.jsonl")


def main() -> int:
    if not os.path.exists(FP):
        print("找不到", FP)
        return 2
    recs = []
    with open(FP, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    print("总记录:", len(recs))
    if not recs:
        return 0
    print("字段:", sorted(recs[0].keys()))
    print()

    by_at = defaultdict(list)
    for r in recs:
        by_at[str(r.get("at", ""))[:10]].append(r)

    print("=" * 78)
    print("按 at(写入日) 分组")
    print("=" * 78)
    print("%-12s %6s %-24s %s" % ("at_day", "n", "consume_day 区间", "档位分布"))
    print("-" * 78)
    for day in sorted(by_at):
        g = by_at[day]
        cd = sorted(str(r.get("consume_day", "")) for r in g)
        span = f"{cd[0]} .. {cd[-1]}" if cd[0] != cd[-1] else cd[0]
        cnt = Counter(r.get("rung") for r in g)
        cstr = ", ".join(f"{k}={v}" for k, v in cnt.most_common())
        print("%-12s %6d %-24s %s" % (day, len(g), span, cstr))

    print()
    print("=" * 78)
    print("全体档位分布(注意: 混合来源, 不等于实盘命中率)")
    print("=" * 78)
    for k, v in Counter(r.get("rung") for r in recs).most_common():
        print("  %-22s %5d  (%.1f%%)" % (k, v, 100.0 * v / len(recs)))

    print()
    print("=" * 78)
    print("按 at 秒级聚类(看是否几分钟内批量写完 = 回放特征)")
    print("=" * 78)
    secs = sorted(set(str(r.get("at", ""))[:19] for r in recs))
    print("不同的 at 秒级时刻数:", len(secs))
    print("最早:", secs[0] if secs else "-")
    print("最晚:", secs[-1] if secs else "-")
    for s in secs[:15]:
        n = sum(1 for r in recs if str(r.get("at", ""))[:19] == s)
        print("   %s  x%d" % (s, n))
    if len(secs) > 15:
        print("   ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
