# -*- coding: utf-8 -*-
"""item2/3: 融合因子拆解 (pb_inv/ep/ocf_ps/roe_yy_chg) + 相关性 + 符号方案测试.

问题: 融合信号 IC 全视界为负(-0.067 ~ -0.095)。到底是 ①四个成分各自都无效,
还是 ②成分有效但融合方向/权重把它做反了?

做法: 复用生产链路 factor_fusion._Snap/_assemble_snapshot 取 11 个决策日的
**原始因子截面**(pb_inv/ep/ocf_ps/roe_yy_chg), 与同一批前向收益算 RankIC;
再构造三种符号方案对比:
  A. 生产方向 (默认 DIRECTIONS, 与 factor_fusion.FACTOR_WEIGHTS 一致)
  B. 全部反向
  C. 按各因子**实测**平均 IC 的符号对齐(ICIR 思想)
注: 此处未做 size/行业中性化(生产链路会做), 故绝对值与生产 IC 会有差异,
    但**用于判断符号方向是否搞反**是充分的。

用法: python scripts/fusion_decompose.py
输出: data/fusion_decompose.json
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import factor_fusion as ff  # noqa: E402
from vnpy_backtest import forward_window_days  # noqa: E402

FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")
OUT = os.path.join(_BASE, "data", "fusion_decompose.json")
XDIR = os.path.join(_BASE, "data", "pit", "fusion_x")
HORIZONS = [5, 10, 20, 60, 120]
FACTORS = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg"]


def ret_at(day: str, end: str) -> pd.DataFrame:
    q = ("SELECT symbol AS s6, EXP(SUM(LN(1 + change_pct / 100.0))) - 1 AS r "
         "FROM daily_bars "
         f"WHERE CAST(ts AS DATE) > DATE '{day}' AND CAST(ts AS DATE) <= DATE '{end}' "
         "AND change_pct IS NOT NULL GROUP BY symbol")
    df = ff._sql(q)
    if df is None or df.empty:
        return pd.DataFrame(columns=["s6", "r"])
    df["s6"] = df["s6"].astype(str).str.zfill(6)
    df["r"] = pd.to_numeric(df["r"], errors="coerce")
    return df


def snapshot(day: str) -> pd.DataFrame:
    """决策日的原始因子截面(带缓存)."""
    os.makedirs(XDIR, exist_ok=True)
    fp = os.path.join(XDIR, f"{day}.parquet")
    if os.path.exists(fp):
        try:
            return pd.read_parquet(fp)
        except Exception:  # noqa: BLE001
            pass
    bars = ff._sql("SELECT CAST(ts AS DATE) d, symbol, close, change_pct "
                   "FROM daily_bars "
                   f"WHERE CAST(ts AS DATE) = DATE '{day}'")
    if bars is None or bars.empty:
        return pd.DataFrame()
    bars = bars[bars["symbol"].astype(str).str.fullmatch(r"\d{6}")].copy()
    bars["symbol"] = bars["symbol"].astype(str).str.zfill(6)
    bars["d"] = pd.to_datetime(bars["d"])
    active = ff._active_szsh()
    fin = ff._fin_frame()
    val = ff._val_frame(pd.Timestamp(day) - pd.Timedelta(days=730))
    snap = ff._Snap(fin, val)
    snap.advance(pd.Timestamp(day))
    df = ff._assemble_snapshot(snap, bars, active)
    if df is not None and not df.empty and "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        df.to_parquet(fp, index=False)
    return df if df is not None else pd.DataFrame()


def zscore(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    lo, hi = x.quantile(0.01), x.quantile(0.99)
    x = x.clip(lo, hi)
    sd = x.std()
    return (x - x.mean()) / sd if sd and np.isfinite(sd) and sd > 1e-12 else x * 0.0


def main() -> None:
    import datetime as dt
    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"融合拆解: {len(fw)} 个决策日, 因子 {FACTORS}\n")

    ic_f: dict[str, dict[int, list]] = {f: {h: [] for h in HORIZONS} for f in FACTORS}
    fuse: dict[str, dict[int, list]] = {k: {h: [] for h in HORIZONS} for k in ("A", "B", "C")}
    corr_rows, sign_rows = [], []
    avail = [f for f in FACTORS]

    for x in fw:
        day = x["day"]
        df = snapshot(day)
        if df.empty:
            print(f"  {day}: 截面为空, 跳过")
            continue
        d0 = dt.date.fromisoformat(day)
        # 因子间 Spearman 相关(取共同非空样本)
        sub = df[[c for c in avail if c in df.columns]].apply(pd.to_numeric, errors="coerce")
        sub = sub.dropna(how="all")
        if len(sub) >= 50:
            corr_rows.append(sub.corr(method="spearman"))
        Z = {f: zscore(df[f]) for f in avail if f in df.columns}
        # 实测平均 IC 符号(用于方案 C)
        per_h_ic = {}
        for h in HORIZONS:
            seg = forward_window_days(d0, h + 1)
            if len(seg) < h + 1:
                continue
            fr = ret_at(day, seg[-1])
            m = pd.DataFrame({"symbol": df["symbol"]}).join(fr.set_index("s6"), on="symbol")
            m = m[m["r"].notna()]
            if len(m) < 50:
                continue
            idx = m.index
            for f in avail:
                if f not in Z:
                    continue
                v = pd.Series(Z[f].reindex(idx).to_numpy(), index=idx)
                ok = v.notna()
                if ok.sum() < 50:
                    continue
                ic = float(v[ok].corr(m.loc[ok, "r"].astype(float), method="spearman"))
                ic_f[f][h].append(ic)
                per_h_ic.setdefault(f, []).append(ic)
        # 三种符号方案
        for h in HORIZONS:
            seg = forward_window_days(d0, h + 1)
            if len(seg) < h + 1:
                continue
            fr = ret_at(day, seg[-1])
            m = pd.DataFrame({"symbol": df["symbol"]}).join(fr.set_index("s6"), on="symbol")
            m = m[m["r"].notna()]
            if len(m) < 50:
                continue
            idx = m.index
            r = m["r"].astype(float)
            for tag, dirs in (("A", ff.DIRECTIONS),
                              ("B", {f: -v for f, v in ff.DIRECTIONS.items()}),
                              ("C", None)):
                sc = np.zeros(len(idx))
                for f in avail:
                    if f not in Z:
                        continue
                    v = Z[f].reindex(idx).to_numpy()
                    v = np.nan_to_num(v, nan=0.0)
                    if tag == "C":
                        arr = per_h_ic.get(f) or [0.0]
                        sgn = 1.0 if st.mean(arr) >= 0 else -1.0
                    else:
                        sgn = float(dirs.get(f, 1))
                    sc += sgn * float(ff.FACTOR_WEIGHTS.get(f, 1.0)) * v
                sc = pd.Series(sc, index=idx)
                if sc.std() > 1e-12:
                    fuse[tag][h].append(float(sc.corr(r, method="spearman")))

    print("=== ① 各成分 RankIC (11 决策日 × 视界) ===")
    print(f"{'因子':<12}" + "".join(f"{str(h) + 'd':>9}" for h in HORIZONS) + f"{'均值':>9}{'正比例':>9}")
    for f in FACTORS:
        row, allv = "", []
        for h in HORIZONS:
            v = ic_f[f][h]
            allv += v
            row += f"{st.mean(v):>9.4f}" if v else f"{'n/a':>9}"
        pos = sum(1 for t in allv if t > 0)
        print(f"{f:<12}{row}{(st.mean(allv) if allv else float('nan')):>9.4f}"
              f"{f'{pos}/{len(allv)}':>9}")

    print("\n=== ② 因子间 Spearman 相关 (各窗口均值) ===")
    corr_mean_df = None
    if corr_rows:
        corr_mean_df = (pd.concat(corr_rows).groupby(level=0).mean().round(3))
        print(corr_mean_df.to_string())

    print("\n=== ③ 符号方案对比 (融合信号 RankIC) ===")
    print(f"{'方案':<28}" + "".join(f"{str(h) + 'd':>9}" for h in HORIZONS))
    names = {"A": "A 生产方向", "B": "B 全部反向",
             "C": "C 按实测IC对齐(ICIR)"}
    for tag in ("A", "B", "C"):
        row, allv = "", []
        for h in HORIZONS:
            v = fuse[tag][h]
            allv += v
            row += f"{st.mean(v):>9.4f}" if v else f"{'n/a':>9}"
        print(f"{names[tag]:<28}{row}   总均值 {st.mean(allv):+.4f}" if allv else f"{names[tag]:<28}{row}")

    res = {"ic_by_factor": {f: {str(h): ic_f[f][h] for h in HORIZONS} for f in FACTORS},
           "corr_mean": (corr_mean_df.to_dict() if corr_mean_df is not None else {}),
           "fusion_by_scheme": {t: {str(h): fuse[t][h] for h in HORIZONS} for t in fuse},
           "factor_weights": ff.FACTOR_WEIGHTS, "directions": ff.DIRECTIONS}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2, default=str)
    print("\n已保存:", OUT)


if __name__ == "__main__":
    main()
