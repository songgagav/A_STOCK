# -*- coding: utf-8 -*-
"""第二层归因: 选股逻辑 —— 为什么"融合分 IC 更高、但融合 top10 更差".

第一层(pool_ic_diag.py)结果(11 窗口前向 120 交易日):
  池内 IC(融合分) 均值 +0.1105 (10/11 正), 全截面 +0.1155
  池内 IC(旧 score) -0.0358, 旧 signal -0.0662
  但 融合 top10 前向收益 -5.89% vs 旧 top10 +1.64% (差 -7.53pp, 融合更优 3/11, 重叠 0.1/10)
=> 秩相关为正、头部却更差, 典型的**尾部非单调**。本脚本定位其机制:

  ① 十分档收益: 池内按融合 z 切 10 档, 看各档前向收益单调性(头部是否反转)
  ② 头部画像: 融合 top10 与旧 top10 在各因子上的**池内分位**(看是否聚在极端价值/小市值)
  ③ 截尾试验: 去掉融合分最高 5% 后再取 top10, 与原始 top10 对比(验证"极端头部是毒")

输出: data/selection_tail_diag.json
用法: python scripts/selection_tail_diag.py   (需带 h5i_db 的解释器, 见 README)
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
sys.path.insert(0, os.path.join(_BASE, "scripts"))
os.chdir(_BASE)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")
XSEC_DIR = os.path.join(_BASE, "data", "pit", "xsec")
FUSION_X = os.path.join(_BASE, "data", "pit", "fusion_x")
OUT = os.path.join(_BASE, "data", "selection_tail_diag.json")
TOPK = 10
N_DECILE = 10
TRIM_Q = 0.95          # 截尾: 去掉融合分最高 5%
FACTORS = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg"]


def main() -> None:
    import factor_ic_forward as fif
    from factor_fusion import cross_section_scores, _residualize
    from factor_library import selector_weights
    from pool_ic_diag import _old_score

    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"第二层(选股逻辑): {len(fw)} 个窗口, 前向 120 交易日, 池内按融合 z 切 {N_DECILE} 档\n")

    decile_rows = []
    prof_f, prof_o = [], []
    top10_raw, top10_trim, top20 = [], [], []
    detail = []

    for x in fw:
        day, end = x["day"], x["stats"].get("end_date")
        t0 = time.time()
        xsec = pd.read_parquet(os.path.join(XSEC_DIR, f"{day}.parquet")).copy()
        if "sym6" not in xsec.columns:
            xsec = xsec.assign(sym6=xsec["canon"].astype(str).str.split(".").str[0].str.zfill(6))
        xsec["sym6"] = xsec["sym6"].astype(str).str.zfill(6)
        xsec["old_score"] = _old_score(xsec, selector_weights(as_of=day))

        zmap = (cross_section_scores(day) or {}).get("scores") or {}
        if not zmap:
            print(f"  {day}: 融合分不可得, 跳过")
            continue
        xsec["fz"] = xsec["sym6"].map(zmap)
        fr = fif.fwd_return(day, end).rename(columns={"canon": "sym6"})[["sym6", "ret"]]
        m = xsec.dropna(subset=["fz"]).merge(fr, on="sym6", how="inner")
        m = m[pd.to_numeric(m["ret"], errors="coerce").notna()]
        if len(m) < 100:
            print(f"  {day}: 可对齐仅 {len(m)}, 跳过")
            continue

        # ① 十分档
        m = m.assign(dec=pd.qcut(m["fz"], N_DECILE, labels=False, duplicates="drop"))
        dec = m.groupby("dec")["ret"].mean()
        decile_rows.append({int(k): float(v) for k, v in dec.items()})

        # ② 头部因子画像(池内分位)
        fx = pd.read_parquet(os.path.join(FUSION_X, f"{day}.parquet"))
        fx["sym6"] = fx["symbol"].astype(str).str.zfill(6)
        for f in FACTORS:
            if f not in fx.columns:
                fx[f] = np.nan
        fz = {}
        for f in FACTORS:
            zm, _ = _residualize(fx, f)
            fz[f] = zm
        prof = pd.DataFrame({"sym6": fx["sym6"]})
        for f in FACTORS:
            v = pd.Series(fz[f])
            prof[f + "_pct"] = prof["sym6"].map(v).rank(pct=True)
        prof["lnsize_pct"] = pd.to_numeric(fx["ln_size"], errors="coerce").rank(pct=True).to_numpy()
        prof = prof.dropna(subset=[FACTORS[0] + "_pct"])

        t_raw = m.nlargest(TOPK, "fz")
        t_old = m.nlargest(TOPK, "old_score")
        # ③ 截尾: 去掉融合分最高 5% 后取 top10
        cut = m["fz"].quantile(TRIM_Q)
        t_trim = m[m["fz"] <= cut].nlargest(TOPK, "fz")
        # 次优档: 融合分排名 11~20
        t_20 = m.nlargest(2 * TOPK, "fz").tail(TOPK)

        top10_raw.append(float(t_raw["ret"].mean()))
        top10_trim.append(float(t_trim["ret"].mean()))
        top20.append(float(t_20["ret"].mean()))

        def _prof(syms):
            p = prof[prof["sym6"].isin(set(syms))]
            return {c: float(p[c].mean()) for c in p.columns if c.endswith("_pct")} if len(p) else {}

        prof_f.append(_prof(t_raw["sym6"]))
        prof_o.append(_prof(t_old["sym6"]))
        detail.append({"day": day, "n": int(len(m)),
                       "ret_top10_fusion": float(t_raw["ret"].mean()),
                       "ret_top10_old": float(t_old["ret"].mean()),
                       "ret_top10_trimmed": float(t_trim["ret"].mean()),
                       "ret_rank11_20": float(t_20["ret"].mean()),
                       "decile_ret": {int(k): float(v) for k, v in dec.items()}})
        print(f"  {day}  池 {len(m):>5}  融合top10 {t_raw['ret'].mean():>7.2f}%  "
              f"旧top10 {t_old['ret'].mean():>7.2f}%  截尾后 {t_trim['ret'].mean():>7.2f}%  "
              f"11~20档 {t_20['ret'].mean():>7.2f}%  ({time.time()-t0:.0f}s)", flush=True)

    if not detail:
        print("\n无可用结果")
        return

    print(f"\n=== ① 十分档前向收益 (池内按融合 z 排序, D1=最低分, D10=最高分) ===")
    print(f"{'档位':<6}{'均值%':>9}{'中位%':>9}{'正比例':>9}")
    for d in range(N_DECILE):
        v = [r[d] for r in decile_rows if d in r]
        if not v:
            continue
        print(f"D{d+1:<5}{st.mean(v):>9.2f}{st.median(v):>9.2f}"
              f"{sum(1 for x in v if x > 0):>6}/{len(v)}")

    def _m(key):
        return st.mean([r[key] for r in detail]), st.median([r[key] for r in detail])

    print(f"\n=== ② 头部组合前向收益对比(等权, 均值/中位) ===")
    for key, label in (("ret_top10_fusion", f"融合 top{TOPK} (原始)"),
                       ("ret_rank11_20", f"融合 rank 11~{2*TOPK} (次优档)"),
                       ("ret_top10_trimmed", f"去掉最高 {int((1-TRIM_Q)*100)}% 后的 top{TOPK}"),
                       ("ret_top10_old", f"旧 score top{TOPK}")):
        a, b = _m(key)
        print(f"  {label:<28} {a:>+8.2f}%  /  {b:>+8.2f}%")

    print(f"\n=== ③ 头部因子画像 (池内分位均值, 1.0=池内最高) ===")
    cols = [c for c in (prof_f[0].keys() if prof_f else [])]
    if cols:
        print(f"{'组合':<26}" + "".join(f"{c.replace('_pct',''):>13}" for c in cols))
        for label, pr in (("融合 top10", prof_f), ("旧 score top10", prof_o)):
            print(f"{label:<26}" + "".join(
                f"{st.mean([p.get(c, float('nan')) for p in pr]):>13.3f}" for c in cols))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"detail": detail, "decile_rows": decile_rows,
                   "profile_fusion_top10": prof_f, "profile_old_top10": prof_o},
                  f, ensure_ascii=False, indent=2)
    print("\n已保存:", OUT)


if __name__ == "__main__":
    main()
