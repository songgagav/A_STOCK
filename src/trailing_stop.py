# -*- coding: utf-8 -*-
"""移动止损 (路线图 #10/⑩): 盈利时上移止损线, 锁定已实现的浮盈.

问题
----
本仓的止损失只有**固定阈值**一条: `config.PAPER["stop_loss"] = 0.03`, 判定式是
`price / avg_cost - 1 < -0.03`。它有两个结构性缺陷:

  1. **赚过又跌回去的单, 与从未赚过的单被同等对待**。一只票 +20% 后回落到 +2%,
     固定止损认为"还赚着, 不动"; 而账户实际已经吐出 18pp 利润。固定止损只锚定
     **成本**, 不锚定**撤出幅度**。
  2. 止损线不随行情推进, 于是"最大回撤"由入场后的运气决定, 而不是由风控决定。

本模块的做法
------------
对每个持仓追踪**入场以来有利极值** `peak`(做多=最高成交价), 止损线随 peak 上移:

    peak <= entry                          -> 止损线 = entry * (1 - stop_loss)   [固定止损, 原口径]
    peak >  entry                          -> 止损线 = peak  * (1 - giveback)      [移动止损]
    最终止损线 = max(上面两条, 硬地板 entry * (1 - hard_floor))

`giveback`(允许的回撤比例)由调用方给出, 本模块**不内置默认数值** —— 见下面的
"参数来源"一节, 那里说明为何不在这里写死, 以及生产接线处取的是什么。

为什么必须 max(固定, 移动)
--------------------------
移动线在"刚赚一点"时会**高于**固定线(peak=1.01*entry、giveback=0.04 时移动线=
entry*0.9696 > entry*0.97), 此时若只取移动线, 止损会比原口径**更早**触发 ——
那就不是"锁利润"而是"改紧了止损", 属于静默改变风险预算。取 max 保证:
**移动止损只会放宽或提前锁定盈利, 永不比固定止损更早砍仓**。

本模块是纯函数, 无 I/O、不 import 本项目其它模块, 因此可在任何解释器下测试。
"""
from __future__ import annotations

import math

#: 判断"是否已进入移动段"的最小盈利幅度。低于它视同 peak == entry(即仍按固定止损)。
#: 取一个极小正数而非 0, 是为了让浮点误差与手续费噪声不至于把刚成交的单立刻
#: 判成"已有盈利极值" —— 否则成交价上一个 1e-9 的抖动就会定出 peak。
_EPS = 1e-9


def entry_floor(entry: float, stop_loss: float, hard_floor: float = 0.0) -> float:
    """固定止损线(原口径) = entry * (1 - stop_loss), 再受硬地板约束。

    hard_floor 的语义是"**最多容忍跌多深**"(0 表示不设上限)。它约束的是
    **固定线**: 实现用 `min(line, entry*(1-hard_floor))`, 即

    · stop_loss=3% 而 hard_floor=9% -> 生效 3%(原口径更紧, 地板不改变行为)
    · stop_loss=15% 而 hard_floor=9% -> 生效 9%(地板把容忍度收窄到 9%)

    若相反地用 max, hard_floor 会把止损线**推低**(允许亏更多), 与名字完全相反 ——
    实现时确实踩过这个方向, `test_hard_floor_*` 就是为锁死它而写。

    **注意 hard_floor 不约束峰值线**: 移动线锁的是利润, 其水平在成本之上
    (peak > entry 且 giveback < 100% 时必然 > entry*(1-giveback)), 用"最多容忍
    跌多深"去压它会把已经锁住的利润重新放开 —— 见 `trail_line`。
    """
    e, s = float(entry), abs(float(stop_loss))
    line = e * (1.0 - s)
    if hard_floor and hard_floor > 0:
        line = min(line, e * (1.0 - abs(float(hard_floor))))
    return line


def trail_line(entry: float, peak: float, giveback: float,
               stop_loss: float | None = None, hard_floor: float = 0.0) -> float:
    """当前止损线。**只上移, 不下移** —— 由 max(固定, 移动) 保证。

    Parameters
    ----------
    entry : float
        持仓均价(含买入滑点的 avg_cost)。
    peak : float
        入场以来有利极值(做多=最高价); 传 None/0 视为等于 entry。
    giveback : float
        允许从峰值回撤的比例(0.04 = 回吐 4% 即出)。
    stop_loss : float | None
        固定止损阈值; None 时**只用移动线**(调用方明确表示不要固定兜底)。
    hard_floor : float
        最多容忍跌多深(0.09 = 固定线最多容忍跌到 entry*0.91)。只作用于**固定线**,
        不压制峰值线 —— 峰值线锁的是利润(其水平在成本之上), 用容忍度去压它等于
        把锁住的利润重新放开。

    返回 止损价(正数)。entry<=0 或 giveback<=0 时返回 0.0(表示"本单不设移动止损",
    由调用方继续用原固定判定) —— 这是**显式的"未启用"**, 不是"止损线为 0"。
    """
    e = float(entry or 0.0)
    if e <= 0 or not math.isfinite(e):
        return 0.0
    g = float(giveback or 0.0)
    # 固定线: (1-hard_floor) 的地板约束在这里生效(max(内部) 与 entry_floor 的
    # min(内部) 不是同一个数字, 下面 max(固定, 峰值) 保证"只上移不下移")
    fixed_line = entry_floor(e, stop_loss, hard_floor) if stop_loss is not None else 0.0
    peak_line = 0.0
    if g > 0:
        pk = float(peak or 0.0)
        if not math.isfinite(pk) or pk <= 0:
            pk = e
        if pk < e:
            pk = e          # 极值不可能低于入场价(做多): 数据异常时退回固定段
        if pk > e + _EPS:
            peak_line = pk * (1.0 - g)
    if fixed_line <= 0 and peak_line <= 0:
        return 0.0
    return float(max(fixed_line, peak_line))


