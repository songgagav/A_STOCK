# -*- coding: utf-8 -*-
"""GP 候选样本外(OOS)检验: 在 gp_mine 未抽到的行上重放 top 表达式.
对比基线 = 各中性化单因子(原方向) 在 OOS 行的逐月 RankIC.
产出: data/factor_mine/gp_oos_report.json
"""
import json
import os
import re
import sys
import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FC = os.path.join(_BASE, "data", "factor_mine")
P = os.path.join(_BASE, "data", "h5i", "factors", "monthly_panel_neutral.parquet")

FEATS = ["pb_inv_neu", "ep_neu", "ocf_ps_neu", "roe_yy_chg_neu", "roe_neu", "eps_neu",
         "np_yoy_neu", "ded_np_yoy_neu", "rev_yoy_neu", "gross_margin_neu",
         "net_margin_neu", "debt_ratio_neu", "bvps_neu", "peg_neu"]
YCOL = "fwd20"

_UN = {"add": lambda a, b: a + b, "sub": lambda a, b: a - b,
       "mul": lambda a, b: a * b, "div": lambda a, b: np.divide(a, b),
       "neg": lambda a: -a, "abs": np.abs, "sqrt": np.sqrt,
       "log": np.log, "inv": lambda a: np.divide(1.0, a)}


def _parse_expr(s: str):
    s = s.replace(" ", "")
    toks = re.findall(r"X\d+|add|sub|mul|div|neg|abs|sqrt|log|inv|[-+]?\d*\.?\d+|\(|\)|,", s)
    toks = [t for t in toks if t not in ("(", ")", ",")]
    it = iter(toks)

    def rec():
        t = next(it)
        if t.startswith("X"):
            return ("X", int(t[1:]))
        if t in _UN:
            n = 1 if t in ("neg", "abs", "sqrt", "log", "inv") else 2
            args = [rec() for _ in range(n)]
            return (t, args)
        return ("C", float(t))

    return rec()


def _eval(node, X):
    kind = node[0]
    if kind == "X":
        return X[:, node[1]]
    if kind == "C":
        return np.full(X.shape[0], node[1], float)
    fn = node[0]
    args = node[1]
    if len(args) == 1:
        return _UN[fn](_eval(args[0], X))
    return _UN[fn](_eval(args[0], X), _eval(args[1], X))


def monthly_rankic(pred, y, m):
    df = pd.DataFrame({"y": np.asarray(y, float).ravel(),
                       "p": np.asarray(pred, float).ravel(),
                       "m": np.asarray(m, object).ravel()})
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    if df["p"].nunique() < 10 or df["y"].nunique() < 10 or len(df) < 200:
        return {"n": 0, "mean_ic": None, "win": None}
    ics = df.groupby("m").apply(
        lambda g: g["p"].corr(g["y"], method="spearman") if len(g) > 30 else np.nan,
        include_groups=False).dropna()
    if not len(ics):
        return {"n": 0, "mean_ic": None, "win": None}
    return {"n": int(len(ics)), "mean_ic": round(float(ics.mean()), 5),
            "win": round(float((ics > 0).mean()), 3)}


def main():
    df = pd.read_parquet(P)
    dc = "date" if "date" in df.columns else "ym"
    df["ym"] = df[dc].astype(str).str[:7]
    df = df[FEATS + [YCOL, "ym", "symbol"]].dropna(subset=FEATS + [YCOL]).reset_index(drop=True)
    df[FEATS] = df[FEATS].clip(lower=-8, upper=8)

    # 与 gp_mine.load 完全一致的训练抽样 -> 其余为 OOS
    train_idx = df.sample(frac=0.35, random_state=7).index
    oos = df.loc[~df.index.isin(train_idx)].reset_index(drop=True)
    X = oos[FEATS].values.astype(float)
    lo, hi = np.nanpercentile(oos[YCOL], [1, 99])
    y = np.clip(oos[YCOL].values.astype(float), lo, hi)
    m = oos["ym"].values
    print(f"OOS rows: {len(oos)}")

    report = json.load(open(os.path.join(FC, "gp_mine_report.json"), encoding="utf-8"))
    exprs = {f"gp_{r['rank']}": r["program"] for r in report["top_candidates"]}

    # 单因子基线(中性化列, 原始正方向)
    baseline = {}
    for f in ["pb_inv_neu", "ep_neu", "ocf_ps_neu", "roe_yy_chg_neu"]:
        r = monthly_rankic(X[:, FEATS.index(f)], y, m)
        baseline[f] = r

    rows = {}
    for name, s in exprs.items():
        try:
            node = _parse_expr(s)
            pred = _eval(node, X)
        except Exception as e:  # noqa: BLE001
            rows[name] = {"program": s, "error": str(e)[:120]}
            continue
        rows[name] = {"program": s, **monthly_rankic(pred, y, m)}

    out = {"built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
           "panel": P, "oos_rows": int(len(oos)), "metric": "逐月 RankIC 均值(OOS 未训练行)",
           "pooled_fitness": {f"gp_{r['rank']}": r["fitness"] for r in report["top_candidates"]},
           "baseline_neu": baseline, "gp_oos": rows}
    fp = os.path.join(FC, "gp_oos_report.json")
    json.dump(out, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({"baseline": baseline, "gp": rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
