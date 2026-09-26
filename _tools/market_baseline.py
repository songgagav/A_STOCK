# -*- coding: utf-8 -*-
"""P0 基线: 组合回测窗口(2026-09-11..09-24)的**市场等权收益**。

## 为什么要算这个

`portfolio_backtest_latest.json` 的 `total_return_pct = -5.7879%` 是**绝对**收益。
没有市场基线就无法判断它是"选股差"还是"大盘跌" ——
而这两者的处置完全相反(前者查信号, 后者查暴露/对冲)。

用 h5i `daily_bars` 在同一窗口上算**全市场等权**与**池内等权**收益作基线。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import h5i_bar_store as S  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_market_baseline.txt")
lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st = S.open_store()
days = st.trading_days()

# 组合回测窗口
bt = json.load(open("data/portfolio_backtest_latest.json", encoding="utf-8"))
win = bt["days"]
p(f"组合回测窗口: {win[0]} .. {win[-1]}  ({len(win)} 天)")
p(f"组合 total_return_pct = {bt['total_return_pct']}%  "
  f"max_dd = {bt['max_drawdown_pct']}%  turnover = {bt['turnover_pct']}%")
p()

# 取窗口内的交易日(与 h5i 对齐)
win_in_h5i = [d for d in days if win[0] <= d <= win[-1]]
p(f"落在 h5i 里的交易日: {win_in_h5i}")
if len(win_in_h5i) < 2:
    p("!! 不足两天, 无法算收益")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print("written:", OUT)
    raise SystemExit(0)

d0, d1 = win_in_h5i[0], win_in_h5i[-1]
p(f"基线区间: {d0} -> {d1}")
p()

# 用 bars_on_day 批量取(比逐标的 close_upto 快得多)
# 注: store.close() 是**关闭数据库**, 不是取收盘价 —— 首次写错过。
def closes(day: str) -> dict:
    df = st.bars_on_day(day)
    out = {}
    for r in df.itertuples(index=False):
        try:
            out[str(r.symbol)] = float(r.close)
        except Exception:  # noqa: BLE001
            continue
    return out


c0, c1 = closes(d0), closes(d1)
common = sorted(set(c0) & set(c1))
p(f"两日都有数据的标的: {len(common)}  (d0={len(c0)}, d1={len(c1)})")


def ret(c: str) -> float | None:
    a, b = c0.get(c), c1.get(c)
    if not a or not b or a <= 0:
        return None
    r = b / a - 1.0
    return r if -0.9 < r < 5.0 else None      # 剔极端/脏数据


rs = [r for r in (ret(c) for c in common) if r is not None]
if not rs:
    p("!! 没有可算的收益, 检查 symbol 格式")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print("written:", OUT)
    raise SystemExit(0)
eq = sum(rs) / len(rs) * 100.0
med = sorted(rs)[len(rs) // 2] * 100.0
p()
p(f"=== 全市场等权收益 ({d0}->{d1}) ===")
p(f"  n={len(rs)}  等权均值 = {eq:+.4f}%  中位 = {med:+.4f}%")
p(f"  组合为 {bt['total_return_pct']:+.4f}%  => 超额 = "
  f"{bt['total_return_pct'] - eq:+.4f}pp")

# 池内等权(组合实际持有的标的)
hold = sorted({s for s in bt.get("symbols", [])})
p()
p(f"=== 组合持有过的标的 n={len(hold)} ===")
hrs = []
for h in hold:
    c = h
    # 组合里是 000823.SZ 这种; h5i canon 可能不同, 两种都试
    for cand in (h, h.split(".")[0]):
        if cand in c0 and cand in c1:
            c = cand
            break
    r = ret(c)
    if r is not None:
        hrs.append(r)
if hrs:
    p(f"  池内等权 = {sum(hrs)/len(hrs)*100:+.4f}%  (n={len(hrs)})")
    p(f"  组合 - 池内等权 = {bt['total_return_pct'] - sum(hrs)/len(hrs)*100:+.4f}pp")
else:
    p("  池内标的在 h5i 里找不到匹配 canon, 跳过")

# ---- 逐日: 组合实际持有 10 只是什么? 拆"选股"与"持有期" ----
p()
p("=== 逐日持仓与前向收益(判「选股质量」 vs 「持有期」) ===")
cw = bt.get("combined_weights") or []
# 需要每日的符号顺序; portfolio_backtest 的 symbols 是列顺序
all_syms = bt.get("symbols") or []
if cw and all_syms:
    p(f"  {'day':12} {'持仓数':>6} {'等权持有':>10} {'次日收益':>10}")
    for i, day in enumerate(win_in_h5i):
        # weights 行与 dates 对齐; win_in_h5i 可能比 dates 略少, 用索引保护
        if i >= len(cw):
            break
        row = cw[i]
        held = [all_syms[j] for j, x in enumerate(row) if x and float(x) > 1e-9]
        if not held:
            p(f"  {day:12} {0:>6} {'-':>10} {'-':>10}")
            continue
        # 该日持仓在**次日**的等权收益(用 h5i)
        nxt = win_in_h5i[i + 1] if i + 1 < len(win_in_h5i) else None
        if nxt is None:
            p(f"  {day:12} {len(held):>6} {'-':>10} {'-':>10}   (末日无次日)")
            continue
        cx0, cx1 = closes(day), closes(nxt)
        rr = []
        for h in held:
            c = h if h in cx0 and h in cx1 else h.split(".")[0]
            a, b = cx0.get(c), cx1.get(c)
            if a and b and a > 0:
                rr.append(b / a - 1.0)
        if rr:
            avg = sum(rr) / len(rr) * 100
            # 同期全市场等权(作对照)
            cm = [v / cx0[k] - 1.0 for k, v in cx1.items()
                  if k in cx0 and cx0[k] > 0 and -0.9 < v / cx0[k] - 1.0 < 5.0]
            mkt = sum(cm) / len(cm) * 100 if cm else float("nan")
            p(f"  {day:12} {len(held):>6} {avg:>+9.3f}% {avg - mkt:>+9.3f}pp")
        else:
            p(f"  {day:12} {len(held):>6} {'-':>10} {'-':>10}")

# ---- 决定性对照: 每日持仓的"次日"差, 但用**全窗口**收益 ----
p()
p("=== 每条日选出的 10 只, 在**全窗口**(09-11..09-24)的等权收益 ===")
p("  (对照: 同一批 10 只在**次日**的收益 —— 若前者好而后者差, 说明是**入场时点**问题)")
tot0, tot1 = closes(win_in_h5i[0]), closes(win_in_h5i[-1])
rows = []
for i, day in enumerate(win_in_h5i):
    if i >= len(cw):
        break
    row = cw[i]
    held = [all_syms[j] for j, x in enumerate(row) if x and float(x) > 1e-9]
    if not held:
        continue

    def _r(mp0, mp1):
        rr = []
        for h in held:
            c = h if h in mp0 and h in mp1 else h.split(".")[0]
            a, b = mp0.get(c), mp1.get(c)
            if a and b and a > 0:
                rr.append(b / a - 1.0)
        return (sum(rr) / len(rr) * 100) if rr else None

    full = _r(tot0, tot1)
    nxt = win_in_h5i[i + 1] if i + 1 < len(win_in_h5i) else None
    n1 = _r(closes(day), closes(nxt)) if nxt else None
    rows.append((day, full, n1))

p(f"  {'day':12} {'全窗口收益':>11} {'次日收益':>10}")
fvals, nvals = [], []
for day, full, n1 in rows:
    fs = f"{full:+.3f}%" if full is not None else "-"
    ns = f"{n1:+.3f}%" if n1 is not None else "-"
    p(f"  {day:12} {fs:>11} {ns:>10}")
    if full is not None:
        fvals.append(full)
    if n1 is not None:
        nvals.append(n1)
if fvals:
    p(f"  {'均值':12} {sum(fvals)/len(fvals):>+10.3f}% "
      f"{(sum(nvals)/len(nvals) if nvals else float('nan')):>+9.3f}%")
    p()
    p("  读法: 全窗口均值若明显为正而次日均值为负 ⇒ **选股长期看是对的, 但入场时点差**")
    p("        (典型机制: 买在已被推高的位置)。若两者都负 ⇒ 选股本身有问题。")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)
