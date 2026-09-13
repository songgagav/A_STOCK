# -*- coding: utf-8 -*-
"""IC 期限结构: 同一决策日截面, 在 1/5/10/20/60/120 交易日视界上的 RankIC.

用 data/pit/xsec/<day>.parquet (全截面因子打分缓存) + 由 change_pct 复利重建的前向收益。
输出均值/中位/正比例, 并按决策树判定所属分支:
    短正长负 -> 信号有反转(改持有期)   全负 -> 信号无效(换信号源)   短负长正 -> 方向反了

用法: python scripts/ic_term_structure.py
输出: data/ic_term_structure.json
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import pandas as pd  # noqa: E402

from vnpy_backtest import forward_window_days  # noqa: E402

XSEC = os.path.join(_BASE, "data", "pit", "xsec")
FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")
OUT = os.path.join(_BASE, "data", "ic_term_structure.json")
HORIZONS = [1, 5, 10, 20, 60, 120]


def _sql(q: str):
    from factor_fusion import _sql as f
    return f(q)


def ret_at(day: str, end: str) -> pd.DataFrame:
    """(day, end] 各标的复利收益(%), 单次 SQL."""
    q = ("SELECT symbol AS s6, EXP(SUM(LN(1 + change_pct / 100.0))) - 1 AS r "
         "FROM daily_bars "
         f"WHERE CAST(ts AS DATE) > DATE '{day}' AND CAST(ts AS DATE) <= DATE '{end}' "
         "AND change_pct IS NOT NULL GROUP BY symbol")
    df = _sql(q)
    if df is None or df.empty:
        return pd.DataFrame(columns=["s6", "r"])
    df["s6"] = df["s6"].astype(str).str.zfill(6)
    df["r"] = pd.to_numeric(df["r"], errors="coerce")
    return df


def main() -> None:
    import datetime as dt

    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    days = [x["day"] for x in fw]
    print(f"IC 期限结构: {len(days)} 个决策日 × 视界 {HORIZONS}\n")

    table: dict[int, list] = {h: [] for h in HORIZONS}
    detail = []
    for day in days:
        fp = os.path.join(XSEC, f"{day}.parquet")
        if not os.path.exists(fp):
            print(f"  {day}: 无截面缓存, 跳过")
            continue
        x = pd.read_parquet(fp)
        if "sym6" not in x.columns:
            x = x.assign(sym6=x["canon"].astype(str).str.split(".").str[0].str.zfill(6))
        d0 = dt.date.fromisoformat(day)
        row = {"day": day}
        for h in HORIZONS:
            # forward_window_days(d0, n) 以"首个 >= d0 的交易日"为起点(即 d0 本身),
            # 故第 h 个交易日之后需取 n = h+1
            seg = forward_window_days(d0, h + 1)
            if len(seg) < h + 1:
                row[h] = None
                continue
            fr = ret_at(day, seg[-1])
            m = x.merge(fr, left_on="sym6", right_on="s6", how="inner")
            m = m[pd.to_numeric(m["signal"], errors="coerce").notna()]
            m = m[m["r"].notna()]
            if len(m) < 50:
                row[h] = None
                continue
            ic = float(m["signal"].astype(float).corr(m["r"].astype(float), method="spearman"))
            row[h] = ic
            table[h].append(ic)
        detail.append(row)
        print("  " + day + "  " + "  ".join(
            f"{h}d={'n/a' if row.get(h) is None else f'{row[h]:+.4f}'}" for h in HORIZONS),
            flush=True)

    print(f"\n=== 融合因子 IC 期限结构 (n={len(detail)} 决策日) ===")
    print(f"{'视界':>6}{'均值':>10}{'中位':>10}{'正比例':>9}{'min':>9}{'max':>9}  判定")
    summ = {}
    for h in HORIZONS:
        v = table[h]
        if not v:
            print(f"{h:>6}{'n/a':>10}")
            continue
        mean, med = st.mean(v), st.median(v)
        pos = sum(1 for t in v if t > 0)
        summ[h] = {"mean": mean, "median": med, "pos": pos, "n": len(v)}
        print(f"{h:>6}{mean:>10.4f}{med:>10.4f}{f'{pos}/{len(v)}':>9}"
              f"{min(v):>9.4f}{max(v):>9.4f}  {'显著为负' if mean < -0.02 else ('有效' if mean > 0.02 else '接近 0')}")

    short = [summ[h]["mean"] for h in (1, 5, 10) if h in summ]
    long_ = [summ[h]["mean"] for h in (60, 120) if h in summ]
    print("\n=== 决策树判定 ===")
    if short and long_:
        s, l = st.mean(short), st.mean(long_)
        print(f"  短视界(1/5/10d) 均值 {s:+.4f}   长视界(60/120d) 均值 {l:+.4f}")
        if s > 0.02 and l < -0.02:
            verdict = "短正长负 -> 信号有反转: 检查因子符号处理 + 修改持有期"
        elif s < -0.02 and l > 0.02:
            verdict = "短负长正 -> 方向反了: 反转信号 + 修改持有期"
        elif s < -0.02 and l < -0.02:
            verdict = "全负 -> 信号无效: 确认门控可回溯后进入换信号流程"
        else:
            verdict = "均接近 0 -> 无可靠方向: 优先修因子构造/换信号源"
        print(f"  => {verdict}")
        summ["verdict"] = verdict

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"summary": summ, "detail": detail}, f, ensure_ascii=False, indent=2)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()
