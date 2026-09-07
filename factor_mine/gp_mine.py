# -*- coding: utf-8 -*-
"""QuantGplearn 范式等价: gplearn 非线性因子深度挖掘试跑(fitness=月度 RankIC)
输入: monthly_panel_neutral.parquet(*_neu 16 因子 + fwd20 收益)
度量: 逐月横截面 RankIC(预测, 收益) 的均值作为适应度(越大越好)
输出: data/factor_mine/gp_mine_ledger.jsonl + gp_mine_report.json
"""
import json
import os
import sys
import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gp_sklearn_patch as _patch  # noqa: E402  (需在 gplearn import 前)
_patch.apply()
FC = os.path.join(_BASE, "data", "factor_mine")
P = os.path.join(_BASE, "data", "h5i", "factors", "monthly_panel_neutral.parquet")

FEATS = ["pb_inv_neu", "ep_neu", "ocf_ps_neu", "roe_yy_chg_neu", "roe_neu", "eps_neu",
         "np_yoy_neu", "ded_np_yoy_neu", "rev_yoy_neu", "gross_margin_neu",
         "net_margin_neu", "debt_ratio_neu", "bvps_neu", "peg_neu"]
YCOL = "fwd20"


def _month_rankic(y, pred):
    """按样本顺序分月? 简化用 pooled; 为贴近逐月口径, 传入了 month 数组经由分组在外部算.
    此处实现为对给定(y,pred,month)逐月 Spearman 再取均值."""
    return _rankic_impl(y, pred)


def _rankic_impl(y, pred, month=None):
    y = np.asarray(y, float).ravel()
    pred = np.asarray(pred, float).ravel()
    if len(y) < 200 or len(y) != len(pred):
        return -1.0
    df = pd.DataFrame({"y": y, "p": pred}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(df) < max(120, int(len(y) * 0.3)) or df["p"].nunique() < 10 or df["y"].nunique() < 10:
        return -1.0
    if month is None:
        return float(df["p"].corr(df["y"], method="spearman"))
    df["m"] = np.asarray(month, object).ravel()[: len(y)]
    if df["m"].nunique() < 6:
        return -1.0
    ics = df.groupby("m").apply(
        lambda g: g["p"].corr(g["y"], method="spearman") if len(g) > 30 else np.nan,
        include_groups=False).dropna()
    return float(ics.mean()) if len(ics) else -1.0


def load(frac=0.35, seed=7):
    df = pd.read_parquet(P)
    dc = "date" if "date" in df.columns else ("month" if "month" in df.columns else "ym")
    df["ym"] = df[dc].astype(str).str[:7]
    df = df[FEATS + [YCOL, "ym"]].dropna(subset=FEATS + [YCOL])
    df[FEATS] = df[FEATS].clip(lower=-8, upper=8)  # 稳健化
    sub = df.sample(frac=frac, random_state=seed).reset_index(drop=True)
    return sub


def main():
    sub = load()
    X = sub[FEATS].values.astype(float)
    # 收益 winsorize
    lo, hi = np.nanpercentile(sub[YCOL], [1, 99])
    y = np.clip(sub[YCOL].values.astype(float), lo, hi)
    months = sub["ym"].values

    def metric(y_t, y_p, w):
        return _rankic_impl(y_t, y_p, months)

    from gplearn.genetic import SymbolicRegressor
    from gplearn.functions import make_function
    from gplearn.fitness import make_fitness
    fit = make_fitness(function=metric, greater_is_better=True)
    est = SymbolicRegressor(
        population_size=200, generations=3, tournament_size=8,
        function_set=["add", "sub", "mul", "div", "neg", "inv", "sqrt", "log", "abs"],
        parsimony_coefficient=0.01, p_crossover=0.7, p_subtree_mutation=0.1,
        p_hoist_mutation=0.05, p_point_mutation=0.1,
        metric=fit, random_state=11, verbose=0, n_jobs=1)
    est.fit(X, y)

    pool = list(est._programs[-1])
    pool.sort(key=lambda p: p.raw_fitness_, reverse=True)
    out = []
    for i, prog in enumerate(pool[:8]):
        out.append({
            "rank": i + 1,
            "program": str(prog),
            "length": prog.length_,
            "fitness": round(float(prog.raw_fitness_), 5),
        })
    led = os.path.join(FC, "gp_mine_ledger.jsonl")
    with open(led, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    rep = {
        "framework": "gplearn(QuantGplearn 范式等价, 本机 Py3.10/Windows)",
        "features": FEATS, "y": YCOL, "n_rows_fit": int(len(sub)),
        "params": {"pop": 200, "gen": 3, "parsimony": 0.01},
        "metric": "逐月 RankIC 均值(pooled 校正)", "top_candidates": out,
    }
    json.dump(rep, open(os.path.join(FC, "gp_mine_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    for r in out[:5]:
        print(r["rank"], r["fitness"], r["program"][:160])


if __name__ == "__main__":
    main()
