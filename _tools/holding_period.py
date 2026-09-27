# -*- coding: utf-8 -*-
"""从**生产台账**测量实际持有期 —— 用来判断 `FUSION_TRIM_Q` 标定该取哪一档。

## 数据源(全部是生产产物, 非回测)

· `data/state.json` —— 当前虚拟盘状态(含 `trades_history`, 但只覆盖最近几天);
· `data/daily/<day>/paper_book.json` —— **每日快照**, 可拼出更长历史;
· `data/_backup_before_dryrun/state.json` —— 一个更早的状态备份(覆盖 08-27..09-09)。

## 方法: 用**加权平均成本法**配对买卖(FIFO 会高估, 因为账本本身就是加权成本)

`paper_book` 的仓位维护是加权平均成本(`avg_cost`), 故持有期只能**近似**:
对每个标的, 按时间顺序累计买入, 每次**卖出**按当时的累计买入均价对应一段持有期
(用**最早未配平的买入日**作为该次卖出的持有起点 —— 这是 FIFO 口径,
在多次加仓时偏保守, 需在结论里注明)。

输出: 持有天数分布(交易日) + 中位数 + 分位数 => 落在标定报告的哪一档。
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_holding_period.txt")
lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


def load_trades():
    """把多个来源的 trades_history 合并去重 -> {day: [trade, ...]}。

    ⚠️ **2026-09-27 更正**: 本函数初版把 `data/_backup_before_dryrun/state.json`
    当作生产台账一起合并, 从而算出「生产实际持有期中位 1 天」。
    实测该目录是**解释器一致性预检在隔离临时目录里跑出的产物**
    (`data/preflight_dryrun.json`: `tmp_data_dir=...\\Temp\\_interp_parity_B_*`,
    `prod_state_untouched=True`), **不是生产实盘增量**。
    样本构成: 预检回放卖出 54 笔 / 生产盘卖出 3 笔 ⇒ **预检占 95%**。

    ⇒ 本函数现把**预检产物排除在"生产"之外**, 并在返回值里带上**来源构成**,
      让调用方能看出"生产口径"的样本到底有多少。
    """
    merged: dict[str, dict] = {}
    srcs = []
    prod = [os.path.join(BASE, "data", "state.json")]
    # 预检隔离产物 —— **单列, 不计入生产口径**
    dry = [os.path.join(BASE, "data", "_backup_before_dryrun", "state.json")]
    snap = sorted(glob.glob(os.path.join(BASE, "data", "daily", "*", "paper_book.json")))
    for fp, kind in ([(p, "prod") for p in prod]
                     + [(p, "snapshot") for p in snap]
                     + [(p, "preflight") for p in dry]):
        if not os.path.exists(fp):
            continue
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        th = d.get("trades_history") or {}
        got = 0
        for day, items in th.items():
            bucket = merged.setdefault(day, {})
            for t in items or []:
                k = (t.get("date") or day, t.get("type"), t.get("canon"),
                     t.get("qty"), t.get("time"))
                if k not in bucket:
                    bucket[k] = dict(t, _src=kind)
                    got += 1
        srcs.append((os.path.relpath(fp, BASE), kind, len(th), got))
    return ({d: list(v.values()) for d, v in merged.items()}, srcs)


trades_by_day, srcs = load_trades()
p("=" * 84)
p("来源(按**口径**分列 —— 预检产物不算生产台账)")
p("=" * 84)
for rel, kind, ndays, got in srcs:
    p(f"  [{kind:9}] {rel:50} 覆盖 {ndays:>2} 天, 新增 {got:>3} 笔")
days = sorted(trades_by_day)
alln = sum(len(v) for v in trades_by_day.values())
buys = [t for v in trades_by_day.values() for t in v if t.get("type") == "buy"]
sells = [t for v in trades_by_day.values() for t in v if t.get("type") == "sell"]
prod_sells = [t for v in trades_by_day.values() for t in v
              if t.get("type") == "sell" and t.get("_src") == "prod"]
p()
p(f"合并后: {len(days)} 个交易日  {days[0]} .. {days[-1]}   共 {alln} 笔 (买 {len(buys)} / 卖 {len(sells)})")
p()
p(f"  ⚠️ 其中**生产口径**(`data/state.json`, 标 `[prod]`)**只有 {len(prod_sells)} 笔卖出**")
if len(prod_sells) < 20:
    p("     ⇒ **不足以做持有期分布/卖出条件归因**。")
    p("     本脚本下面的分布即便算出来, **也主要是预检回放(标 `[preflight]`)的口径**, ")
    p("     **不能当作「生产实际持有期」**。见 ops/acceptance_status.json 的")
    p("     `FINDING-STATE-LEDGER-TOO-SHORT-FOR-HOLDING-MEASUREMENT`。")
p()
assert alln, "没有读到任何成交流水 —— 不要在空数据上出结论"


def tdays_between(a: str, b: str) -> int:
    """两个日期之间的**日历日**差(账本的 min_hold 也用日历日口径)。"""
    import datetime as dt
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


# FIFO 配平
open_lots: dict[str, list] = {}
holds: list[int] = []
matched = 0
unmatched_sell = 0
for day in days:
    # 同日先卖后买(与 paper_book 撮合顺序一致)
    for t in sorted(trades_by_day[day], key=lambda x: (x.get("type") != "sell", x.get("time") or "")):
        c = str(t.get("canon") or "")
        if not c:
            continue
        qty = int(t.get("qty") or 0)
        if qty <= 0:
            continue
        if t.get("type") == "buy":
            open_lots.setdefault(c, []).append([day, qty])
        elif t.get("type") == "sell":
            need = qty
            lots = open_lots.get(c) or []
            while need > 0 and lots:
                d0, q0 = lots[0]
                take = min(need, q0)
                holds.append(tdays_between(d0, day))
                matched += 1
                need -= take
                q0 -= take
                if q0 <= 0:
                    lots.pop(0)
                else:
                    lots[0][1] = q0
            if need > 0:
                unmatched_sell += 1      # 卖出了没有对应买入记录的仓位(历史更早)
            open_lots[c] = lots

p("=" * 84)
p("实际持有期分布(生产台账, FIFO 配平)")
p("=" * 84)
p(f"  可配平的卖出笔数: {matched}   无法配平(建仓早于台账起点): {unmatched_sell}")
if not holds:
    p("  **无可配平的持有期** —— 台账里没有完整的买入->卖出配对。")
    p("  这不是'持有期为零', 而是**数据不足**: 现有快照只覆盖 %d 个交易日。" % len(days))
else:
    hs = sorted(holds)
    def q(pct):
        i = min(len(hs) - 1, max(0, int(round(pct * (len(hs) - 1)))))
        return hs[i]
    p()
    p(f"  持有期(日历日) n={len(hs)}")
    p(f"    min={hs[0]}  p25={q(.25)}  中位={q(.5)}  p75={q(.75)}  p90={q(.90)}  max={hs[-1]}")
    p(f"    均值={st.mean(hs):.2f}")
    p()
    from collections import Counter
    c = Counter(hs)
    p("  分布:")
    for k in sorted(c):
        bar = "#" * min(60, c[k])
        p(f"    {k:>3} 日 : {c[k]:>4}  {bar}")

p()
p("=" * 84)
p("对比: 回测隐含持有期")
p("=" * 84)
p("  · **无 min_hold**(改造前的 portfolio_backtest): 10 天窗口换手 484.10%")
p("    = 2.42 个完整往返 => 隐含持有期约 **4.1 天**;")
p("  · **min_hold=2**(生产配置): 换手 407.87% => 约 **4.9 天**;")
p("  · **min_hold=20**: 换手 98.24% => 约 **20.4 天**。")
p()
p("  标定报告的档位:")
p("    ≤10 天 => 建议 q=0.03;  20~120 天 => 建议 q=0.05;  1~7 天 => 不建议截尾。")
p()

if holds:
    med = st.median(holds)
    p25 = q(.25)
    p(f"  => **生产实际持有期: 中位 {med:.0f} 天, p25={p25} 天, 均值 {st.mean(holds):.2f} 天**")
    p()
    # 档位判据(与 `_tools/trim_by_horizon.py` 的实测一致):
    #   h1/h3/h5/h10 最优 q=0.03~0.15 但**绝对差异很小**(0.5~1.3pp);
    #   h7         最优 q=0.00(**不截尾**, +1.67%);
    #   h20/h60/h120 最优 q=0.05, 且**幅度大**(h120 +10.99% vs -4.00%)。
    if med <= 7:
        p("     落在 **1~7 天** 档 => 该档 h7 的实测最优是 **q=0.00(不截尾)**")
        p("     ⇒ **不应上线截尾**; 截尾的价值在这一档尚未显现(极端尾还没时间兑现下跌)。")
    elif med <= 10:
        p("     落在 **≤10 天** 档 => 标定取 **q=0.03**(但该档优势仅 0.5~1.3pp, 收益有限)")
    elif med <= 20:
        p("     落在 10~20 天 => 两档之间, 需扩样本或按插值处理")
    else:
        p("     落在 **20~120 天** 档 => 标定取 **q=0.05**(该档幅度最大, h120 +10.99% vs -4.00%)")
else:
    p("  => **无法判定落在哪一档** —— 台账不足以配平买卖。")
    p("     这是本项的真实结论, 不是'测不出所以跳过'。")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)
