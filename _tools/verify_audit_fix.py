# -*- coding: utf-8 -*-
"""一次性验证: 修好后, 一次正常放行的买入是否会写订单审计 + equity 是否非空。"""
from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import pretrade_compliance as PC  # noqa: E402
from paper_book import PaperBook  # noqa: E402

tmp = os.path.join(_BASE, "data", "_probe_order_audit.jsonl")
if os.path.exists(tmp):
    os.remove(tmp)
for suf in (".head.json", ".lock"):
    if os.path.exists(tmp + suf):
        os.remove(tmp + suf)

pb = PaperBook(init_capital=100000.0)
eq = pb.snapshot()["equity"]
print("1) PaperBook 现算权益 =", eq, "(修前 getattr(pb,'equity',None) = None)")

print("\n2) 走真实事件: 正常放行的买入")
r = PC.gate({"symbol": "301520.SZ", "side": "buy", "qty": 100, "price": 72.08},
            {"tradable": True, "position_qty": None, "equity": eq, "cash": pb.cash,
             "regime": "normal", "freeze_new_buys": False, "data_lag_days": 3,
             "day_start_equity": eq},
            actor="engine", audit_fp=tmp)
print("   裁决 =", json.dumps(r, ensure_ascii=False))
print("   审计文件已写出 =", os.path.exists(tmp))
if os.path.exists(tmp):
    with open(tmp, encoding="utf-8") as f:
        for ln in f:
            print("   ", ln.strip()[:220])

print("\n3) 走真实事件: 组合闸门拒单(equity 非空 => 仓位上限检查真的生效)")
r2 = PC.gate({"symbol": "600000.SH", "side": "buy", "qty": 900, "price": 10.0},
             {"tradable": True, "position_qty": None, "equity": 100000.0,
              "cash": 50000.0, "regime": "risk", "freeze_new_buys": False},
             actor="engine", audit_fp=tmp)
print("   裁决 =", json.dumps(r2, ensure_ascii=False)[:200])

print("\n4) 审计文件最终内容(逐条)")
if os.path.exists(tmp):
    with open(tmp, encoding="utf-8") as f:
        for i, ln in enumerate(f):
            d = json.loads(ln)
            print("   [{}] action={} symbol={} reason={}".format(
                i, d.get("action"), d.get("symbol"), str(d.get("reasons"))[:70]))
    os.remove(tmp)
    for suf in (".head.json", ".lock"):
        if os.path.exists(tmp + suf):
            os.remove(tmp + suf)
