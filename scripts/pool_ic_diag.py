# -*- coding: utf-8 -*-
"""第一层归因: 池内 IC 诊断 —— 融合分在**候选池内**是否仍有预测力.

问题
  四因子融合分(中性化后)在**全截面**的 RankIC 为 +0.1167, 但把选股排序切到融合后,
  11 窗口影子回测反而更差(均值 -4.22% vs 基线 -2.07%, 正窗口 3/11 vs 6/11)。
  首要怀疑: 候选池是**先由旧 score 复合(IC -0.0674)筛出的有偏子集**, 融合分在
  全截面上的正 IC 未必在池内成立。若池内 IC 仍为正, 则问题在选股/配权/调仓等
  下游环节(进入第二层); 若池内 IC 转负, 则应先修选股逻辑。

做法(每个决策日 D)
  pool   = data/pit/xsec/{D}.parquet  -> RotationSelector 打分后的**完整候选池**
           (top_n 截断前的 scored 列表; 含 signal/trend/govern/vol/mom_rev/liquidity)
  fusion = factor_fusion.cross_section_scores(D)["scores"]  (生产口径, 全票池做 z)
  ret    = (D, D+120 交易日] 的复利收益, 复用 factor_ic_forward.fwd_return 的同一条 SQL
  另外用池内字段按 selector._select_hist 的公式**完整重建旧 score**
  (权重取 selector_weights(as_of=D)), 以对比"池内按融合取 top10"与"按旧 score 取 top10"
  的前向收益差异 —— 这直接量化影子回测里组合劣化的来源。

输出: data/pool_ic_diag.json + 控制台表
用法: python scripts/pool_ic_diag.py
注意: 必须用**带 h5i_db 的解释器**运行(本机为 TraeWork 自带 python3.10),
      .venv314 不含 h5i_db(编译扩展为 cp310), 会取不到日线。
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
OUT = os.path.join(_BASE, "data", "pool_ic_diag.json")
MIN_JOIN = 50
TOPK = 10


def _spearman(a, b):
    if len(a) < MIN_JOIN:
        return None
    return float(pd.Series(np.asarray(a, dtype=float)).corr(
        pd.Series(np.asarray(b, dtype=float)), method="spearman"))


def _old_score(df: pd.DataFrame, W: dict) -> np.ndarray:
    """按 selector._select_hist 的公式重建旧 score (池内口径)."""
    def col(name):
        return pd.to_numeric(df[name], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    sig_part = np.clip(0.5 + 2.0 * col("signal"), 0.0, 1.0)
    trend_part = (col("trend") + 1.0) / 2.0
    return (W.get("signal", 0.0) * sig_part + W.get("trend", 0.0) * trend_part
            + W.get("govern", 0.0) * col("govern")
            + W.get("liquidity", 0.0) * col("liquidity")
            + W.get("vol", 0.0) * col("vol")
            + W.get("mom_rev", 0.0) * col("mom_rev"))


def main() -> None:
    import factor_ic_forward as fif                     # 复用同一条前向收益 SQL
    from factor_fusion import cross_section_scores
    from factor_library import selector_weights

    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"池内 IC 诊断: {len(fw)} 个决策日; 前向窗口 120 交易日; 池内 top{TOPK}\n")
    print(f"{'决策日':<12}{'池大小':>7}{'可对齐':>7}"
          f"{'池内IC融合':>11}{'池内IC旧sc':>11}{'池内ICsig':>10}{'全截面IC融合':>13}"
          f"{'融合top10':>10}{'旧top10':>9}{'重叠':>5}")
    print("-" * 112)

    rows = []
    for x in fw:
        day, end = x["day"], x["stats"].get("end_date")
        t0 = time.time()
        try:
            xsec = pd.read_parquet(os.path.join(XSEC_DIR, f"{day}.parquet"))
        except Exception as e:  # noqa: BLE001
            print(f"{day:<12} 截面缓存缺失({type(e).__name__}), 跳过")
            continue
        xsec = xsec.copy()
        if "sym6" not in xsec.columns:      # 兼容 canon 归一化修复前生成的缓存
            xsec = xsec.assign(
                sym6=xsec["canon"].astype(str).str.split(".").str[0].str.zfill(6))
        xsec["sym6"] = xsec["sym6"].astype(str).str.zfill(6)

        res = cross_section_scores(day) or {}
        zmap = res.get("scores") or {}
        if not zmap:
            print(f"{day:<12} 融合分不可得({res.get('meta')}), 跳过")
            continue

        W = selector_weights(as_of=day)
        xsec["old_score"] = _old_score(xsec, W)
        xsec["fz"] = xsec["sym6"].map(zmap)

        fr = fif.fwd_return(day, end)
        fr = fr.rename(columns={"canon": "sym6"})[["sym6", "ret"]]

        # 池内可对齐集合
        m = xsec.dropna(subset=["fz"]).merge(fr, on="sym6", how="inner")
        m = m[pd.to_numeric(m["ret"], errors="coerce").notna()]
        if len(m) < MIN_JOIN:
            print(f"{day:<12} 池内可对齐仅 {len(m)} (<{MIN_JOIN}), 跳过 (池大小 {len(xsec)})")
            continue

        ic_f = _spearman(m["fz"], m["ret"])
        ic_o = _spearman(m["old_score"], m["ret"])
        ic_s = _spearman(pd.to_numeric(m["signal"], errors="coerce").fillna(0.0), m["ret"])

        # 全截面对照 (融合分覆盖的全部标的, 不受池限制)
        univ = pd.DataFrame({"sym6": list(zmap.keys()),
                             "fz": list(zmap.values())}).merge(fr, on="sym6", how="inner")
        ic_u = _spearman(univ["fz"], univ["ret"])

        top_f = m.nlargest(TOPK, "fz")
        top_o = m.nlargest(TOPK, "old_score")
        ret_f, ret_o = float(top_f["ret"].mean()), float(top_o["ret"].mean())
        overlap = len(set(top_f["sym6"]) & set(top_o["sym6"]))

        rows.append({
            "day": day, "end": end, "n_pool": int(len(xsec)), "n_join": int(len(m)),
            "ic_fusion_pool": ic_f, "ic_oldscore_pool": ic_o, "ic_signal_pool": ic_s,
            "ic_fusion_universe": ic_u, "n_universe": int(len(univ)),
            "top10_fusion_ret": ret_f, "top10_old_ret": ret_o, "top10_overlap": overlap,
            "top10_fusion": list(top_f["sym6"]), "top10_old": list(top_o["sym6"]),
        })
        print(f"{day:<12}{len(xsec):>7}{len(m):>7}{ic_f:>11.4f}{ic_o:>11.4f}"
              f"{(ic_s if ic_s is not None else float('nan')):>10.4f}"
              f"{(ic_u if ic_u is not None else float('nan')):>13.4f}"
              f"{ret_f:>10.2f}{ret_o:>9.2f}{overlap:>5}   ({time.time()-t0:.0f}s)", flush=True)

    if not rows:
        print("\n无可用结果")
        return

    def agg(key):
        v = [r[key] for r in rows if r.get(key) is not None]
        return (st.mean(v), st.median(v), sum(1 for x in v if x > 0), len(v)) if v else (None,) * 4

    print("\n=== 汇总 ===")
    for key, label in (("ic_fusion_pool", "池内 IC(融合分)"),
                       ("ic_oldscore_pool", "池内 IC(旧 score)"),
                       ("ic_signal_pool", "池内 IC(旧 signal)"),
                       ("ic_fusion_universe", "全截面 IC(融合分)")):
        m_, md, pos, n = agg(key)
        if m_ is None:
            continue
        print(f"  {label:<20} 均值 {m_:+.4f}  中位 {md:+.4f}  正 {pos}/{n}")

    df = pd.DataFrame(rows)
    print(f"\n  融合 top{TOPK} 前向收益 均值 {df['top10_fusion_ret'].mean():+.2f}%  "
          f"中位 {df['top10_fusion_ret'].median():+.2f}%")
    print(f"  旧  top{TOPK} 前向收益 均值 {df['top10_old_ret'].mean():+.2f}%  "
          f"中位 {df['top10_old_ret'].median():+.2f}%")
    print(f"  两者差值 均值 {(df['top10_fusion_ret']-df['top10_old_ret']).mean():+.2f}pp  "
          f"融合更优 {int((df['top10_fusion_ret']>df['top10_old_ret']).sum())}/{len(df)} 个窗口")
    print(f"  top{TOPK} 重叠 均值 {df['top10_overlap'].mean():.1f}/{TOPK}")

    mean_pool = agg("ic_fusion_pool")[0]
    print(f"\n[判定] 池内融合 IC 均值 {mean_pool:+.4f} -> "
          f"{'池内仍为正 -> 问题在下游(选股/配权/调仓), 进第二层' if mean_pool > 0 else '池内转负 -> 先修选股逻辑, 进第二层前需先处理'}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()
