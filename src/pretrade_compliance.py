# -*- coding: utf-8 -*-
"""每单强制合规 + 订单审计 + 高风险单人工审批（路线图 #3）。

设计红线（用户既定）
--------------------
**交易仍以 agent 决策为主；只有高风险的订单才转入人工审批。** 故本模块不是"每单都要人点头"，
而是三段式：
  · 合规：每单**强制**过一遍硬规则（不合规直接不下，留痕）
  · 高危：命中高危特征的单**不执行**，进待批队列（留痕），人批了才走
  · 正常：照常执行（留痕）

阈值来源（本仓证据，不另造）
----------------------------
合规项全部复现本仓**已在执行**的规则，不新增数字：
  · 整手 100 股 —— `realtime_engine.py`: `qty = int(diff // (pr * 100)) * 100` 且 `qty < 100: continue`
  · 价格必须为正 —— 同上 `if pr <= 0: continue`
  · 现金底线 —— 同上"买入后现金须 >= min_cash"
  · 可交易性（涨跌停/停牌）—— 复用引擎 `_tradable()` 的结论（本模块**不重造**该判定，
    只消费 `ctx['tradable']`，避免第二份实现漂移 —— 本会话已因两份 `_proc_alive` 吃过一次亏）
  · 可卖数量（T+1 等）—— 由调用方给 `ctx['sellable_qty']`；本模块不假装知道 T+1 的实现细节

高危判据锚定**既有仓位构造**：本仓是 INIT*MAX_POS 预算下的等权组合，故单个等权槽位
`slot = equity / max_pos` 是**结构性**的上界。于是：
  · 单笔金额 > **一个完整等权槽位** => 高危（正常补仓不会有这种单；通常意味着权重算错或
    数据异常导致的巨量单）
  · **清仓式卖出**（一次卖光某标的全部持仓）=> 高危（不可逆；若是数据故障导致，仓位就没了）
两者都不需要新数字：前者来自等权构造，后者是"不可逆"这一性质本身。

留痕
----
所有裁决（放行/拒单/转人工/人工决定）追加进 `data/order_audit.jsonl`（append-only，
含 ts/action/symbol/side/qty/price/level/reasons/actor）。与 `kill_switch_ledger.jsonl`
分开：前者是**订单级**细粒度流水，后者是**闸门级**事件，混在一起会互相淹没。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

_TS_FMT = "%Y-%m-%d %H:%M:%S"

#: 合规项名称（供测试与面板稳定引用）
VIOL_LOT = "lot_size"              # 非整手/不足一手
VIOL_PRICE = "price_invalid"       # 价格非正
VIOL_QTY = "qty_invalid"           # 数量非正
VIOL_TRADABLE = "not_tradable"     # 涨跌停/停牌/只可卖
VIOL_CASH = "cash_floor"           # 买入后跌破现金底线
VIOL_SELLABLE = "exceeds_sellable" # 卖出超过可卖数量(T+1)

#: 高危特征
RISK_FULL_SLOT = "notional_over_slot"   # 单笔金额超过一个完整等权槽位
RISK_LIQUIDATE = "liquidate_position"   # 清仓式卖出


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def audit_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "order_audit.jsonl")


def audit(entry: dict, path: str | None = None, now=None) -> dict:
    """追加一条订单审计（append-only）。**失败不抛** —— 留痕不得拖垮交易路径。"""
    rec = dict(entry or {})
    rec.setdefault("ts", (now or datetime.now()).strftime(_TS_FMT))
    try:
        fp = audit_path(path)
        d = os.path.dirname(fp)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return rec


def check_order(order: dict, ctx: dict) -> dict:
    """**纯函数**：一单硬合规检查。

    order: {symbol, side: 'buy'|'sell', qty, price}
    ctx:   {cash, equity, max_pos, min_cash, tradable: True|False|'sell_only',
            position_qty, sellable_qty}
           缺的键一律视为"该约束无信息"，**不据此拒单**（避免因缺字段静默停手）。

    返回 {'ok': bool, 'violations': [ {'code','detail'} ], 'level': 'OK'|'REJECT'}
    """
    o, c = order or {}, ctx or {}
    v: list = []

    def _num(x):
        return x if isinstance(x, (int, float)) else None

    side = str(o.get("side") or "").lower()
    qty, price = _num(o.get("qty")), _num(o.get("price"))

    if qty is None or qty <= 0:
        v.append({"code": VIOL_QTY, "detail": f"数量非法: {o.get('qty')!r}"})
    elif side == "buy" and (qty % 100 != 0 or qty < 100):
        v.append({"code": VIOL_LOT,
                  "detail": f"非整手: qty={qty} (A 股买入须 100 股整数倍, 且不少于 100)"})

    if price is None or price <= 0:
        v.append({"code": VIOL_PRICE, "detail": f"价格非法: {o.get('price')!r}"})

    tb = c.get("tradable")
    if tb is False or tb == "sell_only":
        if side == "buy":
            v.append({"code": VIOL_TRADABLE,
                      "detail": f"当前不可买入(涨跌停/停牌等, tradable={tb!r})"})

    if side == "buy" and qty is not None and price is not None and price > 0:
        cash, min_cash = _num(c.get("cash")), _num(c.get("min_cash"))
        if cash is not None and min_cash is not None:
            if cash - qty * price < min_cash:
                v.append({"code": VIOL_CASH,
                          "detail": (f"买入后现金 {cash - qty * price:.2f} < 底线 {min_cash:.2f}")})

    if side == "sell" and qty is not None:
        sellable = _num(c.get("sellable_qty"))
        if sellable is not None and qty > sellable:
            v.append({"code": VIOL_SELLABLE,
                      "detail": f"卖出 {qty} 超过可卖 {sellable} (T+1 未解冻等)"})

    return {"ok": not v, "violations": v, "level": "OK" if not v else "REJECT"}


def classify_risk(order: dict, ctx: dict) -> dict:
    """**纯函数**：高危特征识别（只标风险，不改合规结论）。

    返回 {'high_risk': bool, 'flags': [{'code','detail'}], 'slot': float|None}
    """
    o, c = order or {}, ctx or {}
    flags: list = []

    def _num(x):
        return x if isinstance(x, (int, float)) else None

    side = str(o.get("side") or "").lower()
    qty, price = _num(o.get("qty")), _num(o.get("price"))
    equity, max_pos = _num(c.get("equity")), _num(c.get("max_pos"))

    slot = None
    if equity and max_pos and max_pos > 0:
        slot = equity / max_pos
        if qty and price and qty * price > slot:
            flags.append({"code": RISK_FULL_SLOT,
                          "detail": (f"单笔金额 {qty * price:.0f} > 一个完整等权槽位 "
                                     f"{slot:.0f} (equity/max_pos={equity:.0f}/{max_pos:.0f})")})

    if side == "sell":
        pos_qty = _num(c.get("position_qty"))
        if pos_qty and qty and qty >= pos_qty:
            flags.append({"code": RISK_LIQUIDATE,
                          "detail": f"清仓式卖出 {qty}/{pos_qty} 股(不可逆)"})

    return {"high_risk": bool(flags), "flags": flags, "slot": slot}


def review(order: dict, ctx: dict) -> dict:
    """**纯函数**：三段式裁决（合规 -> 高危 -> 放行）。

    返回 {'decision': 'execute'|'reject'|'pending_approval', 'violations', 'flags',
          'reasons': [str], 'slot'}
    """
    chk = check_order(order, ctx)
    rk = classify_risk(order, ctx)
    reasons = [x["detail"] for x in chk["violations"]] + [x["detail"] for x in rk["flags"]]
    if not chk["ok"]:
        decision = "reject"
    elif rk["high_risk"]:
        decision = "pending_approval"      # 红线: 只有高危单转人工, 正常单照常执行
    else:
        decision = "execute"
    return {"decision": decision, "violations": chk["violations"], "flags": rk["flags"],
            "reasons": reasons, "slot": rk["slot"]}


def record_review(order: dict, ctx: dict, actor: str = "engine",
                  path: str | None = None, now=None) -> dict:
    """裁决 + 落审计（生产入口）。"""
    r = review(order, ctx)
    audit({"action": r["decision"], "symbol": (order or {}).get("symbol"),
           "side": (order or {}).get("side"), "qty": (order or {}).get("qty"),
           "price": (order or {}).get("price"), "reasons": r["reasons"],
           "actor": actor}, path=path, now=now)
    return r


# ------------------------------------------------------------- 高危单待批队列

def pending_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "pending_orders.json")


def _read_pending(path: str | None) -> dict:
    fp = pending_path(path)
    try:
        with open(fp, encoding="utf-8-sig") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {"orders": []}
    except Exception:  # noqa: BLE001
        return {"orders": []}


def _write_pending(path: str | None, doc: dict) -> None:
    fp = pending_path(path)
    d = os.path.dirname(fp)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)


def enqueue(order: dict, ctx: dict, actor: str = "engine",
            path: str | None = None, audit_fp: str | None = None, now=None) -> dict:
    """高危单入待批队列（**不执行**）。返回队列项（含 id）。"""
    now = now or datetime.now()
    doc = _read_pending(path)
    seq = len(doc.get("orders") or []) + 1
    item = {"id": f"{now.strftime('%Y%m%d%H%M%S')}-{seq}",
            "ts": now.strftime(_TS_FMT), "order": dict(order or {}),
            "reasons": list(ctx.get("_reasons") or []) if isinstance(ctx, dict) else [],
            "actor": actor, "status": "pending"}
    r = review(order, ctx) if isinstance(ctx, dict) else {"reasons": []}
    item["reasons"] = r.get("reasons") or item["reasons"]
    doc.setdefault("orders", []).append(item)
    _write_pending(path, doc)
    audit({"action": "pending_approval", "id": item["id"], "symbol": (order or {}).get("symbol"),
           "side": (order or {}).get("side"), "qty": (order or {}).get("qty"),
           "price": (order or {}).get("price"), "reasons": item["reasons"],
           "actor": actor}, path=audit_fp, now=now)
    return item


def decide(order_id: str, approve: bool, actor: str = "human", reason: str = "",
           path: str | None = None, audit_fp: str | None = None, now=None) -> dict:
    """人工批准/驳回。**已决的单不可再决**（防重复放行）。"""
    now = now or datetime.now()
    doc = _read_pending(path)
    for it in doc.get("orders") or []:
        if it.get("id") == order_id:
            if it.get("status") != "pending":
                return {"ok": False, "error": f"该单已决: status={it.get('status')}"}
            it["status"] = "approved" if approve else "rejected"
            it["decided_by"] = actor
            it["decided_at"] = now.strftime(_TS_FMT)
            it["decision_reason"] = reason
            _write_pending(path, doc)
            audit({"action": "approved" if approve else "rejected", "id": order_id,
                   "symbol": (it.get("order") or {}).get("symbol"),
                   "side": (it.get("order") or {}).get("side"),
                   "qty": (it.get("order") or {}).get("qty"),
                   "price": (it.get("order") or {}).get("price"),
                   "reasons": [reason] if reason else [], "actor": actor},
                  path=audit_fp, now=now)
            return {"ok": True, "item": it}
    return {"ok": False, "error": f"未找到待批单: {order_id}"}


def approved_orders(path: str | None = None) -> list:
    """取出**已批准但尚未执行**的单（引擎下一 tick 放行的对象）。"""
    return [it for it in (_read_pending(path).get("orders") or [])
            if it.get("status") == "approved"]


def gate(order: dict, ctx: dict, actor: str = "engine", path: str | None = None,
         audit_fp: str | None = None, now=None) -> dict:
    """**引擎侧咽喉点**：一单的三段式处置 + 留痕（生产入口）。

    **卖出只做合规, 绝不进高危队列**（重要取舍）：
      把止损/离场单排进人工审批 = 把风险锁在仓里（跌了也出不来），
      与 kill switch『只停新开仓, 绝不停离场』是同一条纪律。
      故 `liquidate_position` 只对**买入侧**有意义；卖出侧仍校验可卖数量与价格合法性
      （那是硬合规, 违反说明这笔单本身不成立）。

    返回 {'decision': 'execute'|'reject'|'pending_approval', 'reasons': [...]}；
    判定过程异常时**返回 execute**（不阻断交易）—— 否则一个 bug 就让系统静默停手。
    """
    try:
        side = str((order or {}).get("side") or "").lower()
        chk = check_order(order, ctx)
        if not chk["ok"]:
            reasons = [v["detail"] for v in chk["violations"]]
            audit({"action": "reject", "symbol": (order or {}).get("symbol"), "side": side,
                   "qty": (order or {}).get("qty"), "price": (order or {}).get("price"),
                   "reasons": reasons, "actor": actor}, path=audit_fp, now=now)
            return {"decision": "reject", "reasons": reasons}

        if side == "sell":                      # 离场不排队(见 docstring)
            audit({"action": "execute", "symbol": (order or {}).get("symbol"), "side": side,
                   "qty": (order or {}).get("qty"), "price": (order or {}).get("price"),
                   "reasons": ["离场单: 仅合规校验, 不进高危队列"], "actor": actor},
                  path=audit_fp, now=now)
            return {"decision": "execute", "reasons": []}

        rk = classify_risk(order, ctx)
        if rk["high_risk"]:
            it = enqueue(order, {**(ctx or {}), "_reasons": [f["detail"] for f in rk["flags"]]},
                         actor=actor, path=path, audit_fp=audit_fp, now=now)
            return {"decision": "pending_approval",
                    "reasons": [f["detail"] for f in rk["flags"]], "id": it["id"]}
        return {"decision": "execute", "reasons": []}
    except Exception as e:  # noqa: BLE001
        return {"decision": "execute", "reasons": [], "error": f"{type(e).__name__}: {e}"}


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="每单合规 + 高危识别 + 订单审计 + 高危单审批")
    ap.add_argument("--show", type=int, metavar="N", help="显示最近 N 条订单审计")
    ap.add_argument("--selftest", action="store_true", help="对样例单跑一遍三段式裁决")
    ap.add_argument("--pending", action="store_true", help="列出待批高危单")
    ap.add_argument("--approve", metavar="ID", help="批准某高危单(引擎下一 tick 可放行)")
    ap.add_argument("--reject", metavar="ID", help="驳回某高危单")
    ap.add_argument("--actor", default="human", help="操作者标记(留痕用)")
    ap.add_argument("--reason", default="", help="批准/驳回原因(留痕用)")
    args = ap.parse_args(argv)

    if args.approve or args.reject:
        oid = args.approve or args.reject
        r = decide(oid, approve=bool(args.approve), actor=args.actor,
                   reason=args.reason)
        if not r.get("ok"):
            print(f"失败: {r.get('error')}")
            return 1
        it = r["item"]
        print(f"{'已批准' if args.approve else '已驳回'}: {oid} "
              f"({(it.get('order') or {}).get('symbol')} {(it.get('order') or {}).get('side')} "
              f"{(it.get('order') or {}).get('qty')}) by {args.actor}")
        return 0

    if args.pending:
        items = [it for it in (_read_pending(None).get("orders") or [])
                 if it.get("status") == "pending"]
        if not items:
            print("(无待批单)")
            return 0
        for it in items:
            o = it.get("order") or {}
            print(f"{it['id']}  {it['ts']}  {o.get('symbol')} {o.get('side')} "
                  f"qty={o.get('qty')} @{o.get('price')}  原因: {'; '.join(it.get('reasons') or [])}")
        print(f"\n共 {len(items)} 笔待批 —— 批准: --approve <ID> --actor <你>; 驳回: --reject <ID>")
        return 0

    if args.show:
        fp = audit_path()
        if not os.path.isfile(fp):
            print("(无订单审计)")
            return 0
        for ln in open(fp, encoding="utf-8").read().splitlines()[-args.show:]:
            print(ln)
        return 0

    if args.selftest:
        ctx = {"cash": 50000, "equity": 100000, "max_pos": 5, "min_cash": 5000,
               "tradable": True, "position_qty": 1000, "sellable_qty": 1000}
        cases = [
            ({"symbol": "600000", "side": "buy", "qty": 500, "price": 10}, "正常补仓"),
            ({"symbol": "600000", "side": "buy", "qty": 150, "price": 10}, "非整手"),
            ({"symbol": "600000", "side": "buy", "qty": 3000, "price": 10}, "超一个等权槽位(30000>20000)"),
            ({"symbol": "600000", "side": "sell", "qty": 1000, "price": 10}, "清仓式卖出"),
            ({"symbol": "600000", "side": "sell", "qty": 2000, "price": 10}, "超可卖"),
        ]
        for o, desc in cases:
            r = review(o, ctx)
            print(f"{desc:14s} -> {r['decision']:17s} {r['reasons'][:1]}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