def update_peak(peak: float | None, price: float, entry: float = 0.0) -> float:
    """推进有利极值。首次调用(peak 为 None)时以 max(price, entry) 初始化。

    以 `max(price, entry)` 而不是 `price` 初始化: 建仓成交后当根 bar 的收盘价可能
    略低于含滑点的 avg_cost, 若用 price 初始化, peak 会低于 entry 并被
    `trail_line` 拉回 entry —— 无害但语义含糊。用 max 让 peak 从一开始就是
    "入场以来见过的最高价", 与字面定义一致。
    """
    p = float(price or 0.0)
    if not math.isfinite(p) or p <= 0:
        return float(peak) if peak else float(entry or 0.0)
    if peak is None:
        return max(p, float(entry or 0.0))
    return max(float(peak), p)


def should_exit(entry: float, price: float, peak: float | None,
                giveback: float, stop_loss: float | None = None,
                hard_floor: float = 0.0) -> dict:
    """判定是否触发移动止损。**纯函数**, 返回判定明细(供留痕与测试)。

    返回 {'exit': bool, 'line': float, 'trigger': 'fixed'|'trailing'|'',
          'drawdown_from_peak': float, 'lock_pct': float, 'peak': float}

    `trigger='fixed'` 表示这条线其实来自固定止损(移动段还没超过它) —— 调用方据此
    可以继续走原来的"固定止损"日志措辞, 避免把没变的行为报成新行为。
    `lock_pct` = (line/entry - 1) * 100, 即"这条线锁住的利润百分比"(负=仍在亏)。
    """
    e = float(entry or 0.0)
    pr = float(price or 0.0)
    pk = update_peak(peak, pr, e) if (peak is not None or e > 0) else None
    line = trail_line(e, pk if pk is not None else e, giveback, stop_loss, hard_floor)
    out = {
        "exit": False, "line": line, "trigger": "",
        "drawdown_from_peak": 0.0,
        "lock_pct": round((line / e - 1.0) * 100.0, 4) if e > 0 else 0.0,
        "peak": float(pk) if pk is not None else 0.0,
    }
    if e <= 0 or pr <= 0 or line <= 0:
        return out
    if pk and pk > e + _EPS:
        out["drawdown_from_peak"] = round((pr / pk - 1.0) * 100.0, 4)
    if pr <= line:
        out["exit"] = True
        # 归因: 触发线若由峰值决定 => trailing; 否则是固定止损在起作用
        fixed = entry_floor(e, stop_loss, hard_floor) if stop_loss is not None else 0.0
        out["trigger"] = "fixed" if (fixed and line <= fixed + _EPS) else "trailing"
    return out


def apply_to_positions(positions: dict, price_of, giveback: float,
                       stop_loss: float | None = None, hard_floor: float = 0.0,
                       peak_key: str = "peak_price") -> list[dict]:
    """对一组持仓批量判定, 并把最新极值**回写**进持仓 dict(原地)。

    positions : {canon: {"qty","avg_cost","peak_price"?, ...}}
    price_of  : callable(canon) -> 当前价(<=0 表示无价, 该单跳过)

    返回触发列表 [{'canon','entry','price','peak','line','trigger',
                  'drawdown_from_peak','lock_pct'}]; 未触发的不返回, 但极值仍已推进。

    **只判定不卖出** —— 卖出由调用方(PaperBook / realtime_engine)执行, 以便这两个
    生产路径各自保留自己的 T+1 锁定、跌停不可卖、冷却集等既有约束。
    """
    out: list[dict] = []
    if not positions:
        return out
    g = float(giveback or 0.0)
    for canon in list(positions.keys()):
        p = positions.get(canon) or {}
        entry = float(p.get("avg_cost") or 0.0)
        if entry <= 0:
            continue
        try:
            pr = float(price_of(canon) or 0.0)
        except Exception:  # noqa: BLE001
            pr = 0.0
        if pr <= 0 or not math.isfinite(pr):
            continue
        new_peak = update_peak(p.get(peak_key), pr, entry)
        p[peak_key] = new_peak      # 回写: 极值必须跨 tick/跨日单调不减
        v = should_exit(entry, pr, new_peak, g, stop_loss, hard_floor)
        if v["exit"]:
            out.append({"canon": canon, "entry": entry, "price": pr,
                        "peak": v["peak"], "line": v["line"],
                        "trigger": v["trigger"],
                        "drawdown_from_peak": v["drawdown_from_peak"],
                        "lock_pct": v["lock_pct"]})
    return out
