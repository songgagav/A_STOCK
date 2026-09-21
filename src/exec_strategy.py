# -*- coding: utf-8 -*-
"""拆单执行 (路线图 ⑰ 第二阶段: **模拟盘接入 + 对照留痕**, 不接实盘)。

拆单在本仓的真实作用(先把话说清楚)
----------------------------------
本仓虚拟盘的成交成本是常量 7bps/单边, **与订单大小无关**(见 `micro_cost` 的
推导)。因此:

  **拆单不改变成本 —— 它改变的是"单 tick 吃多少"。**

所以本模块做两件事, 且只做这两件:
  1. **参与率闸门**: 单 tick 的吃单量上限 = `ADV × participation_cap`;
     超出部分**不取消**, 推迟到后续 tick(引擎每 15 秒一轮, 自然摊开)。
     这条限制只降低单 tick 的吃单速度, **当日可达成交量不变**, 故不改变
     选股与权重结果。
  2. **对照留痕**: 把每一笔"本来想下多少 / 实际被限到多少 / 参与率是多少 /
     拆与不拆的成本是否相同"写进 append-only 账本, 供事后复盘。

明确不做
--------
· **不接实盘下单路径**(用户指定)。
· **不假装能算"拆单省了多少冲击成本"**: 真实冲击对规模是次线性的, 但本仓
  没有任何成交数据可标定那个指数(`data/state.json.trades_history` 为空)。
  `micro_cost.observed_cost_table()` 在拿到数据前如实返回 `n=0`。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

_TS_FMT = "%Y-%m-%d %H:%M:%S"

ST_PLANNED = "planned"
ST_PARTIAL = "partial"
ST_FILLED = "filled"
ST_SKIPPED = "skipped"


# --------------------------------------------------------------------------
# 1) 拆单计划
# --------------------------------------------------------------------------
def make_plan(canon: str, side: str, total_qty: int, price: float, *,
              adv: float | None, participation_cap: float, lot: int = 100,
              max_slices: int = 20, tick_minutes: float = 30.0,
              day: str | None = None, reason: str = "") -> dict:
    """构造一个计划单(按参与率上限拆档)。**纯函数**。

    Parameters
    ----------
    adv : 标的日均成交额(元)。None/<=0 => 不拆(调用方保持既有一次性下单行为)。
    participation_cap : 单 tick 最多吃 ADV 的比例, **必须显式传入**(唯一自由度)。

    拆档数的推导与 `micro_cost.participation_cap_qty` 同源:
    `n_slices = ceil(参与率 / cap)`, 上限 `max_slices`; 超过上限的部分
    **跨 tick 继续**(不是放弃), `needs_multiday` 只表示"一个 tick 序列装不下"。
    """
    total_qty = int(total_qty or 0)
    price = float(price or 0.0)
    notional = total_qty * price
    cap = float(participation_cap or 0.0)
    p = (notional / float(adv)) if (adv and adv > 0 and notional > 0) else None
    n = 1
    capped = False
    note = ""
    if p is None:
        note = "无 ADV(取不到行情) => 不拆, 保持一次性下单"
    elif cap <= 0:
        note = "参与率上限未配置(<=0) => 不拆"
    elif p <= cap:
        note = f"参与率 {p * 100:.5f}% <= 上限 {cap * 100:.4f}%, 无需拆单"
    else:
        need = int(-(-p // cap)) if cap > 0 else 1        # ceil
        if need > int(max_slices):
            capped = True
            need = int(max_slices)
        n = max(need, 1)
        note = (f"参与率 {p * 100:.5f}% > 上限 {cap * 100:.4f}% => 拆 {n} 档"
                + (f"(原始需要更多档, 受 max_slices={max_slices} 限制, "
                   f"余量跨 tick 继续)" if capped else ""))
    # 每档数量: 整手均分, 余数堆到最后一档(sum 恒等于 total_qty 的整手部分)
    lots = total_qty // lot if lot > 0 else total_qty
    per_lots = lots // n if n > 0 else lots
    qty_lots = [per_lots] * n
    rem = lots - per_lots * n
    if n > 0:
        qty_lots[-1] += rem
    qtys = [int(x * lot) for x in qty_lots] if lot > 0 else [per_lots] * n
    dropped = total_qty - sum(qtys)
    horizon_total = (n * float(tick_minutes)) / 240.0 if n > 0 else 0.0
    from micro_cost import split_cost_identity
    ident = split_cost_identity(notional, n)
    return {
        "canon": canon, "side": str(side).lower(), "day": day,
        "total_qty": total_qty, "filled_qty": 0, "price": price,
        "notional": round(notional, 2),
        "adv": adv, "participation": p, "participation_cap": cap,
        "n_slices": n, "qtys": qtys, "odd_lot_dropped": int(dropped),
        "tick_minutes": float(tick_minutes),
        "horizon_total_days": round(horizon_total, 6),
        "capped_by_max_slices": capped,
        "cost_identical": ident["identical"],
        "cost_unsplit": round(ident["unsplit_cost"], 6),
        "cost_split": round(ident["split_cost"], 6),
        "split_reason": note,
        "status": ST_PLANNED, "reason": reason,
        "created_at": datetime.now().strftime(_TS_FMT),
    }


# --------------------------------------------------------------------------
# 2) 分档推进
# --------------------------------------------------------------------------
def next_slice(plan: dict) -> dict | None:
    """按已成交量推出"下一档该下多少"。返回 {'index','qty'} 或 None(已下完)。"""
    if not plan:
        return None
    filled = int(plan.get("filled_qty", 0) or 0)
    acc = 0
    for i, q in enumerate(plan.get("qtys") or []):
        acc += int(q)
        if filled < acc:
            return {"index": i, "qty": min(int(q), acc - filled)}
    return None


def throttle_qty(planned_qty: int, *, adv: float | None, participation_cap: float,
                 price: float, lot: int = 100) -> dict:
    """**本模块的生产入口(纯函数)**: 把一次下单量限到"单 tick 参与率上限"内。

    返回 {'qty': 本次实际下单量, 'deferred': 推迟到后续 tick 的量,
          'capped': bool, 'cap_qty': 上限, 'participation': 参与率}
    **只减少本次下单量, 从不取消** —— 推迟的量在后续 tick 会继续尝试,
    故当日可达成交量不变(与"取消卖出 = 把风险锁在仓里"是同一类纪律)。
    """
    from micro_cost import constant_rate, participation_cap_qty
    pq = int(planned_qty or 0)
    notional = pq * float(price or 0.0)
    p = (notional / float(adv)) if (adv and adv > 0 and notional > 0) else None
    cap_qty = participation_cap_qty(adv=adv, participation_cap=participation_cap,
                                    price=price, lot=lot)
    out = {"qty": pq, "deferred": 0, "capped": False, "cap_qty": cap_qty,
           "participation": p, "rate": constant_rate()}
    if cap_qty is None or pq <= 0:
        return out
    if pq > cap_qty:
        out["qty"] = int(cap_qty)
        out["deferred"] = pq - int(cap_qty)
        out["capped"] = True
    return out


def apply_fill(plan: dict, qty: int, price: float | None = None) -> dict:
    """登记一档成交(原地更新 plan)。"""
    q = int(qty or 0)
    if q <= 0:
        return plan
    plan["filled_qty"] = int(plan.get("filled_qty", 0) or 0) + q
    plan["status"] = (ST_FILLED if plan["filled_qty"] >= int(plan.get("total_qty", 0) or 0)
                      else ST_PARTIAL)
    if price is not None:
        plan["last_fill_price"] = float(price)
    return plan


# --------------------------------------------------------------------------
# 3) 对照账本 (append-only, 走 #6 哈希链)
# --------------------------------------------------------------------------
def ledger_path(path: str | None = None) -> str:
    if path:
        return path
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "exec_split_compare.jsonl")


def record_comparison(plan: dict, *, extra: dict | None = None,
                      ledger: str | None = None, now=None) -> dict:
    """把计划/限流/成本口径写进对照账本。**失败不抛**(留痕不得拖垮交易路径)。"""
    now = now or datetime.now()
    rec = {
        "at": now.strftime(_TS_FMT),
        "canon": plan.get("canon"), "side": plan.get("side"), "day": plan.get("day"),
        "total_qty": plan.get("total_qty"), "notional": plan.get("notional"),
        "adv": plan.get("adv"), "participation": plan.get("participation"),
        "participation_cap": plan.get("participation_cap"),
        "n_slices": plan.get("n_slices"), "split_reason": plan.get("split_reason"),
        # 成本口径结论(便于事后一眼看出"拆不拆成本一样")
        "cost_identical": plan.get("cost_identical"),
        "cost_unsplit": plan.get("cost_unsplit"),
        "cost_split": plan.get("cost_split"),
        "status": plan.get("status"), "filled_qty": plan.get("filled_qty"),
    }
    if extra:
        rec["extra"] = extra
    fp = ledger_path(ledger)
    try:
        import audit_chain as _AC
        _AC.append(fp, rec, now=now)
        out = {"ok": True, "chained": True}
    except Exception:  # noqa: BLE001
        try:
            d = os.path.dirname(fp)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(fp, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out = {"ok": True, "chained": False}
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out["record"] = rec
    return out


def read_comparison(ledger: str | None = None) -> list[dict]:
    """读对照账本(坏行跳过)。"""
    fp = ledger_path(ledger)
    out: list[dict] = []
    try:
        with open(fp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return out
    return out


# --------------------------------------------------------------------------
# 4) 拆 vs 不拆: 解析对照
# --------------------------------------------------------------------------
def split_vs_unsplit(*, notional: float, n_slices: int,
                     rate: float | None = None) -> dict:
    """**纯函数**: 同一笔单"拆 N 档"与"一次性"的成本对照(解析结论)。

    在当前常量成本口径下两者**恒等**(见 `micro_cost.split_cost_identity` 的证明)。
    本函数把它连同"差异究竟在哪里"一起返回, 免得后人再把"成本相同"误读成
    "拆单没用" —— 拆单的用处是压参与率, 不是省成本。
    """
    from micro_cost import constant_rate, split_cost_identity
    ident = split_cost_identity(notional, n_slices, rate=rate)
    return {
        "unsplit_cost": ident["unsplit_cost"],
        "split_cost": ident["split_cost"],
        "delta_cost": ident["delta_cost"],
        "delta_bps": ident["delta_rate"] * 1e4,
        "identical": ident["identical"],
        "rate_bps": (rate if rate is not None else constant_rate()) * 1e4,
        "n_slices": ident["n_slices"],
        "verdict": "成本完全相同(拆单不改变成本)",
        "where_the_difference_is": (
            "差异不在成本, 在三处: ①各档成交价随行情变动(价格风险); "
            "②单 tick 吃单量被压到 ADV×cap 以内(降低冲击, 但本仓无数据可标定幅度); "
            "③成交时点分散(可能错过或赶上瞬时行情). "
            "故拆单的决策依据应是**参与率是否超过流动性上限**, 而不是成本."
        ),
    }


def decide_split(*, notional: float, adv: float | None, participation_cap: float,
                 max_slices: int = 20) -> dict:
    """该不该拆。**唯一正当理由**: 单笔参与率超过流动性上限。"""
    p = (float(notional) / float(adv)) if (adv and adv > 0 and notional > 0) else None
    cap = float(participation_cap or 0.0)
    if p is None:
        return {"should_split": False, "slices": 1, "slices_needed": 1,
                "participation": None, "reason": "无 ADV => 不拆",
                "capped_by_max_slices": False, "fillable_notional": None}
    if cap <= 0:
        return {"should_split": False, "slices": 1, "slices_needed": 1,
                "participation": p, "reason": "参与率上限未配置 => 不拆",
                "capped_by_max_slices": False, "fillable_notional": None}
    need = int(-(-p // cap))
    capped = need > int(max_slices)
    return {
        "should_split": need > 1,
        "slices": min(need, int(max_slices)),
        "slices_needed": need,
        "participation": p,
        "capped_by_max_slices": capped,
        "fillable_notional": float(adv) * cap * int(max_slices),
        "needs_multiday": capped,
        "reason": (f"参与率 {p * 100:.5f}% " +
                   ("<=" if need <= 1 else ">") +
                   f" 上限 {cap * 100:.4f}% => " +
                   ("无需拆单" if need <= 1 else f"需 {min(need, max_slices)} 档")),
    }
