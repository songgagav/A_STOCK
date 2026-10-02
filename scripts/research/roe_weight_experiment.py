# -*- coding: utf-8 -*-
"""`roe_yy_chg` 权重的 METHOD-1 下游组合表现实验。

方向固定为生产值 +1，只改变相对权重；在相同 11 个非重叠前向窗口、相同候选池、
相同旧复合分和相同目标配权规则下比较 top10 组合收益。输出只记录数值，不自动选择
或部署权重，避免用同一批样本既调参又宣布胜出。
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
import time
from pathlib import Path

_BASE = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
sys.path.insert(0, os.path.join(_BASE, "scripts"))
os.chdir(_BASE)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")
XSEC_DIR = os.path.join(_BASE, "data", "pit", "xsec")
FUSION_X_DIR = os.path.join(_BASE, "data", "pit", "fusion_x")
OUT = os.path.join(_BASE, "data", "roe_weight_experiment.json")
TOPK = 10

# 不从结果反推网格，避免样本内优化。0.7300 是“IC 排第三”对应的现有第三档权重；
# 0.5309 是当前权重的一半；0 是留一法消融；1.0618 是现行基线。
VARIANTS = {
    "current_1.0618": 1.0618,
    "rank3_0.7300": 0.7300,
    "half_0.5309": 0.5309,
    "ablation_0": 0.0,
}


def _fused_scores(df: pd.DataFrame, roe_weight: float) -> dict[str, float]:
    import factor_fusion as ff

    weights = dict(ff.FACTOR_WEIGHTS)
    weights["roe_yy_chg"] = float(roe_weight)
    z_by = {f: ff._residualize(df, f)[0] for f in weights if f in df.columns}
    syms = sorted(set().union(*(set(v) for v in z_by.values())) if z_by else set())
    raw = {}
    for sym in syms:
        raw[sym] = sum(w * ff.DIRECTIONS[f] * z_by[f][sym]
                       for f, w in weights.items() if sym in z_by.get(f, {}))
    if not raw:
        return {}
    vals = np.asarray(list(raw.values()), dtype=float)
    sd = float(vals.std())
    if not np.isfinite(sd) or sd <= 1e-12:
        return {}
    mean = float(vals.mean())
    return {sym: float((v - mean) / sd) for sym, v in raw.items()}


def summarize(rows: list[dict], variants: dict[str, float] = VARIANTS) -> dict:
    current = "current_1.0618"
    out = {}
    for name, weight in variants.items():
        rec = {"roe_yy_chg_weight": weight}
        for mode in ("equal", "weighted"):
            vals = [float(r["variants"][name][mode]) for r in rows]
            base = [float(r["variants"][current][mode]) for r in rows]
            delta = [v - b for v, b in zip(vals, base)]
            rec[mode] = {
                "n_windows": len(vals),
                "mean_return_pct": st.mean(vals),
                "median_return_pct": st.median(vals),
                "min_return_pct": min(vals),
                "max_return_pct": max(vals),
                "positive_windows": sum(v > 0 for v in vals),
                "paired_delta_vs_current_mean_pp": st.mean(delta),
                "paired_delta_vs_current_median_pp": st.median(delta),
                "windows_better_than_current": sum(d > 0 for d in delta),
            }
        overlaps = [r["variants"][name]["overlap_with_current"] for r in rows]
        rec["mean_top10_overlap_with_current"] = st.mean(overlaps)
        out[name] = rec
    return out


def run() -> dict:
    import factor_ic_forward as fif
    from factor_library import selector_weights
    from ml_fusion_bridge import FML_WEIGHT
    from pool_ic_diag import _old_score
    from target_weighting import allocate_target_weights

    windows = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    rows = []
    for win in windows:
        day, end = win["day"], (win.get("stats") or {}).get("end_date")
        t0 = time.time()
        pool_fp = os.path.join(XSEC_DIR, f"{day}.parquet")
        factor_fp = os.path.join(FUSION_X_DIR, f"{day}.parquet")
        if not os.path.isfile(pool_fp) or not os.path.isfile(factor_fp) or not end:
            continue
        pool = pd.read_parquet(pool_fp).copy()
        raw = pd.read_parquet(factor_fp).copy()
        if "sym6" not in pool.columns:
            pool["sym6"] = pool["canon"].astype(str).str.split(".").str[0].str.zfill(6)
        pool["sym6"] = pool["sym6"].astype(str).str.zfill(6)
        pool["old_score"] = _old_score(pool, selector_weights(as_of=day))
        ret = fif.fwd_return(day, end).rename(columns={"canon": "sym6"})
        base = pool.merge(ret[["sym6", "ret"]], on="sym6", how="inner")
        base = base[pd.to_numeric(base["ret"], errors="coerce").notna()].copy()
        scores = {name: _fused_scores(raw, weight) for name, weight in VARIANTS.items()}
        picks = {}
        rec = {"day": day, "end": end, "n_pool": int(len(base)), "variants": {}}
        for name, fmap in scores.items():
            m = base.copy()
            m["fusion_z"] = m["sym6"].map(fmap)
            m = m.dropna(subset=["fusion_z"])
            m["fusion_pct"] = m["fusion_z"].rank(pct=True)
            w = min(max(float(FML_WEIGHT), 0.0), 0.5)
            m["prod_score"] = (1.0 - w) * m["old_score"] + w * m["fusion_pct"]
            selected = m.nlargest(TOPK, "prod_score").copy()
            picks[name] = set(selected["sym6"])
            equal_ret = float(selected["ret"].astype(float).mean())
            items = [{"canon": sym, "score": float(score), "fml": float(fz)}
                     for sym, score, fz in zip(selected["sym6"], selected["prod_score"],
                                               selected["fusion_z"])]
            allocate_target_weights(items)
            weights = np.asarray([float(x["target_weight"]) for x in items])
            weighted_ret = float(np.dot(weights, selected["ret"].to_numpy(dtype=float)))
            rec["variants"][name] = {
                "equal": equal_ret,
                "weighted": weighted_ret,
                "picks": sorted(picks[name]),
                "overlap_with_current": 0,
            }
        current = picks["current_1.0618"]
        for name in VARIANTS:
            rec["variants"][name]["overlap_with_current"] = len(current & picks[name])
        rec["elapsed_s"] = round(time.time() - t0, 2)
        rows.append(rec)
        print(f"{day}: " + "  ".join(
            f"{name}={rec['variants'][name]['weighted']:+.2f}%" for name in VARIANTS),
            flush=True)

    result = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "method": "METHOD-1 downstream portfolio performance",
        "window_mode": "11 non-overlapping forward 120-trading-day windows",
        "ranking": "production old_score + FML_WEIGHT * fusion percentile",
        "direction_roe_yy_chg": 1,
        "threshold_applied": False,
        "decision": None,
        "note": ("只记录下游组合表现；同一批窗口不得同时用于调参和最终验收。"
                 "方向 +1 已由留一法/中性化 IC 确认，本实验不改方向。"),
        "variants": VARIANTS,
        "summary": summarize(rows),
        "windows": rows,
    }
    return result


def main() -> int:
    result = run()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print("已保存:", OUT)
    return 0 if result["windows"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
