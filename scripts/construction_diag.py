# -*- coding: utf-8 -*-
"""第三层归因 + 组合构建对比: 排序键 × 配权方式 的前向收益矩阵.

背景(第一/二层结论)
  融合分池内 IC +0.1105(10/11 正) 却打不出好组合: 池内最尖的 top10 反向(-5.89%),
  去掉融合分最高 5% 后的 top10 反而 +10.76%(中位 +12.46%, 8/11 正)。
  另核实: 生产排序键 score = (1-w)*旧复合 + w*pct_rank(融合 z), w=FML_WEIGHT(默认 0.10);
  配权 allocate_target_weights(mode='fml', min_mult 0.60 / max_mult 1.21) 用的是
  fml(=融合 z) 的 rank 线性收缩 -> **配权层已在用融合分**。

本脚本对比(每个窗口, 前向 120 交易日)
  排序键 K ∈ {旧复合 base / 生产 score(0.9+0.1) / 纯融合 z (即 RANK_BY_FUSION=1) /
             融合截尾 top10 (剔除融合 z 最高 5%) / 融合次优档 rank11~20}
  配权  W ∈ {等权 / 生产 allocate_target_weights(fml=融合 z)}
  -> 组合收益 = Σ w_i * ret_i  (窗口内买入持有, 与 vnpy 静态持有口径一致)

输出: data/construction_diag.json
用法: python scripts/construction_diag.py   (需带 h5i_db 的解释器, 见 README)
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
OUT = os.path.join(_BASE, "data", "construction_diag.json")
TOPK = 10
TRIM_Q = 0.95
FML_W = 0.10          # 生产 FML_WEIGHT 默认值 (ml_fusion_bridge)

KEYS = ["old_base", "prod_score", "pure_fusion", "fusion_trim5", "fusion_rank11_20",
        "prod_trim"]


def _pct_rank(a: pd.Series) -> pd.Series:
    return a.rank(pct=True)


def main() -> None:
    import factor_ic_forward as fif
    from factor_fusion import cross_section_scores
    from factor_library import selector_weights
    from pool_ic_diag import _old_score
    from target_weighting import allocate_target_weights

    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"第三层(配权) + 构建对比: {len(fw)} 窗口 × {len(KEYS)} 排序键 × 2 配权方式\n")

    rows = []
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

        m["fz_pct"] = _pct_rank(m["fz"])
        m["prod_score"] = (1.0 - FML_W) * m["old_base"] + FML_W * m["fz_pct"]
        m["pure_fusion"] = m["fz"]

        cut = m["fz"].quantile(TRIM_Q)
        m_trim = m[m["fz"] <= cut]
        m_top20 = m.nlargest(2 * TOPK, "fz")
        # 组合方案: 生产式 10% 掺入 + 融合分截尾 (极端头部降为最低分位, 不参与头部)
        m["fz_trim_pct"] = m["fz"].where(m["fz"] <= cut).rank(pct=True).fillna(0.0)
        m["prod_trim"] = (1.0 - FML_W) * m["old_base"] + FML_W * m["fz_trim_pct"]

        picks = {
            "old_base": m.nlargest(TOPK, "old_base"),
            "prod_score": m.nlargest(TOPK, "prod_score"),
            "pure_fusion": m.nlargest(TOPK, "pure_fusion"),
            "fusion_trim5": m_trim.nlargest(TOPK, "fz"),
            "fusion_rank11_20": m_top20.tail(TOPK),
            "prod_trim": m.nlargest(TOPK, "prod_trim"),
        }

        rec = {"day": day, "n": int(len(m))}
        for k, sel in picks.items():
            rets = sel["ret"].to_numpy(dtype=float)
            eq = float(np.mean(rets))
            # 生产配权: fml 字段 = 融合 z, 走 allocate_target_weights(mode='fml')
            items = [{"canon": c, "score": float(s), "fml": float(f)}
                     for c, s, f in zip(sel["sym6"], sel["prod_score"], sel["fz"])]
            allocate_target_weights(items)
            w = np.array([float(it.get("target_weight") or 0.0) for it in items])
            wt = float(np.sum(w * rets)) if abs(w.sum() - 1.0) < 1e-6 else float("nan")
            rec[f"{k}|equal"] = eq
            rec[f"{k}|weighted"] = wt
            rec[f"{k}|wmax_ratio"] = float(w.max() / w.min()) if w.min() > 0 else float("nan")
        rows.append(rec)
        print(f"  {day}  池 {len(m):>5}  " + "  ".join(
            f"{k}: {rec[k+'|equal']:>7.2f}/{rec[k+'|weighted']:>7.2f}" for k in KEYS)
            + f"  ({time.time()-t0:.0f}s)", flush=True)

    if not rows:
        print("\n无可用结果")
        return

    print("\n=== 组合构建矩阵: 前向收益均值% (等权 / 生产配权) ; 中位数; 正窗口 ===")
    head = f"{'排序键':<20}{'等权均值':>10}{'配权均值':>10}{'等权中位':>10}{'配权中位':>10}{'正窗口(等权/配权)':>20}"
    print(head)
    print("-" * len(head) + "----")
    for k in KEYS:
        e = [r[f"{k}|equal"] for r in rows]
        w = [r[f"{k}|weighted"] for r in rows if np.isfinite(r[f"{k}|weighted"])]
        print(f"{k:<20}{st.mean(e):>10.2f}{st.mean(w):>10.2f}"
              f"{st.median(e):>10.2f}{st.median(w):>10.2f}"
              f"{sum(1 for v in e if v > 0):>11}/{len(e)}"
              f"  {sum(1 for v in w if v > 0):>4}/{len(w)}")

    print(f"\n=== 配权层的边际影响 (生产配权 - 等权, pp) ===")
    for k in KEYS:
        d = [r[f"{k}|weighted"] - r[f"{k}|equal"] for r in rows
             if np.isfinite(r[f"{k}|weighted"])]
        print(f"  {k:<20} 均值 {st.mean(d):+6.2f}pp   中位 {st.median(d):+6.2f}pp   "
              f"改善 {sum(1 for v in d if v > 0)}/{len(d)}")
    wr = [r[f"{KEYS[0]}|wmax_ratio"] for r in rows if np.isfinite(r[f"{KEYS[0]}|wmax_ratio"])]
    if wr:
        print(f"\n  生产配权的最大/最小权重比 均值 {st.mean(wr):.3f} (越接近 1 越接近等权)")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("\n已保存:", OUT)


if __name__ == "__main__":
    main()
