# -*- coding: utf-8 -*-
"""极端头部反转专项诊断 + 截尾比例敏感性 (3/5/8/10/15%).

问题: 融合分在池内 IC 为正(+0.1105) 且全截面为正, 但其**最尖的 top10**(前 ~0.5%)
前向收益反而是负的; 去掉融合分最高 5% 后 top10 由 -5.89% 变 +10.76%。本脚本回答:
  ① 截尾比例敏感性: 3/5/8/10/15% 是否都有效(还是只有 5% 这个点有效)
  ② 时间稳定性: 11 窗口分拆(前半/后半 + 逐年)看是否只在个别窗口有效
  ③ 头部画像: 市值/波动/流动性/反转 + 行业集中度
  ④ 被丢弃的极端头部 vs 保留头部 vs 旧 score 头部 的因子画像对比

输出: data/trim_sensitivity.json
用法: python scripts/trim_sensitivity.py   (需带 h5i_db 的解释器, 见 README)
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
import time
from collections import Counter

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
OUT = os.path.join(_BASE, "data", "trim_sensitivity.json")
TOPK = 10
FML_W = 0.10
Q_LIST = [0.00, 0.03, 0.05, 0.08, 0.10, 0.15]      # 0.00 = 不截尾(对照)
FACTORS = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg"]


def main() -> None:
    import factor_ic_forward as fif
    from factor_fusion import cross_section_scores, _residualize
    from factor_library import selector_weights
    from pool_ic_diag import _old_score

    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"截尾敏感性: {len(fw)} 窗口 × {len(Q_LIST)} 个截尾比例; 池内 top{TOPK}\n")

    per_q_pure, per_q_prod = {q: [] for q in Q_LIST}, {q: [] for q in Q_LIST}
    rows = []
    prof_keep, prof_drop, prof_old = [], [], []
    decile_hi = []          # 每个窗口"最高 1% 分位"收益, 用于看极端尾

    for x in fw:
        day, end = x["day"], x["stats"].get("end_date")
        t0 = time.time()
        xs = pd.read_parquet(os.path.join(XSEC_DIR, f"{day}.parquet")).copy()
        if "sym6" not in xs.columns:
            xs = xs.assign(sym6=xs["canon"].astype(str).str.split(".").str[0].str.zfill(6))
        xs["sym6"] = xs["sym6"].astype(str).str.zfill(6)
        xs["old_base"] = _old_score(xs, selector_weights(as_of=day))

        zmap = (cross_section_scores(day) or {}).get("scores") or {}
        if not zmap:
            print(f"  {day}: 融合分不可得, 跳过")
            continue
        xs["fz"] = xs["sym6"].map(zmap)
        fr = fif.fwd_return(day, end).rename(columns={"canon": "sym6"})[["sym6", "ret"]]
        m = xs.dropna(subset=["fz"]).merge(fr, on="sym6", how="inner")
        m = m[pd.to_numeric(m["ret"], errors="coerce").notna()]
        if len(m) < 100:
            print(f"  {day}: 可对齐仅 {len(m)}, 跳过")
            continue

        # 最高 1% 分位 vs 其余 (看极端尾有多毒)
        hi_cut = m["fz"].quantile(0.99)
        decile_hi.append({"day": day,
                          "top1pct": float(m[m["fz"] > hi_cut]["ret"].mean()),
                          "rest": float(m[m["fz"] <= hi_cut]["ret"].mean()),
                          "n_top1pct": int((m["fz"] > hi_cut).sum())})

        rec = {"day": day, "n": int(len(m))}
        for q in Q_LIST:
            if q == 0:
                sub = m
            else:
                cut = m["fz"].quantile(1.0 - q)
                sub = m[m["fz"] <= cut]
            sel = sub.nlargest(TOPK, "fz")
            r = float(sel["ret"].mean())
            per_q_pure[q].append(r)
            rec[f"pure_q{q}"] = r
            # 生产式掺入(以截尾后的分位参与 10% 混合)
            mm = m.copy()
            mm["fz_t"] = mm["fz"].where(mm["fz"] <= m["fz"].quantile(1.0 - q)) if q else mm["fz"]
            mm["fzp"] = mm["fz_t"].rank(pct=True).fillna(0.0)
            mm["prod"] = (1.0 - FML_W) * mm["old_base"] + FML_W * mm["fzp"]
            selp = mm.nlargest(TOPK, "prod")
            rp = float(selp["ret"].mean())
            per_q_prod[q].append(rp)
            rec[f"prod_q{q}"] = rp
        rec["old_top10"] = float(m.nlargest(TOPK, "old_base")["ret"].mean())
        rows.append(rec)

        # ---- 头部画像 (5% 截尾口径) ----
        fx = pd.read_parquet(os.path.join(FUSION_X, f"{day}.parquet"))
        fx["sym6"] = fx["symbol"].astype(str).str.zfill(6)
        fzp = {}
        for f in FACTORS:
            zm, _ = _residualize(fx, f) if f in fx.columns else ({}, {})
            fzp[f] = pd.Series(zm)
        prof = pd.DataFrame({"sym6": fx["sym6"]})
        for f in FACTORS:
            prof[f] = prof["sym6"].map(fzp[f]).rank(pct=True)
        prof["lnsize"] = pd.to_numeric(fx["ln_size"], errors="coerce").rank(pct=True).to_numpy()
        prof = prof.merge(m[["sym6", "vol", "liquidity", "mom_rev"]], on="sym6", how="inner")
        for c in ("vol", "liquidity", "mom_rev"):
            prof[c] = pd.to_numeric(prof[c], errors="coerce").rank(pct=True)
        ind = fx.set_index("sym6")["industry"].to_dict()

        cut5 = m["fz"].quantile(0.95)
        keep = set(m[m["fz"] <= cut5].nlargest(TOPK, "fz")["sym6"])
        drop = set(m.nlargest(max(TOPK, int((m["fz"] > cut5).sum())), "fz")["sym6"]) - keep
        drop = set(m[m["fz"] > cut5]["sym6"])
        oldt = set(m.nlargest(TOPK, "old_base")["sym6"])

        def _avg(syms):
            p = prof[prof["sym6"].isin(syms)]
            return {c: float(p[c].mean()) for c in
                    ("pb_inv", "ep", "ocf_ps", "roe_yy_chg", "lnsize", "vol",
                     "liquidity", "mom_rev")} if len(p) else {}

        prof_keep.append(_avg(keep))
        prof_drop.append(_avg(drop))
        prof_old.append(_avg(oldt))
        rec["ind_keep"] = Counter(ind.get(s) for s in keep).most_common(5)
        rec["ind_drop"] = Counter(ind.get(s) for s in drop).most_common(5)
        rec["ind_old"] = Counter(ind.get(s) for s in oldt).most_common(5)
        print(f"  {day}  池 {len(m):>5}  " + " ".join(
            f"q{q:.2f}:{rec[f'pure_q{q}']:>7.2f}" for q in Q_LIST)
            + f"  old:{rec['old_top10']:>7.2f}  ({time.time()-t0:.0f}s)", flush=True)

    if not rows:
        print("\n无可用结果")
        return

    print(f"\n=== ① 截尾比例敏感性: 纯融合截尾 top10 前向收益 ===")
    print(f"{'截尾比例':<10}{'均值%':>9}{'中位%':>9}{'正窗口':>9}{'前半均值%':>11}{'后半均值%':>11}")
    half = len(rows) // 2
    print("-" * 62)
    for q in Q_LIST:
        v = per_q_pure[q]
        print(f"{('不截尾' if q == 0 else f'{q:.0%}'):<10}{st.mean(v):>9.2f}{st.median(v):>9.2f}"
              f"{sum(1 for x in v if x > 0):>6}/{len(v)}"
              f"{st.mean(v[:half]):>11.2f}{st.mean(v[half:]):>11.2f}")

    print(f"\n=== ② 生产式掺入(0.9 旧 + 0.1 融合截尾) 的敏感性 ===")
    print(f"{'截尾比例':<10}{'均值%':>9}{'中位%':>9}{'正窗口':>9}{'前半均值%':>11}{'后半均值%':>11}")
    print("-" * 62)
    for q in Q_LIST:
        v = per_q_prod[q]
        print(f"{('不截尾' if q == 0 else f'{q:.0%}'):<10}{st.mean(v):>9.2f}{st.median(v):>9.2f}"
              f"{sum(1 for x in v if x > 0):>6}/{len(v)}"
              f"{st.mean(v[:half]):>11.2f}{st.mean(v[half:]):>11.2f}")

    base = per_q_prod[0.00]
    print(f"\n  对照: 纯旧 score top10 均值 "
          f"{st.mean([r['old_top10'] for r in rows]):+.2f}%  "
          f"中位 {st.median([r['old_top10'] for r in rows]):+.2f}%")
    print(f"  生产现状(掺入但不截尾) 均值 {st.mean(base):+.2f}%")

    print(f"\n=== ③ 极端尾(最高 1% 分位) vs 其余 ===")
    print(f"  最高 1% 分位 均值 {st.mean([d['top1pct'] for d in decile_hi]):+.2f}%  "
          f"(中位 {st.median([d['top1pct'] for d in decile_hi]):+.2f}%, 平均 "
          f"{int(st.mean([d['n_top1pct'] for d in decile_hi]))} 只)")
    print(f"  其余标的     均值 {st.mean([d['rest'] for d in decile_hi]):+.2f}%  "
          f"(中位 {st.median([d['rest'] for d in decile_hi]):+.2f}%)")

    print(f"\n=== ④ 头部画像 (池内分位均值, 1.0=池内最高) ===")
    cols = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg", "lnsize", "vol", "liquidity", "mom_rev"]
    print(f"{'组合':<22}" + "".join(f"{c:>11}" for c in cols))
    for label, pr in (("保留头部(截5%后top10)", prof_keep),
                      ("被丢弃的极端头部", prof_drop),
                      ("旧 score top10", prof_old)):
        print(f"{label:<22}" + "".join(
            f"{st.mean([p.get(c, float('nan')) for p in pr]):>11.3f}" for c in cols))

    print(f"\n=== ⑤ 行业集中度 (各窗口 top1 行业) ===")
    for label, key in (("保留头部", "ind_keep"), ("被丢弃头部", "ind_drop"), ("旧头部", "ind_old")):
        c = Counter()
        for r in rows:
            if r.get(key):
                c[r[key][0][0]] += 1
        print(f"  {label:<12} 最常见: " + ", ".join(f"{k}({v})" for k, v in c.most_common(4)))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "decile_hi": decile_hi,
                   "profile_keep": prof_keep, "profile_drop": prof_drop,
                   "profile_old": prof_old}, f, ensure_ascii=False, indent=2)
    print("\n已保存:", OUT)


if __name__ == "__main__":
    main()
