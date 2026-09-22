# -*- coding: utf-8 -*-
"""独立复核审计结论: 逐字复刻生产 ctx, 看那三项检查到底生效没有。

不采信报告, 自己跑。
"""
from __future__ import annotations

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import pretrade_compliance as PC  # noqa: E402
import pretrade_gates as PG  # noqa: E402
from config import PAPER, MAX_STOCKS  # noqa: E402

print("=== 0) config 里到底有没有这些键 ===")
for k in ("max_pos", "min_cash", "pretrade_max_position_pct",
          "portfolio_drawdown", "max_single_weight"):
    print("  PAPER.get(%-28r) = %r" % (k, PAPER.get(k)))
print("  MAX_STOCKS =", MAX_STOCKS)

print("\n=== 1) 逐字复刻生产 ctx(realtime_engine.py:1120-1133) ===")
EQUITY = 101760.31
ctx_prod = {
    "tradable": True,
    "position_qty": None,
    "equity": EQUITY,                 # 已由 _pb_equity() 供给
    "cash": 80689.31,
    "max_pos": MAX_STOCKS,            # [修] 此前不传 => 高危槽位判定永不触发
    "min_cash": 5000.0,               # [修] 此前不传 => 现金底线永不触发
    "drawdown_pct": -0.37,            # [修] 此前不传 => 组合回撤项永远 skip
    "peak_equity": 102140.31,
    "regime": "normal",
    "freeze_new_buys": False,
    "data_lag_days": 3,
    "day_start_equity": EQUITY,
}
print("  keys =", sorted(ctx_prod))
print("  含 max_pos?        ", "max_pos" in ctx_prod)
print("  含 min_cash?       ", "min_cash" in ctx_prod)
print("  含 drawdown_pct?   ", "drawdown_pct" in ctx_prod)
print("  含 peak_equity?    ", "peak_equity" in ctx_prod)

print("\n=== 2) 一笔 999,000 元的买单(权益的 9.8 倍)在生产 ctx 下会怎样 ===")
order = {"symbol": "600000.SH", "side": "buy", "qty": 99900, "price": 10.0}
th = PG.thresholds_from_paper()
print("  thresholds_from_paper =", th)
g = PG.evaluate(order, ctx_prod, **th)
print("  pretrade_gates.evaluate -> ok=%s failed=%s" % (g["ok"], g["failed"]))
for c in g["checks"]:
    print("     %-20s %-5s %s" % (c["name"], c["status"], str(c["detail"])[:80]))
r = PC.review(order, ctx_prod)
print("  pretrade_compliance.review -> decision=%s reasons=%s" % (r["decision"], r["reasons"]))
print("  classify_risk ->", PC.classify_risk(order, ctx_prod))

print("\n=== 3) 补上 max_pos/min_cash/drawdown_pct 后同样一笔单 ===")
ctx_fix = dict(ctx_prod, max_pos=MAX_STOCKS, min_cash=5000.0,
               drawdown_pct=-0.37, peak_equity=102140.31)
g2 = PG.evaluate(order, ctx_fix, **th)
print("  pretrade_gates -> ok=%s failed=%s" % (g2["ok"], g2["failed"]))
r2 = PC.review(order, ctx_fix)
print("  pretrade_compliance.review -> decision=%s" % r2["decision"])
print("  reasons:", (r2["reasons"] or [])[:2])

print("\n=== 4) 停牌判定: 生产主源的 volume 是什么 ===")
import inspect  # noqa: E402
import paper_book as PB  # noqa: E402
src = inspect.getsource(PB)
i = src.find('"volume": -1')
if i < 0:
    i = src.find("'volume': -1")
print("  源码里是否有 volume=-1 硬编码:", i > 0)
j = src.find("def _tradable")
trad = src[j:j + 700]
print("  _tradable 片段:")
print("   ", trad.replace("\n", "\n    ")[:600])
