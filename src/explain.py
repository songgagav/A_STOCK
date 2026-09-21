# -*- coding: utf-8 -*-
"""可解释查询（路线图 #7）：把散落各处的"为什么"汇成一个可读答案。

要解决的问题
------------
本会话为风控加固造出的"为什么"如今分散在四处:
  · `health_state`        —— 现在是什么状态(NORMAL/DEGRADED/HALTED) + 原因列表
  · `flow_watchdog`       —— 数据还在流动吗 + **归因分类**(死锁 / 行情源冻住 / 进程不在)
  · `kill_switch`         —— 有没有被闸住 + **哪一层**闸的(GLOBAL/ACCOUNT/STRATEGY)
  · `pretrade_compliance` —— 每笔单为什么被放行/拒单/送审 + 待批队列
人被问"为什么系统现在不动了", 得同时打开四个文件才能拼出答案。本模块把它们汇成一次查询。

设计纪律（沿用本会话的归因原则）
--------------------------------
1. **每条"为什么"都必须带来源与证据值** —— 不是"延迟异常", 而是
   "延迟异常: p50=11888ms > 阈值 1000ms（来源 health_state）"。**不在本模块重算任何阈值**,
   只引用各模块自己算出的结论, 避免第二份判据漂移（本会话已因两份 `_proc_alive` 吃过一次亏）。
2. **不知道就说不知道** —— `not_judged` 专门列出"这次查询没能判定的东西"及其原因
   （如: 状态快照已 254s 未更新 => 上面结论基于那个时刻; 账本链校验失败 => 结论不可信）。
   把盲区藏在结论里, 比没有结论更危险。
3. **默认只读且快** —— 默认读**已发布快照**(见 #2 的发布/读取分离), 不跑引擎探针;
   需要现场重算时显式加 `fresh=True`。
4. **引用了哪个账本, 就报它的链校验状态** —— 与 #6 衔接: 若账本曾被篡改, 解释本身不可信。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ 纯函数(CI 可测)

def build_explanation(state=None, reasons=None, flow=None, ks=None,
                      pending_count=None, snapshot_ts=None, snapshot_age_s=None,
                      stale_after_s=1800.0) -> dict:
    """把各来源的结论拼成一份带来源标注的解释（**纯函数**）。

    未提供的来源**不猜** —— 记入 `not_judged`。
    """
    whys: list = []
    not_judged: list = []

    if state is None:
        not_judged.append({"what": "当前健康状态", "why": "没有已发布快照(见 health_state.publish)"})
    else:
        whys.append({"source": "health_state", "kind": "state", "detail": f"状态={state}"})
        for r in (reasons or []):
            whys.append({"source": "health_state", "kind": "reason", "detail": str(r)})

    if isinstance(flow, dict) and flow.get("level"):
        whys.append({"source": "flow_watchdog", "kind": "flow",
                     "detail": f"[{flow.get('level')}/{flow.get('cause')}] {flow.get('reason')}"})
    elif flow is None:
        not_judged.append({"what": "数据是否仍在流动", "why": "本次查询未取看门狗结论"})

    if isinstance(ks, dict) and "blocked" in ks:
        if ks.get("blocked"):
            for r in (ks.get("reasons") or []):
                whys.append({"source": "kill_switch", "kind": "layer_block", "detail": str(r)})
        else:
            whys.append({"source": "kill_switch", "kind": "layer_block",
                         "detail": "三层均未拦截(GLOBAL 未拉闸, 账户/策略层无触发)"})
        if ks.get("fail_closed"):
            not_judged.append({"what": "GLOBAL 层真实状态",
                               "why": "开关文件存在但不可解析 => 已按最严处理(fail-closed)"})
    elif ks is None:
        not_judged.append({"what": "是否被 kill switch 拦截", "why": "本次查询未取该结论"})

    if pending_count is not None:
        whys.append({"source": "pretrade_compliance", "kind": "pending_queue",
                     "detail": f"待批高危单 {pending_count} 笔"
                               + ("（人工未决, 这些单不会执行）" if pending_count else "")})

    if snapshot_age_s is not None and stale_after_s and snapshot_age_s > stale_after_s:
        not_judged.append({
            "what": "上述结论是否代表**此刻**",
            "why": (f"状态快照已 {snapshot_age_s:.0f}s 未更新(阈值 {stale_after_s:.0f}s), "
                    f"以上基于 {snapshot_ts} 那一刻的观测")})

    return {"blocked": bool(isinstance(ks, dict) and ks.get("blocked")),
            "state": state, "whys": whys, "not_judged": not_judged,
            "snapshot_ts": snapshot_ts, "snapshot_age_s": snapshot_age_s}


def find_order_events(entries, symbol=None, side=None, limit=10) -> list:
    """从订单审计条目里挑出与某标的/方向相关的最近若干条（**纯函数**）。"""
    out = []
    for e in entries or []:
        if symbol and str(e.get("symbol")) != str(symbol):
            continue
        if side and str(e.get("side") or "").lower() != str(side).lower():
            continue
        out.append(e)
    return out[-limit:]


def explain_events(events, ledger_ok=None, ledger_reason=None) -> dict:
    """把订单事件解释成人话（**纯函数**）：放行/拒单/送审各自的原因。"""
    lines = []
    for e in events or []:
        why = "; ".join(str(x) for x in (e.get("reasons") or [])) or "(无原因记录)"
        lines.append({"ts": e.get("ts"), "action": e.get("action"),
                      "symbol": e.get("symbol"), "side": e.get("side"),
                      "qty": e.get("qty"), "price": e.get("price"),
                      "actor": e.get("actor"), "why": why})
    out = {"events": lines, "n": len(lines)}
    if ledger_ok is False:
        out["warning"] = (f"所引账本**链校验失败**, 上面内容不可信: {ledger_reason}")
    return out


# ------------------------------------------------------------------ IO 入口

def _read_jsonl(fp, tail=2000) -> list:
    if not os.path.isfile(fp):
        return []
    try:
        with open(fp, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()[-tail:] if x.strip()]
    except Exception:  # noqa: BLE001
        return []


def explain_now(fresh: bool = False) -> dict:
    """此刻为什么是这个状态（默认读快照; fresh=True 则现场重算, 会跑引擎探针）。"""
    import sys
    sys.path.insert(0, os.path.join(_repo_root(), "src"))
    state = reasons = None
    ts = age = None
    flow = None
    try:
        import health_state as H
        if fresh:
            g = H.gather()
            state, reasons = g.get("state"), g.get("reasons")
            flow = (g.get("observed") or {}).get("flow")
        else:
            r = H.read_published()
            if r.get("available"):
                state, reasons = r.get("state"), r.get("reasons")
                ts, age = r.get("ts"), r.get("age_s")
                flow = (r.get("observed") or {}).get("flow")
            else:
                reasons = None
    except Exception as e:  # noqa: BLE001
        state = None
        reasons = [f"health_state 不可用: {type(e).__name__}: {e}"]

    ks = None
    try:
        import kill_switch as K
        ks = K.verdict()
    except Exception:  # noqa: BLE001
        pass

    pending = None
    try:
        import pretrade_compliance as PC
        pending = len([it for it in (PC._read_pending(None).get("orders") or [])
                       if it.get("status") == "pending"])
    except Exception:  # noqa: BLE001
        pass

    out = build_explanation(state=state, reasons=reasons, flow=flow, ks=ks,
                            pending_count=pending, snapshot_ts=ts, snapshot_age_s=age)
    out["queried_at"] = datetime.now().strftime(_TS_FMT)
    out["mode"] = "fresh(现场重算)" if fresh else "snapshot(读已发布快照)"
    return out


def explain_order(symbol: str, side: str | None = None, limit: int = 10) -> dict:
    """这笔单为什么被放行/拒单/送审 —— 含所引账本的链校验状态。"""
    import sys
    sys.path.insert(0, os.path.join(_repo_root(), "src"))
    import pretrade_compliance as PC

    ledger = PC.audit_path()
    entries = _read_jsonl(ledger)
    ledger_ok = ledger_reason = None
    try:
        import audit_chain as AC
        v = AC.verify(ledger)
        ledger_ok, ledger_reason = v.get("ok"), v.get("reason")
    except Exception:  # noqa: BLE001
        pass

    ev = find_order_events(entries, symbol=symbol, side=side, limit=limit)
    out = explain_events(ev, ledger_ok=ledger_ok, ledger_reason=ledger_reason)
    out["symbol"] = symbol
    out["side"] = side
    out["ledger"] = ledger
    out["ledger_verified"] = ledger_ok
    # 队列里的当前状态(可能还没执行/已批准等待)
    try:
        q = [it for it in (PC._read_pending(None).get("orders") or [])
             if (it.get("order") or {}).get("symbol") == symbol]
        out["queue"] = [{"id": it.get("id"), "status": it.get("status"),
                         "reasons": it.get("reasons"), "decided_by": it.get("decided_by")}
                        for it in q]
    except Exception:  # noqa: BLE001
        out["queue"] = []
    return out


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="可解释查询: 为什么是这个状态 / 这笔单为什么被拦")
    ap.add_argument("--now", action="store_true", help="此刻为什么是这个状态(默认)")
    ap.add_argument("--fresh", action="store_true", help="现场重算(跑引擎探针, 较慢)")
    ap.add_argument("--order", metavar="SYMBOL", help="查某标的的订单裁决历史与原因")
    ap.add_argument("--side", choices=["buy", "sell"], help="配合 --order 过滤方向")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.order:
        r = explain_order(args.order, side=args.side)
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
        print(f"标的 {r['symbol']}"
              + (f" 方向 {r['side']}" if r.get("side") else "")
              + f" —— 订单裁决历史(最近 {r['n']} 条), 账本链校验: "
              + ("OK" if r.get("ledger_verified") else "FAIL/未知"))
        if r.get("warning"):
            print("  !! " + r["warning"])
        for e in r["events"]:
            print(f"  {e['ts']}  [{e['action']}] {e['side']} qty={e['qty']} @{e['price']}"
                  f"  by {e['actor']}")
            print(f"        为什么: {e['why']}")
        if r.get("queue"):
            print("  队列中的当前状态:")
            for q in r["queue"]:
                print(f"    {q['id']}  {q['status']}  {'; '.join(q.get('reasons') or [])}")
        return 0

    r = explain_now(fresh=args.fresh)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print(f"查询时刻 {r['queried_at']}  模式={r['mode']}")
    print(f"结论: 状态={r['state']}  kill switch {'已拦截' if r['blocked'] else '未拦截'}")
    print("为什么:")
    for w in r["whys"]:
        print(f"  [{w['source']}] {w['detail']}")
    if r["not_judged"]:
        print("这次查询**没能判定**的:")
        for n in r["not_judged"]:
            print(f"  - {n['what']}: {n['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
