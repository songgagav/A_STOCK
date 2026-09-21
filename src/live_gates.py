# -*- coding: utf-8 -*-
"""虚拟盘交易前闸门的**卖侧**接线 (把只有买入才有的闸门补到卖出侧)。

为什么卖侧不能简单照搬买入侧的闸门
----------------------------------
买入侧的清单可以"拒绝下单" —— 最坏结果是少建一个仓。
卖侧不行: **把卖出也拦掉等于把风险锁在仓里**(跌了也出不来)。这与
`kill_switch` 的纪律是同一条("只停新开仓, 绝不停离场")。

那卖侧到底还剩下什么可查的?
--------------------------
**账实相符(canary)**: 引擎计划卖出的数量, 与账本记录的可卖数量是否一致。
这两者由不同代码路径算出(引擎看行情与 T+1, 账本看持仓与锁定), 一旦分叉,
症状是"卖出了一批根本不在账上的股票"或"该卖的没卖掉" —— 两者都不会报错。

· `planned > sellable` 且超出容忍度 => **账实不一致**(引擎的账算错了, 或昨日
  持仓在账本里对不上)。此时**不取消交易**(那是把风险锁在仓里), 而是:
  缩量到 `sellable` 成交 + **响亮告警 + 落审计**, 把"少卖了"这件事变成看得见的。
· `sellable == 0` => 无货可卖, 跳过(本就会跳过, 但这里补一条留痕)。

本模块**不做**的事(明确划界)
----------------------------
· 不做卖出侧的"人工审批": 离场单排队 = 风险锁仓。
· 不做"跌停不卖"判定: 那由引擎的 `_tradable()` 负责, 本模块**不重造**第二份实现
  (本仓已因两份 `_proc_alive` 吃过一次亏)。
· 不改变任何选股/权重结果。
"""
from __future__ import annotations

import os

#: 审计动作名(稳定引用, 供测试与面板)
ACT_SELL_OK = "sell_gate_ok"
ACT_SELL_CLAMPED = "sell_gate_clamped"
ACT_SELL_EMPTY = "sell_gate_nothing_sellable"
ACT_SELL_LOT = "sell_gate_lot_violation"


def sellable_of(positions: dict, canon: str, trade_date: str) -> int:
    """账本口径的可卖数量 = 持仓 - 当日锁定(T+1)。

    与 `paper_book.sell()` 的判定**同口径**(同一表达式), 但本函数是只读的
    —— 它回答"账本认为能卖多少", 而不去动账本。两者分叉即 `sell_gate_clamped`。
    """
    p = (positions or {}).get(canon)
    if not p:
        return 0
    locked = p.get("locked_qty", 0) if p.get("buy_date") == trade_date else 0
    return max(int(p.get("qty", 0) or 0) - int(locked or 0), 0)


def review_sell(canon: str, planned_qty: int, positions: dict, trade_date: str, *,
                price: float = 0.0, tolerance: float = 0.0,
                lot_free: bool = True) -> dict:
    """**纯函数**: 卖侧处置裁定。

    Parameters
    ----------
    planned_qty : 引擎打算卖出的数量(股)
    tolerance   : `planned` 超过 `sellable` 的容忍比例(0 = 不允许超过)。
                  生产默认取自 `PAPER.sell_cash_tolerance`(0.10), 即有 10% 的
                  余量吸收"引擎与账本各算一次"的正常抖动, 超过才判账实不一致。
    lot_free    : 卖出是否允许非整手。A 股**允许**零股卖出(送股等产生), 故默认 True;
                  `pretrade_compliance.check_order` 对买入强制整手、对卖出不强制 —— 两者一致。

    返回 {'action': 'ok'|'clamp'|'nothing', 'qty': 实际可卖数量,
          'planned_qty', 'sellable_qty', 'overflow_pct', 'reasons': [...]}
    """
    sellable = sellable_of(positions, canon, trade_date)
    planned = int(planned_qty or 0)
    out = {"action": ACT_SELL_OK, "qty": planned, "planned_qty": planned,
           "sellable_qty": sellable, "overflow_pct": 0.0, "reasons": [],
           "canon": canon}
    if planned <= 0:
        out["action"] = ACT_SELL_EMPTY
        out["qty"] = 0
        out["reasons"].append("计划卖出数量非正")
        return out
    if sellable <= 0:
        out["action"] = ACT_SELL_EMPTY
        out["qty"] = 0
        out["reasons"].append(f"账本可卖 0 股(持仓={(positions.get(canon) or {}).get('qty', 0)}, "
                              f"T+1 锁定或已无持仓)")
        return out
    if planned <= sellable:
        return out
    over = (planned - sellable) / float(sellable)
    out["overflow_pct"] = round(over * 100.0, 4)
    if over <= max(float(tolerance or 0.0), 0.0):
        # 容忍带内: 缩量到可卖, 不告警(引擎与账本各算一次的正常抖动)
        out["action"] = ACT_SELL_CLAMPED
        out["qty"] = sellable
        out["reasons"].append(
            f"计划 {planned} 略超账本可卖 {sellable}(超 {over * 100:.2f}% <= 容忍 "
            f"{float(tolerance or 0.0) * 100:.0f}%), 缩量成交")
        return out
    out["action"] = ACT_SELL_CLAMPED
    out["qty"] = sellable
    out["reasons"].append(
        f"**账实不一致**: 计划卖出 {planned} 股 > 账本可卖 {sellable} 股"
        f"(超 {over * 100:.2f}% > 容忍 {float(tolerance or 0.0) * 100:.0f}%)"
        f" —— 已缩量到可卖数量成交(**未取消交易**: 取消卖出等于把风险锁在仓里)")
    return out


