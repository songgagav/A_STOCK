# -*- coding: utf-8 -*-
"""一次性诊断: (1) PaperBook 有没有 equity 属性 (2) 买入闸门为何不写订单审计。"""
from __future__ import annotations

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

from paper_book import PaperBook  # noqa: E402

pb = PaperBook(init_capital=100000.0)
print("=== 1) PaperBook 的属性 ===")
for a in ("equity", "cash", "peak_equity", "market_value"):
    v = getattr(pb, a, "MISSING")
    print("  getattr(pb, {:<13}) = {}".format(repr(a), v))
print("  pb.snapshot()['equity'] = {}".format(pb.snapshot()["equity"]))

print("\n=== 2) 引擎买入时真正传入的 ctx(照 realtime_engine 的写法复算) ===")
ctx = {
    "tradable": True,
    "position_qty": (pb.positions.get("301520.SZ") or {}).get("qty"),
    "equity": getattr(pb, "equity", None),
    "cash": getattr(pb, "cash", None),
    "regime": None,                 # 引擎里是 (self._gate or {}).get('regime')
    "freeze_new_buys": False,
    "data_lag_days": None,
    "day_start_equity": None,
}
print("  ctx =", ctx)

import pretrade_compliance as PC  # noqa: E402
print("\n=== 3) gate() 分步 ===")
chk = PC.check_order({"symbol": "301520.SZ", "side": "buy", "qty": 100,
                      "price": 72.08}, ctx)
print("  check_order ->", chk)
try:
    import pretrade_gates as PG
    th = PG.thresholds_from_paper()
    print("  thresholds_from_paper ->", th)
    g = PG.evaluate({"symbol": "301520.SZ", "side": "buy", "qty": 100,
                     "price": 72.08}, ctx, **th)
    print("  pretrade_gates.evaluate -> ok={} failed={} skipped={}".format(
        g["ok"], g["failed"], g["skipped"]))
except Exception as e:  # noqa: BLE001
    print("  pretrade_gates 异常:", e)

print("\n=== 4) 审计文件状态 ===")
fp = PC.audit_path()
print("  audit_path =", fp)
print("  存在 =", os.path.exists(fp))
print("  目录存在 =", os.path.isdir(os.path.dirname(fp)))

print("\n=== 5) 手工调一次 audit() 看能否写出 ===")
import tempfile
tmp = os.path.join(_BASE, "data", "_probe_audit.jsonl")
try:
    rec = PC.audit({"action": "probe", "symbol": "X", "actor": "diagnose"}, path=tmp)
    print("  audit() 返回 =", rec)
    print("  文件已写出 =", os.path.exists(tmp))
    if os.path.exists(tmp):
        with open(tmp, encoding="utf-8") as f:
            print("  内容 =", f.read().strip()[:200])
        os.remove(tmp)
except Exception as e:  # noqa: BLE001
    print("  audit() 异常:", type(e).__name__, e)

print("\n=== 6) gate() 里到底走哪条分支 ===")
import inspect
src = inspect.getsource(PC.gate)
i = src.find("if side == \"buy\"")
print(src[i:i + 900])
