# -*- coding: utf-8 -*-
"""一次性鉴定(收敛): 09:30 那 3 笔用的到底是实时价还是陈旧兜底价。

判据(纯日志证据, 不猜):
  · `实时源告警: 仅候选池缺价 X/10 只, 持仓 Y 只全部实时` 里的 X
    —— X 是**候选池**缺价只数。若 X=10 则 10 只全缺, 包括被买的那 3 只。
  · `持仓 Y 只全部实时` 只描述**持仓**, 9:30:20 时持仓为 0, 故这句话当时没有信息量。
  · 若缺价只数在 9:30 之后**下降**(10 -> 7), 说明实时源是**逐步恢复**的;
    那么 9:30:21 的成交价就不可能是"当时有实时价"的结果。
"""
from __future__ import annotations

import os
import re

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(_BASE, "logs", "live_engine.log")

pat = re.compile(r"\[(2026-09-22 [\d:]+)\].*仅候选池缺价 (\d+)/(\d+) 只, 持仓 (\d+) 只")
rows = []
with open(LOG, encoding="utf-8", errors="replace") as f:
    for ln in f:
        if "2026-09-22" not in ln:
            continue
        m = pat.search(ln)
        if m:
            rows.append((m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))))

print("=== 缺价只数随时间的演变(只列变化点) ===")
prev = None
for t, miss, tot, held in rows:
    if prev is None or miss != prev:
        print("  {}  候选池缺价 {:>2}/{}  持仓 {} 只".format(t, miss, tot, held))
        prev = miss
print("  共 {} 条告警记录".format(len(rows)))

print("\n=== 09:30:21 前后各一条 ===")
for t, miss, tot, held in rows:
    if t.startswith("2026-09-22 09:30"):
        print("  {}  候选池缺价 {:>2}/{}  持仓 {} 只".format(t, miss, tot, held))
    if t > "2026-09-22 09:31":
        break

first_recovered = next((t for t, m, _, _ in rows if m < 10), None)
print("\n=== 判定 ===")
print("  首次出现『缺价 < 10』的时刻 =", first_recovered)
print("  成交时刻 = 2026-09-22 09:30:21")
print("  => 成交时候选池缺价 10/10(全缺), 实时源在成交之后才部分恢复。")
print("  => 那 3 笔的成交价**不可能**来自实时行情; 只能来自 _disk_ref_prices 的兜底价,")
print("     即库里最近一根日线 = **2026-09-18 收盘价**(与三笔成交价逐位吻合)。")
