# -*- coding: utf-8 -*-
"""factor_quick_check.py -- ai-factor-lab 等价"快速因子验证"CLI(月频面板).
任一候选(前缀表达式或中性化特征列) -> 一键输出:
  逐月RankIC均值 / ICIR / 胜率 + 五分位收益单调性(spread_q4_q0).
用法:
  python factor_quick_check.py -e "add(X1, X0)" -n value_plus_growth
  python factor_quick_check.py -b pb_inv_neu
无参默认: 融合4因子中性列(方向取 |IC| 口径) + GP top(gp1/gp4/gp8) 复查.
产出: data/factor_mine/quick_check_latest.json
"""
import argparse
import json
import os
import sys
import re
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gp_oos_check as G  # noqa: E402  (_parse_expr/_eval)

FC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "factor_mine")

_UN = {"add": lambda a, b: a + b, "sub": lambda a, b: a - b,
       "mul": lambda a, b: a * b, "div": lambda a, b: np.divide(a, b),
       "neg": lambda a: -a, "abs": np.abs, "sqrt": np.sqrt,
       "log": np.log, "inv": lambda a: np.divide(1.0, a)}


def _spec_pred(spec, X, feats):
    """spec 为前缀表达式 -> 树求值; 否则视为特征列名."""
    if re.fullmatch(r"X\d+", spec):
        return X[:, int(spec[1:])]
    if spec in feats:
        return X[:, feats.index(spec)]
    return G._eval(G._parse_expr(spec), X)


def _full_eval(pred, y, m):
    """返回 dict: 逐月RankIC均值/ICIR/胜率 + 五分位(各月 qcut 后收益均值再平均)."""
    df = pd.DataFrame({"y": np.asarray(y, float).ravel(),
                       "p": np.asarray(pred, float).ravel(),
                       "m": np.asarray(m, object).ravel()})
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    if len(df) < 200 or df["p"].nunique() < 10 or df["y"].nunique() < 10:
        return {"n_months": 0, "error": "有效样本不足"}
    ics = []
    q_means = []
    for _, g in df.groupby("m"):
        if len(g) <= 30:
            continue
        ic = g["p"].corr(g["y"], method="spearman")
        if np.isfinite(ic):
            ics.append(ic)
        try:
            gg = g.copy()
            gg["q"] = pd.qcut(gg["p"].rank(method="first"), 5, labels=False)
            q_means.append(gg.groupby("q")["y"].mean())
        except Exception:  # noqa: BLE001
            pass
    if len(ics) < 6:
        return {"n_months": len(ics), "error": "有效月份不足"}
    ic = np.asarray(ics, float)
    icir = float(ic.mean() / ic.std()) if ic.std() > 0 else 0.0
    qm = pd.DataFrame(q_means).mean()
    mono = bool(all(qm.get(i, np.nan) <= qm.get(i + 1, np.nan)
                    for i in range(4) if pd.notna(qm.get(i, np.nan)) and pd.notna(qm.get(i + 1, np.nan))))
    return {
        "n_months": int(len(ic)),
        "mean_ic": round(float(ic.mean()), 5),
        "icir": round(float(icir), 3),
        "win": round(float((ic > 0).mean()), 3),
        "quintile": {
            "q0": round(float(qm.get(0, np.nan)), 5),
            "q4": round(float(qm.get(4, np.nan)), 5),
            "spread_q4_q0": round(float(qm.get(4, np.nan) - qm.get(0, np.nan)), 5),
            "monotone_asc": mono,
        },
    }


def load_panel():
    df = pd.read_parquet(G.P)
    dc = "date" if "date" in df.columns else "ym"
    df["ym"] = df[dc].astype(str).str[:7]
    cols = G.FEATS + [G.YCOL, "ym"]
    df = df[cols].dropna(subset=cols).reset_index(drop=True)
    df[G.FEATS] = df[G.FEATS].clip(lower=-8, upper=8)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-e", "--expr", action="append", default=[])
    ap.add_argument("-n", "--name", action="append", default=[])
    ap.add_argument("-b", "--base", action="append", default=[])
    args = ap.parse_args()

    df = load_panel()
    X = df[G.FEATS].values.astype(float)
    lo, hi = np.nanpercentile(df[G.YCOL], [1, 99])
    y = np.clip(df[G.YCOL].values.astype(float), lo, hi)
    m = df["ym"].values

    specs = [("base", args.base[i] if i < len(args.base) else n, n)
             for i, n in enumerate(args.base)]
    specs += [("expr", args.name[i] if i < len(args.name) else f"e{i}", e)
              for i, e in enumerate(args.expr)]
    if not specs:
        specs = [("base", "pb_inv_neu", "pb_inv_neu"), ("base", "ep_neu", "ep_neu"),
                 ("base", "ocf_ps_neu", "ocf_ps_neu"), ("base", "roe_yy_chg_neu", "roe_yy_chg_neu"),
                 ("expr", "gp1", "add(X1, add(abs(neg(X0)), X2))"),
                 ("expr", "gp4", "div(sub(abs(X8), add(X6, X3)), add(abs(X4), sqrt(X12)))"),
                 ("expr", "gp8", "div(-0.660, X3)")]

    res = {}
    for kind, name, spec in specs:
        try:
            pred = _spec_pred(spec, X, G.FEATS)
            res[name] = {"kind": kind, "spec": spec, **_full_eval(pred, y, m)}
        except Exception as ex:  # noqa: BLE001
            res[name] = {"kind": kind, "spec": spec, "error": f"{type(ex).__name__}: {str(ex)[:100]}"}

    fp = os.path.join(FC, "quick_check_latest.json")
    payload = {"built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
               "metric": "月频面板(2019-01..2025-07, 79月): 逐月RankIC均值/ICIR/胜率 + 五分位",
               "rows": int(len(df)), "results": res}
    json.dump(payload, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for k, v in res.items():
        q = v.get("quintile") or {}
        print(f"{k:16s} IC={v.get('mean_ic')} ICIR={v.get('icir')} win={v.get('win')} "
              f"spread={q.get('spread_q4_q0')} mono={q.get('monotone_asc')} | {v['spec'][:64]}")


if __name__ == "__main__":
    main()
