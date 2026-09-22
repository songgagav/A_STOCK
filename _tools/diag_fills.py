# -*- coding: utf-8 -*-
"""一次性鉴定: 09-22 那 3 笔买入用的是实时价还是 09-18 兜底价。用完即弃。

线索: 三笔成交价 72.08 / 14.32 / 24.57 与 2026-09-18 收盘价**逐位相同**,
而 h5i 在 09-22 当天最新就是 09-18。需要区分两种可能:
  A) 用的是实时价, 只是恰好等于 09-18 收盘(需看当时的 live_state 快照)
  B) 用的是 _disk_ref_prices 兜底的 09-18 收盘价(即"对着陈旧价成交")
"""
from __future__ import annotations

import json
import os

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ST = os.path.join(_BASE, "data", "live_state.json")
state = json.load(open(ST, encoding="utf-8"))

print("=== live_state.json 快照 ===")
print("  updated     =", state.get("updated"))
print("  day         =", state.get("day"), "| in_session =", state.get("in_session"))
print("  live_source =", state.get("live_source"), "| data_ts =", state.get("data_ts"))
print("  tick        =", state.get("tick"))

print("\n=== 目标池(10 只) ===")
targets = state.get("targets") or []
for t in targets:
    if isinstance(t, dict):
        print("  {:12s} price={} weight={} signal={}".format(
            str(t.get("canon")), t.get("price"), t.get("target_weight"),
            t.get("source_signal")))
    else:
        print("  ", t)

print("\n=== 持仓快照(含 last_price) ===")
pos = state.get("positions")
items = pos.items() if isinstance(pos, dict) else [(p.get("canon"), p) for p in (pos or [])]
for canon, p in items:
    if not isinstance(p, dict):
        print("  ", canon, p)
        continue
    print("  {:12s} qty={} avg_cost={} last_price={} peak={}".format(
        str(canon), p.get("qty"), p.get("avg_cost"), p.get("last_price"),
        p.get("peak_price")))

print("\n=== 判定 ===")
FILLS = {"301520.SZ": 72.08, "600127.SH": 14.32, "603248.SH": 24.57}
REF_0918 = {"301520.SZ": 72.08, "600127.SH": 14.32, "603248.SH": 24.57}
tmap = {str(t.get("canon")): t for t in targets if isinstance(t, dict)}
for canon, fill in FILLS.items():
    t = tmap.get(canon)
    in_pool = canon in tmap
    pool_px = t.get("price") if t else None
    print("  {:12s} 成交价={:<8} 在目标池={:<5} 池内价={:<8} 与09-18收盘同={}".format(
        canon, fill, in_pool, pool_px, fill == REF_0918[canon]))
print("\n  池内价的意义: 若池内价 == 成交价, 说明成交价取自**选股时的价**(即兜底/陈旧价);")
print("                若不同, 说明成交价来自 engine 当时的 latest(可能实时)。")
