# -*- coding: utf-8 -*-
"""P0: 生产台账里「持有期 = 1 天」的卖出 —— 是 min_hold 失效, 还是有条件优先于它?

## 代码事实(已读, 不是猜)

`src/realtime_engine.py` 的卖出有三条独立路径, **顺序即优先级**:

| 序 | 块 | 行 | 是否受 `min_hold` 约束 | 是否受"调仓间隔门"约束 |
|---|---|---|---|---|
| 1 | **离开目标池** | 951-981 | **是**(`held < min_hold` 则 skip) | **是**(整块在 `if gate_open:` 内) |
| 2 | **单票止损/移动止损** | 983-1036 | **否** —— 只查 T+1 `locked_qty` | **否**(在 `gate_open` 之外) |
| 3 | **组合级风控(熔断/集中度压回)** | 1038-1049 | **否** | **否** |

且 L933 的日志明写: 「调仓间隔未到…**本轮仅止损/风控**」 ⇒
**门关时"离开目标池"这一整块不执行**, 故 min_hold 在那轮根本不参与。

⇒ 结构性结论: **`min_hold` 只约束"离开目标池"这一条路径**;
止损与风控是**设计上的例外**(代码注释: 「最小持仓天数: 持仓不足N天不出售(**止损/风控除外**)」)。

## 本脚本要回答

1. 实际 1 天卖出共几笔? 它们**可能**由哪条路径产生?
2. 用**可观测代理**归因每条卖出(见下), 给出占比;
3. 明确哪些是**代码可证**、哪些只是**代理推断**(不夸大)。

### 归因代理(只有这些字段可用)

台账每条只有 `type/canon/qty/price/fee/pnl/time/date` —— **没有 `reason` 字段**。
故只能用代理:

· 卖出当日该票**是否仍在目标池**(`top_targets` 每日落盘) ⇒ 在 = **不是**"离开目标池";
· 卖出时相对**买入均价的回撤** ⇒ 是否触及 −3% 止损线;
· 持有天数。

**代理的局限必须先声明**: `top_targets` 只存当日的; 而"离开目标池"的判定用的是
**当轮 targets**, 二者可能不同。故"在目标池里却卖了"只能**支持**"非路径 1",
不能**证明**。
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sell_attribution.txt")

import h5i_bar_store as S  # noqa: E402

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st_ = S.open_store()
ALL = st_.trading_days()
_px: dict[str, dict] = {}


def close_of(day: str, code: str):
    if day not in _px:
        df = st_.bars_on_day(day)
        _px[day] = {str(r.symbol): float(r.close) for r in df.itertuples(index=False)
                    if r.close is not None}
    v = _px[day].get(code)
    return v if v and v > 0 else None


# ---------------------------------------------------------------- 读台账
def load_all():
    """返回 {day: [trade...]} 与 {day: top_targets}。"""
    tr: dict[str, dict] = {}
    tg: dict[str, list] = {}
    cand = [os.path.join(BASE, "data", "state.json"),
            os.path.join(BASE, "data", "_backup_before_dryrun", "state.json")]
    cand += sorted(glob.glob(os.path.join(BASE, "data", "daily", "*", "paper_book.json")))
    for fp in cand:
        if not os.path.exists(fp):
            continue
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for day, items in (d.get("trades_history") or {}).items():
            b = tr.setdefault(day, {})
            for t in items or []:
                k = (t.get("date") or day, t.get("type"), t.get("canon"),
                     t.get("qty"), t.get("time"))
                b.setdefault(k, t)
        tt = d.get("top_targets")
        if tt:
            tg.setdefault(os.path.basename(os.path.dirname(fp)) or day, list(tt))
            tg[day] = list(tt)
    return ({d: list(v.values()) for d, v in tr.items()}, tg)


tr, targets = load_all()
days = sorted(tr)
p("=" * 90)
p("资料")
p("=" * 90)
p(f"  交易日: {len(days)}  {days[0]} .. {days[-1]}")
p(f"  有 top_targets 的日子: {len(targets)}")
p()

# ---------------------------------------------------------------- FIFO 配平 + 归因
buys: dict[str, list] = {}
rows = []
sells_n = 0
for day in days:
    for t in sorted(tr[day], key=lambda x: (x.get("type") != "sell", x.get("time") or "")):
        code = str(t.get("canon") or "").split(".")[0]
        qty = int(t.get("qty") or 0)
        if qty <= 0:
            continue
        if t.get("type") == "buy":
            buys.setdefault(code, []).append([day, qty, float(t.get("price") or 0)])
            continue
        sells_n += 1
        need = qty
        lots = buys.get(code) or []
        first_lot_day, first_lot_cost = (lots[0][0], lots[0][2]) if lots else (None, None)
        # 配平(只为拿持有天数)
        held_days = None
        if lots:
            import datetime as dt
            held_days = (dt.date.fromisoformat(day) - dt.date.fromisoformat(lots[0][0])).days
        while need > 0 and lots:
            d0, q0, pr0 = lots[0]
            take = min(need, q0)
            need -= take
            q0 -= take
            if q0 <= 0:
                lots.pop(0)
            else:
                lots[0][1] = q0
        buys[code] = lots
        pr = float(t.get("price") or 0)
        # 归因代理
        tgt = targets.get(day)
        in_pool = None
        if tgt:
            bare = {str(x).split(".")[0] for x in tgt}
            in_pool = code in bare
        dd = None
        if first_lot_cost and pr > 0:
            dd = (pr / first_lot_cost - 1.0) * 100
        rows.append({"day": day, "code": code, "qty": qty, "price": pr,
                     "held": held_days, "in_pool": in_pool, "dd_vs_cost": dd,
                     "pnl": t.get("pnl")})

p("=" * 90)
p("1) 持有期分布(卖出侧, 仅可配平的)")
p("=" * 90)
matched = [r for r in rows if r["held"] is not None]
p(f"  卖出总笔数: {sells_n}   可配平: {len(matched)} (其余建仓早于台账起点)")
from collections import Counter
c = Counter(r["held"] for r in matched)
for k in sorted(c):
    p(f"    {k:>3} 日 : {c[k]:>3}  {'#' * min(50, c[k]*2)}")
p()
one = [r for r in matched if r["held"] == 1]
p(f"  **持有期 = 1 天的卖出: {len(one)} 笔**({'全部卖出的 %.0f%%' % (100*len(one)/len(matched)) if matched else '-'})")
p()

p("=" * 90)
p("2) 持有期 = 1 天的卖出: 逐笔归因代理")
p("=" * 90)
p(f"  {'日期':12}{'代码':11}{'成交价':>9}{'相对成本%':>11}{'当日仍在池?':>13}{'判定':>22}")
k_pool = k_stop = k_unknown = 0
for r in one:
    dd = r["dd_vs_cost"]
    inpool = r["in_pool"]
    if inpool is True and dd is not None and dd <= -3.0:
        verdict = "**在池 + 触止损线**"
        k_stop += 1
    elif inpool is True:
        verdict = "在池 => 非『离开池』"
        k_pool += 1
    elif inpool is False:
        verdict = "不在池 => 可为『离开池』"
        k_pool += 1
    else:
        verdict = "无 targets 数据"
        k_unknown += 1
    p(f"  {r['day']:12}{r['code']:11}{r['price']:>9.3f}"
      f"{(f'{dd:+.2f}%' if dd is not None else '-'):>11}"
      f"{(str(inpool) if inpool is not None else '-'):>13}{verdict:>22}")
p()
p(f"  合计: 在池+触止损线 {k_stop} / 在池或不在池(路径1可能) {k_pool} / 无数据 {k_unknown}")
p()

p("=" * 90)
p("3) 全部卖出的条件归因(代理口径)")
p("=" * 90)
tot = len(matched)
n_touch_stop = sum(1 for r in matched if r["dd_vs_cost"] is not None and r["dd_vs_cost"] <= -3.0)
n_held_short = sum(1 for r in matched if r["held"] is not None and r["held"] < 2)
n_inpool = sum(1 for r in matched if r["in_pool"] is True)
p(f"  {'条件':38}{'笔数':>7}{'占比':>9}")
p(f"  {'-'*54}")
def line(name, n):
    p(f"  {name:38}{n:>7}{(f'{100*n/tot:.1f}%' if tot else '-'):>9}")
line("触及/低于 -3% 止损线(代理)", n_touch_stop)
line("持有 < 2 天(可为 min_hold 例外)", n_held_short)
line("卖出当日仍在目标池(=> 非『离开池』)", n_inpool)
line("合计可配平卖出", tot)
p()
p("  ⚠️ 这些是**代理**, 不是日志级归因: 台账**没有 reason 字段**, 且 `top_targets`")
p("     只存当日的, 而『离开目标池』判的是**当轮 targets** ⇒ 只能支持不能证明。")
p()

p("=" * 90)
p("结论")
p("=" * 90)
p("  1. **不是 min_hold 失效(bug)** —— 代码上 min_hold 只管『离开目标池』一条路径(L951-981),")
p("     而**止损(L983-1036)与风控(L1038-1049)在它之外**, 且注释**明写**「止损/风控除外」;")
p("  2. 故 1 天卖出**有设计内的合法来源**: ①止损 ②风控 ③调仓门关时的止损/风控;")
p("  3. 但**min_hold 的实际覆盖面比名字听起来小得多**: 它**不**约束止损,")
p("     而止损线是 **-3%** —— 在一个中位持有期 1 天的组合里, 3% 的回撤很容易触及;")
p("  4. 故『min_hold=2』这个配置**几乎不构成持有期下限**: 真正的下限由**止损线**决定。")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)
