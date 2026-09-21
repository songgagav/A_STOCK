# -*- coding: utf-8 -*-
"""拆单闸门在**虚拟盘引擎**上的接线 (路线图 ⑰)。

设计要点(为什么是"限速"而不是"改价")
------------------------------------
`micro_cost` 已解析证明: 在当前常量成本口径(7bps/单边, 与订单大小无关)下,
**拆单不改变成本**(`Σ r·n_i = r·Σ n_i`)。所以本接线**只做一件事**:

  **把单 tick 的下单量限到 `ADV × participation_cap` 以内, 超出部分推迟到后续 tick。**

这带来三条可验证的性质:
  1. **不改变成本** —— 每档都按同一个常量费率成交, 总额不变;
  2. **不改变当日可达成交量** —— 推迟的量在后续 tick 继续尝试, 从不取消
     (与"取消卖出 = 把风险锁在仓里"同一条纪律);
  3. **不改变选股与权重** —— 限速只影响下单节奏, 不影响目标。
因此 `_to_used`(按成交额累计的换手预算)在"拆"与"不拆"两种情形下**口径可比**:
两者累加的都是同一批成交额, 只是发生在不同的 tick。

接口
----
`throttle(engine, canon, qty, price)` -> {'qty','deferred','capped','participance'}
   纯读 + 纯算, 不改引擎状态; 调用方(引擎)用返回的 qty 下单。
`fetch_adv(engine, canons)` -> {canon: adv}
   一次批量取 ADV(带缓存), 失败返回空字典 => 全部不拆(保持既有行为)。
`record(engine, ...)` -> dict
   把"想下/实际/参与率/成本恒等"写进对照账本(失败不抛)。
"""
from __future__ import annotations

import os


def _cfg() -> dict:
    try:
        from config import PAPER
    except Exception:  # noqa: BLE001
        return {"on": False, "cap": None, "max_slices": 20, "tick_minutes": 30.0}
    return {
        "on": bool(PAPER.get("exec_split", False)),
        "cap": PAPER.get("participation_cap"),
        "max_slices": int(PAPER.get("exec_max_slices", 20) or 20),
        "tick_minutes": float(PAPER.get("exec_tick_minutes", 30.0) or 30.0),
    }


def enabled() -> bool:
    c = _cfg()
    return bool(c["on"] and c["cap"] and float(c["cap"]) > 0)


def fetch_adv(canons, *, lookback: int = 20, day: str | None = None) -> dict:
    """批量取 ADV(元)。失败/无数据返回 `{}` => 调用方走"不拆"。**不抛**。"""
    if not enabled():
        return {}
    try:
        import market_stats
        st = market_stats.stats_for(canons, lookback=lookback, end_day=day)
        return {c: v["adv"] for c, v in (st or {}).items() if v.get("adv")}
    except Exception:  # noqa: BLE001
        return {}


def throttle(canon: str, qty: int, price: float, *, adv: float | None,
             lot: int = 100) -> dict:
    """把单次下单量限到参与率上限内。**纯函数**, 不改任何状态。"""
    c = _cfg()
    out = {"canon": canon, "wanted": int(qty or 0), "qty": int(qty or 0),
           "deferred": 0, "capped": False, "participation": None,
           "cap_qty": None, "cost_identical": True}
    if not c["on"] or not adv or adv <= 0 or not c["cap"]:
        return out
    try:
        import exec_strategy as ES
        r = ES.throttle_qty(int(qty or 0), adv=adv, participation_cap=float(c["cap"]),
                            price=price, lot=lot)
        out.update({"qty": r["qty"], "deferred": r["deferred"], "capped": r["capped"],
                    "participation": r["participation"], "cap_qty": r["cap_qty"]})
    except Exception:  # noqa: BLE001
        return out
    return out


def record(*, canon: str, side: str, wanted: int, throttled: dict, price: float,
           day: str | None = None, adv: float | None = None,
           ledger: str | None = None) -> dict:
    """把一次限速写进对照账本。**失败不抛**。"""
    if not throttled.get("capped"):
        # 未被限速的普通单不写账本 —— 每 tick 都写会把对照账本淹掉,
        # 使"真正被限速的那些"再也找不出来。
        return {"ok": True, "skipped": "not_capped"}
    try:
        import exec_strategy as ES
        c = _cfg()
        plan = ES.make_plan(canon, side, int(wanted), float(price), adv=adv,
                            participation_cap=float(c["cap"] or 0.0),
                            max_slices=c["max_slices"],
                            tick_minutes=c["tick_minutes"], day=day,
                            reason="engine throttle")
        return ES.record_comparison(plan, extra={
            "throttled_qty": throttled.get("qty"),
            "deferred_qty": throttled.get("deferred"),
            "cap_qty": throttled.get("cap_qty"),
            "participation": throttled.get("participation"),
        }, ledger=ledger)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def state_path(path: str | None = None) -> str:
    """推迟量的持久化路径(跨 tick / 跨日重启后可继续)。"""
    if path:
        return path
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "exec_deferred.json")
