# -*- coding: utf-8 -*-
"""一次性探针: 用真实 ADV/波动率验证拆单闸门(需要 h5i 解释器)。用完即弃。"""
from __future__ import annotations

import os
import sys
import warnings

warnings.filterwarnings("ignore")
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import market_stats as MS  # noqa: E402
import exec_gate as EG  # noqa: E402
from h5i_bar_store import H5iBarStore  # noqa: E402

days = H5iBarStore().trading_days()
canons = ["600000.SH", "000001.SZ", "300750.SZ", "688981.SH", "830799.BSE"]
st = MS.stats_for(canons, end_day=days[-1])
print("数据末尾 =", days[-1], "| 拆单闸门 enabled =", EG.enabled())
print("\n=== ADV / 波动率实取 ===")
for c in canons:
    v = st.get(c)
    if v:
        print("  {:12s} ADV = {:>18s} 元   vol = {:.4f} ({})  n_amt={} n_chg={}".format(
            c, format(v["adv"], ",.0f"), v["vol"], v["vol_source"],
            v["n_amount"], v["n_change"]))
    else:
        print("  {:12s} <无数据>".format(c))

print("\n=== 拆单闸门实跑(真实 ADV, 价 10 元, cap 10%) ===")
print("  {:12s} {:>14s} {:>12s} {:>12s} {:>12s} {:>8s} {:>10s}".format(
    "标的", "名义额", "想下(股)", "本次(股)", "推迟(股)", "capped", "参与率"))
for c in canons:
    v = st.get(c)
    if not v:
        continue
    adv = v["adv"]
    for notional in (10_000, 1_000_000, 50_000_000):
        qty = int(notional / 10.0)
        r = EG.throttle(c, qty, 10.0, adv=adv)
        print("  {:12s} {:>14s} {:>12d} {:>12d} {:>12d} {:>8s} {:>9.4f}%".format(
            c, format(notional, ","), r["wanted"], r["qty"], r["deferred"],
            str(r["capped"]), (r["participation"] or 0) * 100))

print("\n=== 本仓虚拟盘的真实参与率(用实际单笔规模) ===")
print("  虚拟盘 10 万资金 / 10 只 => 约 1 万/单; 单票上限 8% => 约 8 千/单")
for c in canons:
    v = st.get(c)
    if not v:
        continue
    for notional in (8_000, 10_000):
        p = notional / v["adv"]
        print("  {:12s} 单笔 {:>6s} 元 => 参与率 {:.6f}%  (上限 10%, 相差 {:.0f} 倍)".format(
            c, format(notional, ","), p * 100, 0.10 / p))
