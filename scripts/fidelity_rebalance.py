# -*- coding: utf-8 -*-
"""B 归因(第 3 批): 回测保真度 —— "持有 120 天" vs "定期调仓".

背景
  当前 vnpy 回测是"决策日买入 10 只, 持有 120 个交易日不动";
  而实盘 realtime_engine 是**每日按新选股调仓**。两者不是同一件事:
    ① 定期调仓会带来换手成本(当前回测 120 天内只买一次, 几乎没有卖出成本);
    ② 定期调仓会换股, 组合成分随时间漂移(可能变好也可能变差)。
  不量化这个偏差, 就无法判断"换信号后实盘会怎样"。

做法(成本可控的近似)
  完全按日调仓需要 11 窗口 × 120 日 = 1320 次 PIT 选股(实测每次 30~160s, 约 20+ 小时),
  故改为**每 20 个交易日调仓一次**(120 天窗口内 6 次), 在同一批决策日上对比:
    A) 静态持有 120 天(现有回测口径)
    B) 每 20 交易日按当日 PIT 选股重建 10 只等权组合
  并对 B 计入换手成本: 换掉 k/10 只 -> 成本 = (k/10) × 0.162%
  (卖出 0.106% = 佣金0.025%+印花0.05%+过户0.001%+滑点0.01%+冲击0.02%,
   买入 0.056% = 佣金0.025%+过户0.001%+滑点0.01%+冲击0.02%, 与 config.PAPER 一致)

用法: python scripts/fidelity_rebalance.py [--windows 2022-12-30 2024-07-03]
输出: data/fidelity_rebalance.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import pandas as pd  # noqa: E402

from vnpy_backtest import (_load_bars_forward, _load_pit_selection_cached,  # noqa: E402
                           forward_window_days)

FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")
OUT = os.path.join(_BASE, "data", "fidelity_rebalance.json")
STEP = 20            # 调仓间隔(交易日)
COST_SWAP = 0.00162  # 卖出+买入一次的合计费率


def _names(day: str, top: int = 10) -> list[str]:
    return [t["canon"] for t in _load_pit_selection_cached(day, 20)][:top]


def _ret(day: str, end: str, top: int = 10):
    """[day, end] 区间等权篮子收益(**百分数**). 返回 (ret%, 有效只数).

    注意: _load_bars_forward 在 DB 读取瞬时失败时会静默返回空表, 若不加约束会
    用极少数标的算均值(实测并发读取时出现过 1~2 只 -> 收益严重失真)。故此处返回
    有效只数, 由调用方校验。
    """
    rs = []
    for canon in _names(day, top):
        s6 = canon.split(".")[0]
        df = _load_bars_forward(s6, pd.Timestamp(day).date(), 200)
        if df is None or df.empty:
            continue
        c = pd.to_numeric(df["adj_close"], errors="coerce")
        dts = pd.to_datetime(df["date"])
        sel = (dts >= pd.Timestamp(day)) & (dts <= pd.Timestamp(end))
        v = c[sel].dropna()
        if len(v) < 2:
            continue
        rs.append(float(v.iloc[-1] / v.iloc[0] - 1.0) * 100.0)
    if not rs:
        return None, 0
    return st.mean(rs), len(rs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", nargs="*", default=None)
    args = ap.parse_args()

    fw = {x["day"]: x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")}
    days = args.windows or list(fw)
    print(f"保真度对比: {len(days)} 个决策日; 调仓间隔 {STEP} 交易日\n")
    print(f"{'决策日':<12}{'静态持有%':>10}{'定期调仓%':>11}{'成本%':>8}"
          f"{'调仓后%':>10}{'差值pp':>9}{'调仓次数':>9}{'累计换手%':>10}{'新名/次':>9}")

    rows = []
    for day in days:
        if day not in fw:
            print(f"  {day}: 无前向结果, 跳过")
            continue
        t0 = time.time()
        end = fw[day]["stats"].get("end_date")
        cal = forward_window_days(pd.Timestamp(day).date(), 120)
        if not cal:
            print(f"  {day}: 未来数据不足, 跳过")
            continue
        # 静态持有
        static_ret, n_static = _ret(day, cal[-1])
        if n_static < 8:
            print(f"  {day}: 静态篮子仅取到 {n_static} 只(<8), 数据读取异常, 跳过本窗口")
            continue
        # 定期调仓: 每 STEP 个交易日
        pts = list(range(0, len(cal), STEP))
        eq, cost_sum, n_swaps, prev = 1.0, 0.0, 0, None
        new_counts = []
        seg_bad = 0
        for i, p in enumerate(pts):
            reb = cal[p]
            seg_end = cal[min(p + STEP, len(cal)) - 1]
            nm = set(_names(reb, 10))
            if prev is not None:
                k = len(nm - prev)
                new_counts.append(k)
                c = (k / 10.0) * COST_SWAP
                cost_sum += c
                eq *= (1.0 - c)
                n_swaps += 1
            prev = nm
            r, nseg = _ret(reb, seg_end)
            if r is None:
                seg_bad += 1
                continue
            if nseg < 8:
                seg_bad += 1
            eq *= (1.0 + r / 100.0)
        if seg_bad:
            print(f"  {day}: {seg_bad} 个分段数据不足, 结果不可信, 跳过")
            continue
        rebal_net = (eq - 1.0) * 100.0
        rebal_gross = rebal_net + cost_sum * 100.0
        rows.append({"day": day, "static": static_ret, "rebal_gross": rebal_gross,
                     "rebal_net": rebal_net, "cost": cost_sum * 100.0,
                     "n_rebal": len(pts), "avg_new": st.mean(new_counts) if new_counts else None,
                     "turnover_pct": (st.mean(new_counts) / 10.0 * 100.0 * n_swaps)
                     if new_counts else None, "n_static": n_static})
        print(f"{day:<12}{(static_ret if static_ret is not None else float('nan')):>10.2f}"
              f"{rebal_gross:>11.2f}{cost_sum * 100.0:>8.2f}{rebal_net:>10.2f}"
              f"{(rebal_net - (static_ret or 0.0)):>9.2f}{len(pts):>9}"
              f"{(rows[-1]['turnover_pct'] or 0.0):>10.1f}"
              f"{(rows[-1]['avg_new'] or 0.0):>9.1f}   ({time.time() - t0:.0f}s)", flush=True)

    if rows:
        s = [r["static"] for r in rows if r["static"] is not None]
        n = [r["rebal_net"] for r in rows]
        c = [r["cost"] for r in rows]
        print(f"\n=== 汇总({len(rows)} 窗口) ===")
        print(f"静态持有 均值 {st.mean(s):+.2f}%   中位 {st.median(s):+.2f}%")
        print(f"定期调仓 均值 {st.mean(n):+.2f}%   中位 {st.median(n):+.2f}%")
        print(f"平均成本 {st.mean(c):.2f}%   保真度偏差(调仓-持有) {st.mean(n) - st.mean(s):+.2f}pp")
        print(f"平均每次换掉新名 {st.mean([r['avg_new'] for r in rows if r['avg_new'] is not None]):.1f}/10 只")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()
