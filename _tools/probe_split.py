# -*- coding: utf-8 -*-
"""一次性探针: 验证"成本恒等"与"参与率闸门"两条结论。用完即弃。"""
from __future__ import annotations

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import micro_cost as M  # noqa: E402
import exec_strategy as ES  # noqa: E402

print("常量单边成本率 = %s = %.2f bps" % (M.constant_rate(), M.constant_rate() * 1e4))

print("\n=== 1) 成本恒等(解析结论, 与档数/名义额/标的无关) ===")
print("%14s %6s %14s %14s %12s %10s" % ("名义额", "档数", "不拆成本", "拆单成本", "delta", "identical"))
for n, k in ((10_000, 1), (10_000, 5), (10_000, 20), (1_000_000, 3), (100_000_000, 20)):
    r = M.split_cost_identity(n, k)
    print("%14s %6d %14.4f %14.4f %12.3e %10s"
          % (format(n, ","), k, r["unsplit_cost"], r["split_cost"],
             r["delta_cost"], r["identical"]))

print("\n=== 2) 参与率闸门(拆单真正起作用的地方) ===")
print("%14s %12s %10s %8s %10s %10s %8s"
      % ("名义额", "ADV", "参与率", "拆档", "想下(股)", "本次(股)", "推迟(股)"))
for n, adv in ((10_000, 1e8), (1_000_000, 1e8), (20_000_000, 1e8), (10_000, 1e6)):
    d = ES.decide_split(notional=n, adv=adv, participation_cap=0.10)
    want = int(n / 10)
    q = ES.throttle_qty(want, adv=adv, participation_cap=0.10, price=10.0)
    print("%14s %12.0e %9.5f%% %8d %10d %10d %8d"
          % (format(n, ","), adv, d["participation"] * 100, d["slices"],
             want, q["qty"], q["deferred"]))

print("\n=== 3) 计划单(含每档数量与成本口径留痕) ===")
plan = ES.make_plan("600000.SH", "buy", 25_000, 10.0, adv=1e8,
                    participation_cap=0.10, day="2026-09-22")
for k in ("total_qty", "notional", "participation", "n_slices", "qtys",
          "odd_lot_dropped", "cost_identical", "cost_unsplit", "cost_split",
          "split_reason"):
    print("  %-16s = %s" % (k, plan[k]))

print("\n=== 4) 无成交数据的事实(不假装能算拆单收益) ===")
ct = M.observed_cost_table()
print("  n = %d | source = %s | regression_ready = %s"
      % (ct["n"], ct["source"], ct["regression_ready"]))
print("  note: %s" % ct["note"])

print("\n=== 5) 拆 vs 不拆 的解析对照 ===")
v = ES.split_vs_unsplit(notional=20_000_000.0, n_slices=4)
print("  verdict      =", v["verdict"])
print("  delta_bps    =", v["delta_bps"])
print("  差异在哪里   =", v["where_the_difference_is"])
