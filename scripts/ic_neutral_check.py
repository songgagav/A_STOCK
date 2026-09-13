# -*- coding: utf-8 -*-
"""中性化 IC 复核: 用生产链路的口径 (Winsorize -> OLS(ln_size+行业) -> z) 重算
pb_inv / ep / ocf_ps / roe_yy_chg 的 IC, 决定是否改 DIRECTIONS 并切换排序.

与 fusion_decompose.py 的区别: 那里用**原始值**算 IC, 这里用**中性化后 z**,
与生产 `_residualize` 完全一致 —— 这是改方向前必须看的口径。

判定:
  roe_yy_chg 中性化 IC 均值 > 0  -> 生产方向 -1 是错的, 应改 +1
  roe_yy_chg 中性化 IC 均值 < 0  -> 生产方向正确, 不可改

用法: python scripts/ic_neutral_check.py
输出: data/ic_neutral_check.json
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
XDIR = os.path.join(_BASE, "data", "pit", "fusion_x")
OUT = os.path.join(_BASE, "data", "ic_neutral_check.json")
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


def main() -> None:
    import datetime as dt
    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"中性化 IC 复核: {len(fw)} 个决策日 (Winsorize -> OLS(ln_size+行业) -> z)\n")

    ic_n: dict[str, dict[int, list]] = {f: {h: [] for h in HORIZONS} for f in FACTORS}
    fuse: dict[str, dict[int, list]] = {k: {h: [] for h in HORIZONS} for k in ("A", "C")}
    rsize, meta_rows = [], []
    for x in fw:
        day = x["day"]
        fp = os.path.join(XDIR, f"{day}.parquet")
        if not os.path.exists(fp):
            print(f"  {day}: 无截面缓存, 跳过")
            continue
        df = pd.read_parquet(fp)
        if "ln_size" not in df.columns or "industry" not in df.columns:
            print(f"  {day}: 缺 ln_size/industry, 跳过")
            continue
        d0 = dt.date.fromisoformat(day)
        Z, metas = {}, {}
        for f in FACTORS:
            z, meta = ff._residualize(df, f)
            Z[f] = pd.Series(z, dtype=float)
            metas[f] = meta
        meta_rows.append({"day": day, **{f: metas[f].get("r_size") for f in FACTORS}})
        rsize.append({f: metas[f].get("r_size") for f in FACTORS})
        per_h: dict[str, list] = {f: [] for f in FACTORS}
        for h in HORIZONS:
            seg = forward_window_days(d0, h + 1)
            if len(seg) < h + 1:
                continue
            fr = ret_at(day, seg[-1])
            m = pd.DataFrame({"symbol": df["symbol"].astype(str).str.zfill(6)})
            m = m.join(fr.set_index("s6"), on="symbol")
            m = m[m["r"].notna()]
            if len(m) < 50:
                continue
            idx = m.index
            r = m["r"].astype(float)
            for f in FACTORS:
                v = Z[f].reindex(df["symbol"].astype(str).str.zfill(6).values)
                v = pd.Series(v.to_numpy(), index=df.index).reindex(idx)
                ok = v.notna()
                if ok.sum() < 50:
                    continue
                ic = float(v[ok].corr(r[ok], method="spearman"))
                ic_n[f][h].append(ic)
                per_h[f].append(ic)
            for tag in ("A", "C"):
                sc = np.zeros(len(idx))
                for f in FACTORS:
                    v = pd.Series(Z[f].reindex(df["symbol"].astype(str).str.zfill(6).values)
                                  .to_numpy(), index=df.index).reindex(idx)
                    v = np.nan_to_num(v.to_numpy(dtype=float), nan=0.0)
                    if tag == "C":
                        sgn = 1.0 if (per_h[f] and st.mean(per_h[f]) >= 0) else -1.0
                    else:
                        sgn = float(ff.DIRECTIONS.get(f, 1))
                    sc += sgn * float(ff.FACTOR_WEIGHTS.get(f, 1.0)) * v
                sc = pd.Series(sc, index=idx)
                if sc.std() > 1e-12:
                    fuse[tag][h].append(float(sc.corr(r, method="spearman")))
        print(f"  {day} 完成 (n_pool={metas['pb_inv'].get('n')})", flush=True)

    print("\n=== ① 中性化后 RankIC ===")
    print(f"{'因子':<12}" + "".join(f"{str(h) + 'd':>9}" for h in HORIZONS)
          + f"{'均值':>9}{'正比例':>9}{'生产方向':>9}")
    summ = {}
    for f in FACTORS:
        row, allv = "", []
        for h in HORIZONS:
            v = ic_n[f][h]
            allv += v
            row += f"{st.mean(v):>9.4f}" if v else f"{'n/a':>9}"
        mean = st.mean(allv) if allv else float("nan")
        pos = sum(1 for t in allv if t > 0)
        summ[f] = {"mean": mean, "pos": pos, "n": len(allv)}
        print(f"{f:<12}{row}{mean:>9.4f}{f'{pos}/{len(allv)}':>9}"
              f"{ff.DIRECTIONS.get(f, 1):>9}")

    print("\n=== ② 与中性化的正交性快检 (r_size 均值) ===")
    for f in FACTORS:
        v = [r[f] for r in rsize if r.get(f) is not None]
        print(f"  {f:<12} r_size 均值 {st.mean(v):+.4f}" if v else f"  {f}: n/a")

    print("\n=== ③ 符号方案 (中性化后) ===")
    print(f"{'方案':<24}" + "".join(f"{str(h) + 'd':>9}" for h in HORIZONS) + f"{'总均值':>10}")
    for tag, nm in (("A", "A 生产方向"), ("C", "C 按实测IC对齐")):
        row, allv = "", []
        for h in HORIZONS:
            v = fuse[tag][h]
            allv += v
            row += f"{st.mean(v):>9.4f}" if v else f"{'n/a':>9}"
        print(f"{nm:<24}{row}{st.mean(allv):>10.4f}" if allv else f"{nm:<24}{row}")

    print("\n=== 判定 ===")
    r = summ["roe_yy_chg"]["mean"]
    prod = ff.DIRECTIONS.get("roe_yy_chg", 1)
    if r > 0 and prod < 0:
        print(f"  roe_yy_chg 中性化 IC 均值 {r:+.4f} > 0, 但生产方向 {prod} -> "
              f"**方向错误, 应改 +1**")
    elif r < 0 and prod > 0:
        print(f"  roe_yy_chg 中性化 IC 均值 {r:+.4f} < 0, 但生产方向 {prod} -> 应改 -1")
    else:
        print(f"  roe_yy_chg 中性化 IC 均值 {r:+.4f}, 生产方向 {prod} -> **方向一致, 不可改**")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"summary": summ,
                   "ic_by_factor": {f: {str(h): ic_n[f][h] for h in HORIZONS} for f in FACTORS},
                   "fusion": {t: {str(h): fuse[t][h] for h in HORIZONS} for t in fuse},
                   "r_size": rsize,
                   "directions": ff.DIRECTIONS, "weights": ff.FACTOR_WEIGHTS},
                  fh, ensure_ascii=False, indent=2, default=str)
    print("\n已保存:", OUT)


if __name__ == "__main__":
    main()
