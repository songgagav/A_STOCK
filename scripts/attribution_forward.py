# -*- coding: utf-8 -*-
"""前向口径下的策略归因 (attribution_forward.py) — 2026-09-13.

用于回答"策略为什么没有样本外边际"。读取 `data/vnpy_backtest_nonoverlap_fwd_results.json`
(由 scripts/nonoverlap_rerun.py 前向口径产出) 与逐窗口 summary/选股缓存, 输出:

  持仓数   窗口平均持仓只数        < 5 只   => 分散不足
  换手率   窗口内累计换手/资金     折合日均后判断是否成本侵蚀
  盈亏分布 逐名收益的盈亏比         < 1.5    => 亏损过大
  一致性   篮子等权收益 vs 回测收益 差值 = 交易成本+整手取整造成的影响

用法:
    python scripts/attribution_forward.py
输出: data/attribution_b1.json
"""
import json
import os
import statistics as st
import sys

BASE = r"D:\狗屁通のA大奇妙冒险\A_stock_rotation"
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))
os.chdir(BASE)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from vnpy_backtest import _load_bars_forward  # noqa: E402

fw = json.load(open(os.path.join(BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json"),
                    encoding="utf-8"))
ok = [x for x in fw if x.get("ok")]

rows = []
for x in ok:
    day = x["day"]
    day_dir = day.replace("-", "")
    summ = os.path.join(BASE, "data", "vnpy_backtest", day_dir, "summary.json")
    s = {}
    if os.path.exists(summ):
        s = json.load(open(summ, encoding="utf-8"))
        st_ = s.get("stats") or {}
        # 只接受前向产物(区间起点 == 决策日)
        if str(st_.get("start_date")) != day:
            s = {}
            st_ = {}
    else:
        st_ = {}
    n_sym = s.get("n_symbols")
    cap = float(st_.get("capital") or 100000)
    to = st_.get("total_turnover")
    to_ratio = (float(to) / cap * 100.0) if to is not None else None

    # 逐名收益(窗口内), 用于盈亏分布
    rets = []
    sel_fp = os.path.join(BASE, "data", "pit", "selection_cache", f"{day}_n20.json")
    if os.path.exists(sel_fp):
        tg = json.load(open(sel_fp, encoding="utf-8"))["targets"][:10]
        for t in tg:
            s6 = t["canon"].split(".")[0]
            df = _load_bars_forward(s6, pd.Timestamp(day).date(), 120)
            if df is None or df.empty or len(df) < 20:
                continue
            c = pd.to_numeric(df["adj_close"], errors="coerce").dropna()
            if len(c) < 20:
                continue
            rets.append(float(c.iloc[-1] / c.iloc[0] - 1.0) * 100.0)
    wins = [v for v in rets if v > 0]
    losses = [v for v in rets if v <= 0]
    pl = (st.mean(wins) / abs(st.mean(losses))) if wins and losses else None
    rows.append({"day": day, "n_sym": n_sym, "to_ratio": to_ratio,
                 "n_names": len(rets), "win_rate": (len(wins) / len(rets) * 100) if rets else None,
                 "avg_win": st.mean(wins) if wins else None,
                 "avg_loss": st.mean(losses) if losses else None,
                 "pl_ratio": pl,
                 "basket": st.mean(rets) if rets else None,
                 "bt": float(st_.get("total_return")) if st_.get("total_return") is not None else None})

print(f"{'决策日':<12}{'持仓只':>7}{'窗口换手%':>10}{'逐名数':>7}{'胜率%':>7}"
      f"{'均盈%':>8}{'均亏%':>8}{'盈亏比':>7}{'篮子%':>8}{'回测%':>8}")
for r in rows:
    f = lambda v, p=2: ("n/a" if v is None else f"{v:.{p}f}")
    print(f"{r['day']:<12}{str(r['n_sym']):>7}{f(r['to_ratio'],1):>10}{r['n_names']:>7}"
          f"{f(r['win_rate'],0):>7}{f(r['avg_win']):>8}{f(r['avg_loss']):>8}"
          f"{f(r['pl_ratio']):>7}{f(r['basket']):>8}{f(r['bt']):>8}")

def agg(key):
    v = [r[key] for r in rows if r[key] is not None]
    return st.mean(v) if v else None

print("\n=== 汇总(11 窗口) ===")
print(f"持仓只数   均值 {agg('n_sym'):.1f}   最小 {min(r['n_sym'] for r in rows if r['n_sym'])}"
      f"   [判定: <5 只=分散不足]")
print(f"窗口换手率 均值 {agg('to_ratio'):.1f}%  (一次性建仓+末段清仓, 120 交易日)"
      f"  [判定: >50% 需看是否成本侵蚀]")
print(f"逐名胜率   均值 {agg('win_rate'):.1f}%")
print(f"平均盈利   +{agg('avg_win'):.2f}%    平均亏损 {agg('avg_loss'):.2f}%")
print(f"盈亏比     均值 {agg('pl_ratio'):.2f}   [判定: <1.5 说明亏损过大]")
print(f"\n篮子等权收益 均值 {agg('basket'):+.2f}%   回测收益 均值 {agg('bt'):+.2f}%")
print(f"篮子 vs 回测 差 {agg('basket') - agg('bt'):+.2f}pp  (交易成本+整手取整)")

out = os.path.join(BASE, "data", "attribution_b1.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(rows, fh, ensure_ascii=False, indent=2)
print("已保存:", out)
