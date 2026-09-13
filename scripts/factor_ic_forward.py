# -*- coding: utf-8 -*-
"""B 归因(第 2 批): 融合因子在前向窗口的 IC.

方法
  对每个决策日 D, 取**全截面**因子打分(用 RotationSelector(n=大值) 拿到打分后不再截断),
  与该窗口 [D, D+120 交易日] 的前向收益算 Spearman RankIC。
  前向收益用 SQL 由 change_pct 复利重建: EXP(SUM(LN(1+change_pct/100)))-1,
  区间取 (D, end] —— 与回测"决策日收盘买入"一致。

为什么不用现成视图
  data/h5i/views/v_factor_scores_daily.parquet 只覆盖 2026-05-11 ~ 2026-09-07(85 个交易日),
  无法支撑 2018-2026 的历史窗口; v_factor_ic_latest.parquet 只有最近 20 日 IC。

用法: python scripts/factor_ic_forward.py
输出: data/factor_ic_forward.json
"""
from __future__ import annotations

import json
import os
import sys
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import pandas as pd  # noqa: E402

FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")
OUT = os.path.join(_BASE, "data", "factor_ic_forward.json")
XSEC_DIR = os.path.join(_BASE, "data", "pit", "xsec")
N_BIG = 6000


def _sql(q: str):
    from factor_fusion import _sql as f
    return f(q)


def cross_section(day: str) -> pd.DataFrame:
    """决策日的全截面因子打分包 (canon, signal, ...), 带磁盘缓存."""
    os.makedirs(XSEC_DIR, exist_ok=True)
    fp = os.path.join(XSEC_DIR, f"{day}.parquet")
    if os.path.exists(fp):
        try:
            return pd.read_parquet(fp)
        except Exception:  # noqa: BLE001
            pass
    from db import StockDB
    from selector import RotationSelector
    sel = RotationSelector(StockDB(), n=N_BIG).select(hist_day=day)
    if not sel or sel.get("error"):
        return pd.DataFrame()
    rows = []
    for t in sel.get("top_n") or []:
        canon = str(t.get("canon") or "")
        # daily_bars.symbol 是 6 位纯数字, 而选股的 canon 带 .SH/.SZ 后缀 -> 统一成 6 位
        sym6 = canon.split(".")[0].zfill(6)
        rows.append({"canon": canon, "sym6": sym6, "signal": t.get("signal"),
                     "trend": t.get("trend"), "govern": t.get("govern"),
                     "vol": t.get("vol"), "mom_rev": t.get("mom_rev"),
                     "liquidity": t.get("liquidity")})
    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_parquet(fp, index=False)
    return df


def fwd_return(day: str, end: str) -> pd.DataFrame:
    """(day, end] 区间内各标的的复利收益(%), 单次 SQL 直接算完."""
    q = ("SELECT symbol AS canon, "
         "EXP(SUM(LN(1 + change_pct / 100.0))) - 1 AS ret "
         "FROM daily_bars "
         f"WHERE CAST(ts AS DATE) > DATE '{day}' AND CAST(ts AS DATE) <= DATE '{end}' "
         "AND change_pct IS NOT NULL "
         "GROUP BY symbol")
    df = _sql(q)
    if df is None or df.empty:
        return pd.DataFrame(columns=["canon", "ret"])
    df["canon"] = df["canon"].astype(str).str.zfill(6)
    df["ret"] = pd.to_numeric(df["ret"], errors="coerce") * 100.0
    return df


def main() -> None:
    from vnpy_backtest import forward_window_days
    import datetime as dt

    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"前向窗口 {len(fw)} 个; 每窗口取全截面打分(n={N_BIG}) 算 RankIC\n")
    rows = []
    for x in fw:
        day = x["day"]
        end = x["stats"].get("end_date")
        t0 = time.time()
        xsec = cross_section(day)
        if xsec.empty:
            print(f"  {day}: 截面为空, 跳过")
            continue
        fr = fwd_return(day, end)
        if "sym6" not in xsec.columns:      # 兼容修复前生成的缓存
            xsec = xsec.assign(
                sym6=xsec["canon"].astype(str).str.split(".").str[0].str.zfill(6))
        m = xsec.merge(fr, left_on="sym6", right_on="canon", how="inner",
                       suffixes=("", "_r"))
        m = m[pd.to_numeric(m["signal"], errors="coerce").notna()]
        m = m[pd.to_numeric(m["ret"], errors="coerce").notna()]
        ic = None
        if len(m) >= 50:
            ic = float(m["signal"].astype(float).corr(m["ret"].astype(float), method="spearman"))
        rows.append({"day": day, "end": end, "n_xsec": int(len(xsec)),
                     "n_join": int(len(m)), "ic_signal": ic,
                     "xsec_mean_signal": float(pd.to_numeric(xsec["signal"], errors="coerce").mean())
                     if "signal" in xsec else None})
        print(f"  {day}  截面 {len(xsec):>5}  可对齐 {len(m):>5}  "
              f"RankIC={('n/a' if ic is None else f'{ic:+.4f}')}  ({time.time() - t0:.0f}s)",
              flush=True)

    ics = [r["ic_signal"] for r in rows if r["ic_signal"] is not None]
    print(f"\n=== 融合因子(signal) 前向 RankIC ===")
    if ics:
        import statistics as st
        print(f"  n={len(ics)}  均值 {st.mean(ics):+.4f}  中位 {st.median(ics):+.4f}  "
              f"min {min(ics):+.4f}  max {max(ics):+.4f}  正比例 "
              f"{sum(1 for v in ics if v > 0)}/{len(ics)}")
        print(f"  [判定] 均值 > 0.02 为有预测力 -> "
              f"{'有' if st.mean(ics) > 0.02 else '**无**'}预测力")
    else:
        print("  无可计算的 IC")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()
