# -*- coding: utf-8 -*-
"""入场后收益衰减 / 各入场时点终值 / min_hold=20 实际持仓 —— 全部用**同一份权重矩阵**。

## 为什么要"同一份权重矩阵"

用户要求的三件事必须**可比**:
  ① 每日选出的 10 只「入场后 1/5/10/20 日」收益;
  ② 每日选股「从该天到窗口末」收益;
  ③ 第一天选股 vs 组合实际。

若各用各的取数, 三者之间的差就混进了"口径差"。故先一次性取
`portfolio_live` 的目标权重矩阵(与生产回测**同源**), 之后全部在它上面算。

## 读数规则(用户给定)

· 入场后 1 日负、但 5/10/20 日转正 ⇒ **持有期问题**(选股对, 拿不住);
· 入场后 1 日负、且 5/10/20 日仍负 ⇒ **选股问题**。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np  # noqa: E402
import h5i_bar_store as S  # noqa: E402
import portfolio_backtest as PB  # noqa: E402
import portfolio_live as PL  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_entry_decay.txt")
lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st = S.open_store()
ALL_DAYS = st.trading_days()


def closes(day: str) -> dict:
    """返回 `{bare_code: close}` —— h5i 的 symbol 是**裸 6 位代码**(实测 `600016`),
    而权重矩阵用的是带后缀的 canon(`600016.SH`)。故统一按**裸码**建索引。

    [2026-09-27 自查] 第一版直接拿 `600016.SH` 去查, **全部 miss** ⇒
    ① ② ③ 三节的曲线全空。而 `nan` 在表格里显示为 `-`, 看起来像"数据不足"
    而不是"字段名写错" —— 差点把"查不到"读成"还没到时候"。
    故这里显式做裸码归一, 并在下面断言"至少命中一些", 让这类错误响起来。
    """
    df = st.bars_on_day(day)
    out = {}
    for r in df.itertuples(index=False):
        try:
            out[str(r.symbol)] = float(r.close)
        except Exception:  # noqa: BLE001
            continue
    return out


def bare(canon: str) -> str:
    return str(canon).split(".")[0]


# ---------------------------------------------------------------- 取权重矩阵
bt = json.load(open("data/portfolio_backtest_latest.json", encoding="utf-8"))
WIN = list(bt["days"])
tf = PL.make_targets_of(live_pool=False)
syms, W = PL.weights_from_targets(WIN, tf)
p(f"窗口: {WIN[0]} .. {WIN[-1]}  ({len(WIN)} 天)   标的并集: {len(syms)}")
p(f"权重矩阵: {W.shape}   每日非零列数: "
  f"{[int((W[i] > 1e-9).sum()) for i in range(W.shape[0])]}")
p(f"每日权重和: {[round(float(W[i].sum()), 4) for i in range(W.shape[0])]}")
p()

# 权重口径: 每日 top10 是否等权?
row0 = W[0]
nz0 = row0[row0 > 1e-9]
p(f"首日权重取值(前 10): {[round(float(x), 5) for x in sorted(nz0, reverse=True)[:10]]}")
p(f"首日是否等权: {bool(np.allclose(nz0, nz0[0], atol=1e-6)) if len(nz0) else 'n/a'}")
p()

# ------------------------------------------------- ① 入场后 1/5/10/20 日衰减
HORIZONS = (1, 5, 10, 20)
p("=" * 78)
p("① 入场后收益衰减曲线(每日选出的 10 只, 等权)")
p("=" * 78)
px_cache: dict[str, dict] = {d: closes(d) for d in ALL_DAYS}


def fwd_ret(day: str, hold: list[str], h: int):
    """从 day 起、持有 h 个交易日后的等权收益。不足则 None。"""
    try:
        i = ALL_DAYS.index(day)
    except ValueError:
        return None
    j = i + h
    if j >= len(ALL_DAYS):
        return None
    d1 = ALL_DAYS[j]
    m0, m1 = px_cache[day], px_cache[d1]
    rr = []
    for c in hold:
        k = bare(c)
        a, b = m0.get(k), m1.get(k)
        if a and b and a > 0:
            rr.append(b / a - 1.0)
    return (sum(rr) / len(rr) * 100) if rr else None


p(f"  {'day':12}{'n':>4}" + "".join(f"{'h'+str(h):>10}" for h in HORIZONS))
acc = {h: [] for h in HORIZONS}
_checked = False
for i, day in enumerate(WIN):
    hold = [syms[j] for j in np.nonzero(W[i] > 1e-9)[0]]
    if not hold:
        continue
    if not _checked:
        # **响起来**: 若一个都查不到, 说明字段/格式不对 —— 必须报错,
        # 不能让空曲线冒充"数据不足"(2026-09-27 实测踩过: `.SH` 后缀导致全 miss)
        m = px_cache.get(day, {})
        hit = sum(1 for c in hold if bare(c) in m)
        assert hit > 0, (
            f"持有标的一个都没在 h5i 里命中(day={day}) —— "
            f"检查 symbol 格式: h5i 是裸码, canon 带后缀。样本: {hold[:3]}")
        _checked = True
    row = []
    for h in HORIZONS:
        v = fwd_ret(day, hold, h)
        row.append(v)
        if v is not None:
            acc[h].append(v)
    p(f"  {day:12}{len(hold):>4}"
      + "".join((f"{v:>+9.3f}%" if v is not None else f"{'-':>10}") for v in row))
p(f"  {'均值':12}{'':>4}"
  + "".join((f"{sum(acc[h])/len(acc[h]):>+9.3f}%" if acc[h] else f"{'-':>10}")
            for h in HORIZONS))
p(f"  {'样本数':12}{'':>4}" + "".join(f"{len(acc[h]):>10}" for h in HORIZONS))
p()
p("  读法: 1 日负而后转正 => **持有期问题**; 一路为负 => **选股问题**。")
p()

# ------------------------------------------------- ② 各入场时点 -> 窗口末
p("=" * 78)
p("② 每日选股「从该天持有到窗口末」的等权收益")
p("=" * 78)
d_end = ALL_DAYS[ALL_DAYS.index(WIN[-1])]
m_end = px_cache[d_end]
p(f"  {'day':12}{'n':>4}{'-> 窗口末':>14}")
e2e = []
for i, day in enumerate(WIN):
    hold = [syms[j] for j in np.nonzero(W[i] > 1e-9)[0]]
    if not hold:
        continue
    m0 = px_cache[day]
    rr = [m_end[bare(c)] / m0[bare(c)] - 1.0 for c in hold
          if m0.get(bare(c)) and m_end.get(bare(c)) and m0[bare(c)] > 0]
    if rr:
        v = sum(rr) / len(rr) * 100
        e2e.append(v)
        p(f"  {day:12}{len(hold):>4}{v:>+13.3f}%")
    else:
        p(f"  {day:12}{len(hold):>4}{'-':>14}")
p(f"  {'均值':12}{'':>4}{sum(e2e)/len(e2e):>+13.3f}%" if e2e else "  (无)")
p()

# ------------------------------------------------- ③ 第一天 vs 组合实际
p("=" * 78)
p("③ 第一天选股 vs 组合实际")
p("=" * 78)
d0 = WIN[0]
hold0 = [syms[j] for j in np.nonzero(W[0] > 1e-9)[0]]
m0 = px_cache[d0]
rr0 = [m_end[bare(c)] / m0[bare(c)] - 1.0 for c in hold0
       if m0.get(bare(c)) and m_end.get(bare(c)) and m0[bare(c)] > 0]
first = sum(rr0) / len(rr0) * 100 if rr0 else float("nan")
p(f"  第一天({d0})选出的 {len(hold0)} 只, 持有到 {d_end}: **{first:+.4f}%**")
p(f"  组合实际(改造前, min_hold=0):        **{bt['total_return_pct']:+.4f}%**")
p(f"  => 调仓过程吃掉: **{bt['total_return_pct'] - first:+.4f}pp**")
p()
p("  读法: 第一天选股为正而组合为负 => **调仓过程吃掉了收益**(持有期/换手问题)。")
p()

# ------------------------------------------------- ④ min_hold=20 实际持仓
p("=" * 78)
p("④ min_hold=20 的实际持仓明细")
p("=" * 78)
from config import PAPER  # noqa: E402
import paper_book  # noqa: E402


def run_mh(mh: int) -> dict:
    old = dict(paper_book.PAPER)
    try:
        paper_book.PAPER["min_hold_days"] = mh
        return PB.simulate_portfolio(
            WIN, syms, W,
            price_of=PL.make_price_of(None),
            tradable_of=PL.make_tradable_of(None),
            invest_ratio=None, init_capital=None)
    finally:
        paper_book.PAPER.clear()
        paper_book.PAPER.update(old)


res20 = run_mh(20)
p(f"  min_hold=20: return={res20['total_return_pct']:+.4f}%  "
  f"turnover={res20['turnover_pct']:.2f}%  trades={res20['trades']}")
p()
tl = res20.get("trade_log", [])
p(f"  逐笔明细({len(tl)} 笔):")
for t in tl:
    p(f"    {t['day']}  {t['side']:4}  {t['canon']:12}  qty={t['qty']:>7}  px={t['price']}")
p()
held_days = sorted({t["day"] for t in tl if t["side"] == "buy"})
p(f"  有买入的日子: {held_days}")
p(f"  是否只买了第一天选出的 10 只? "
  f"{'是' if held_days and held_days[0] == d0 else '否(首买日 ' + str(held_days[:1]) + ')'}")
p()

# ------------------------------------------------- ⑤ 多窗口 min_hold 对比
p("=" * 78)
p("⑤ 多窗口 min_hold 对比(分层: 各窗口市场环境不同)")
p("=" * 78)


def win_ret(days: list) -> float:
    """窗口内全市场等权收益(作环境分层)。"""
    a, b = px_cache[days[0]], px_cache[days[-1]]
    rr = [b[c] / a[c] - 1.0 for c in set(a) & set(b)
          if a[c] > 0 and -0.9 < b[c] / a[c] - 1 < 5]
    return sum(rr) / len(rr) * 100 if rr else float("nan")


# 用 h5i 里所有可切出的 10 天窗口(步长 1)。
#
# [2026-09-27 自查] 第一版用 `range(ALL_DAYS.index(WIN[0]), ALL_DAYS.index(WIN[-1])-9+1)`
# ⇒ 起止索引相同 ⇒ 空 range ⇒ 只切出 1 个窗口, 看起来像"数据不够"。
# **实际原因是我的区间算错了**: 5 级回退梯子(`_select_targets_hist`)对更早的日子
# 也能给出目标池(实测 08-26 / 08-31 / 09-07 / 09-08 都返回 10 只)。
# 故改为**按可服务日期**切窗, 而不是按那个静态产物里的窗口端点。
cand_days = [d for d in ALL_DAYS if d <= ALL_DAYS[-1]]
served = []
for d in cand_days:
    try:
        if tf(d):
            served.append(d)
    except Exception:  # noqa: BLE001
        pass
p(f"  目标池可服务的交易日: n={len(served)}  范围 {served[0]} .. {served[-1]}")
starts = []
for i in range(len(served) - 10 + 1):
    w = served[i:i + 10]
    # 只接受**连续交易日**的窗口(中间不能跳日, 否则"10 天"名不副实)
    if w == [d for d in ALL_DAYS if w[0] <= d <= w[-1]][:10]:
        starts.append(w[0])
starts = list(dict.fromkeys(starts))
p(f"  可切出的连续 10 天窗口数: **{len(starts)}**")
p()
p(f"  {'窗口':26}{'市场%':>9}" + "".join(f"{'mh'+str(m):>10}" for m in (2, 5, 10, 20)))
wins = []
for st_day in starts:
    st_i = ALL_DAYS.index(st_day)
    wdays = ALL_DAYS[st_i:st_i + 10]
    s2, W2 = PL.weights_from_targets(wdays, tf)
    if W2.size == 0:
        continue
    mkt = win_ret(wdays)
    row = {}
    for mh in (2, 5, 10, 20):
        old = dict(paper_book.PAPER)
        try:
            paper_book.PAPER["min_hold_days"] = mh
            r = PB.simulate_portfolio(
                wdays, s2, W2,
                price_of=PL.make_price_of(None),
                tradable_of=PL.make_tradable_of(None),
                invest_ratio=None, init_capital=None)
        except Exception as e:  # noqa: BLE001
            r = {"total_return_pct": float("nan"), "turnover_pct": float("nan"),
                 "error": f"{type(e).__name__}: {e}"}
        finally:
            paper_book.PAPER.clear()
            paper_book.PAPER.update(old)
        row[mh] = r
    wins.append((wdays, mkt, row))
    p(f"  {wdays[0]+'..'+wdays[-1]:26}{mkt:>+8.2f}%"
      + "".join(f"{row[mh]['total_return_pct']:>+9.3f}%" for mh in (2, 5, 10, 20)))

p()
p("  汇总(仅口径: 每个窗口各自的市场环境, 故同时给市场列):")
for mh in (2, 5, 10, 20):
    vals = [r[mh]["total_return_pct"] for _, _, r in wins
            if r[mh].get("total_return_pct") is not None
            and not np.isnan(r[mh]["total_return_pct"])]
    turns = [r[mh]["turnover_pct"] for _, _, r in wins
             if r[mh].get("turnover_pct") is not None
             and not np.isnan(r[mh]["turnover_pct"])]
    if vals:
        pos = sum(1 for v in vals if v > 0)
        p(f"    min_hold={mh:<3} n={len(vals):<3} 均值={sum(vals)/len(vals):+.4f}%  "
          f"中位={sorted(vals)[len(vals)//2]:+.4f}%  正={pos}/{len(vals)}  "
          f"换手均值={sum(turns)/len(turns):.1f}%")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)