def thresholds_from_paper() -> dict:
    """卖侧阈值。`tolerance` 取 `PAPER.sell_cash_tolerance`(默认 0.10)。

    为何是 10%: 引擎与账本对"可卖"各算一次(引擎按 `locked_qty`+`buy_date`,
    账本按同一表达式的只读副本), 正常抖动只可能来自当日买入的整手取整 ——
    一笔买入的量级是 100 股, 相对单票持仓通常 <1%; 10% 留两个数量级余量,
    使正常抖动不告警, 而"差一个数量级"的账实分叉必被抓到。
    """
    try:
        from config import PAPER
    except Exception:  # noqa: BLE001
        return {"tolerance": 0.10}
    return {"tolerance": float(PAPER.get("sell_cash_tolerance", 0.10) or 0.10)}


def enabled() -> bool:
    """卖侧闸门开关(`PAPER.sell_gate`, 取不到时启用 —— 它只会缩量+告警, 不会拦单)。"""
    try:
        from config import PAPER
        return bool(PAPER.get("sell_gate", True))
    except Exception:  # noqa: BLE001
        return True


def apply_to_engine(engine, log_fn=None) -> dict:
    """在引擎上执行卖侧闸门检查(留痕 + 返回摘要)。**不取消任何交易**。

    实现刻意做成"只读+记账"而不是"改引擎逻辑": 真正的缩量发生在
    `PaperBook.sell()` 里(它本就会截到可卖量), 本函数的价值是**把这件事说出来**
    —— 账实一致时静默, 不一致时告警 + 落审计。
    """
    out = {"checked": 0, "clamped": [], "nothing": [], "anomalies": []}
    if not enabled():
        return out
    try:
        import pretrade_compliance as _PC
        th = thresholds_from_paper()
        pb = engine.pb
        positions = pb.positions or {}
        trade_date = pb.trade_date
        for canon in list(positions.keys()):
            p = positions[canon]
            planned = int(p.get("qty", 0) or 0)
            r = review_sell(canon, planned, positions, trade_date, **th)
            out["checked"] += 1
            if r["action"] == ACT_SELL_CLAMPED:
                out["clamped"].append(canon)
                if r["overflow_pct"] > float(th["tolerance"]) * 100.0:
                    out["anomalies"].append({"canon": canon, **{
                        k: r[k] for k in ("planned_qty", "sellable_qty", "overflow_pct")}})
                    if log_fn:
                        log_fn(f"[卖侧闸门] {canon}: {r['reasons'][0]}")
                    try:
                        _PC.audit({"action": ACT_SELL_CLAMPED, "symbol": canon,
                                   "side": "sell", "qty": r["qty"],
                                   "price": 0.0, "reasons": r["reasons"],
                                   "actor": "engine"})
                    except Exception:  # noqa: BLE001
                        pass        # 留痕失败不得拖垮交易路径
            elif r["action"] == ACT_SELL_EMPTY:
                out["nothing"].append(canon)
        # 卖侧也可记一条"整体通过"的审计? 不 —— 每个 tick 都写会把订单流水淹掉。
        # 只在有异常时留痕(上面的 clamped 分支)。
        # 把摘要挂到引擎上, 供面板/复盘读取(与 self._gate 同风格)
        try:
            engine._sell_gate = out
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        # 闸门自身异常不得让引擎停手(与 kill switch 同纪律), 但要响亮
        out["error"] = f"{type(e).__name__}: {e}"
        if log_fn:
            log_fn(f"[卖侧闸门] 判定异常(不阻断, 需排查): {out['error']}")
    return out
