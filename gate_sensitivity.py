# -*- coding: utf-8 -*-
"""门控参数敏感性分析 (gate_sensitivity).

基于 B 路径重放 (一次载数据, 批量模拟) 回答:
  1) IC 均值阈值 对结果影响多大?  2) 负占比阈值?
  3) 暴露下限是否收益损失主因?  4) 哪个参数最敏感?

方法:
  A. 单参数扫描: 每次只滑一个参数, 其余固定当前默认值 (步长见 GRID).
  B. 随机网格搜索: 在全参数网格中确定性抽样 N 组, 找满足
     Sharpe差 > -0.05 且 回撤改善 >= 0.5pp 且 risk天数占比 ∈ [15%,25%]
     且 CAGR差 > -2pp 的参数组合.

评估口径均相对 B 路径等权基准 (非实盘承诺).
用法: python gate_sensitivity.py [--n-grid 200] [--output gate_sensitivity.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)

from backtest_with_gate import load_dataset, simulate  # noqa: E402

DATA_DIR = os.path.join(_BASE, "data")
OUT_JSON = os.path.join(DATA_DIR, "gate_sensitivity.json")

# 当前默认参数 (对齐 data/factor_gate_config.json, 2026-09-07)
CURRENT = {
    "ic_risk_mean": -0.005,
    "ic_neg_share_risk": 0.60,
    "exp_risk": 0.90,
    "exp_caution": 0.95,
    "hyst_enter": 5,
    "hyst_exit": 5,
}

# 单参数扫描网格 (用户设计方案)
GRID = {
    "ic_risk_mean": [-0.003, -0.006, -0.009, -0.012, -0.015],
    "ic_neg_share_risk": [0.50, 0.55, 0.60, 0.65, 0.70],
    "exp_risk": [0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
    "exp_caution": [0.80, 0.85, 0.90, 0.95, 1.00],
    "hyst": [2, 3, 4, 5],   # 进入/退出同值
}

PARAM_LABEL = {
    "ic_risk_mean": "IC均值阈值", "ic_neg_share_risk": "负占比阈值",
    "exp_risk": "risk暴露下限", "exp_caution": "caution暴露", "hyst": "滞后天数",
}


def _overrides(param: str, value: float) -> dict:
    o = {k: v for k, v in CURRENT.items()}
    if param == "hyst":
        o["hyst_enter"] = int(value)
        o["hyst_exit"] = int(value)
    else:
        o[param] = value
    return o


def _row(ds, o: dict) -> dict:
    r = simulate(ds, cfg_overrides=o)
    if not r.get("ok"):
        return None
    dl = r["delta"]
    gs = r["gate_stats"]
    b = r["baseline"]
    return {
        "cfg": {k: v for k, v in o.items() if k in CURRENT},
        "sharpe_diff": dl.get("sharpe"),
        "cagr_diff_pp": (dl.get("cagr") * 100) if dl.get("cagr") is not None else None,
        "dd_improve_pp": ((b["max_drawdown"] - r["gated"]["max_drawdown"]) * 100)
                         if b.get("max_drawdown") is not None else None,
        "risk_share": gs["risk_day_share"],
        "avg_exposure": gs["avg_exposure"],
        "gated_sharpe": r["gated"].get("sharpe"),
    }


def one_at_a_time(ds) -> dict:
    scans = {}
    for param, vals in GRID.items():
        rows = []
        for v in vals:
            o = _overrides(param, v)
            row = _row(ds, o)
            if row is None:
                continue
            row["param"] = param
            row["value"] = v
            row["is_current"] = (
                v == CURRENT[param] if param != "hyst"
                else int(v) == CURRENT["hyst_enter"])
            rows.append(row)
        scans[param] = rows
    return scans


def sensitivity_spread(scans: dict) -> list:
    out = []
    for param, rows in scans.items():
        sd = [r["sharpe_diff"] for r in rows if r["sharpe_diff"] is not None]
        cd = [r["cagr_diff_pp"] for r in rows if r["cagr_diff_pp"] is not None]
        out.append({
            "param": param,
            "label": PARAM_LABEL.get(param, param),
            "n": len(rows),
            "sharpe_diff_min": round(min(sd), 4) if sd else None,
            "sharpe_diff_max": round(max(sd), 4) if sd else None,
            "sharpe_diff_spread": round(max(sd) - min(sd), 4) if sd else None,
            "cagr_diff_min_pp": round(min(cd), 2) if cd else None,
            "cagr_diff_max_pp": round(max(cd), 2) if cd else None,
            "cagr_diff_spread_pp": round(max(cd) - min(cd), 2) if cd else None,
        })
    out.sort(key=lambda x: -(x["sharpe_diff_spread"] or 0.0))
    return out


def grid_search(ds, n: int = 200, seed: int = 42) -> dict:
    rng = np.random.default_rng(seed)
    keys = list(GRID.keys())
    combos = []
    best = []
    for _ in range(n):
        o = {k: v for k, v in CURRENT.items()}
        for k in keys:
            if k == "hyst":
                o["hyst_enter"] = o["hyst_exit"] = int(rng.choice(GRID[k]))
            else:
                o[k] = float(rng.choice(GRID[k]))
        if any(c == o for c in combos):
            continue
        # 合理性约束: risk 应比 caution 更保守 (暴露更低)
        if o["exp_risk"] > o["exp_caution"]:
            continue
        combos.append(o)
        row = _row(ds, o)
        if row is None:
            continue
        ok = (row["sharpe_diff"] is not None and row["sharpe_diff"] > -0.05
              and row["dd_improve_pp"] is not None and row["dd_improve_pp"] >= 0.5
              and row["risk_share"] is not None and 0.15 <= row["risk_share"] <= 0.25
              and row["cagr_diff_pp"] is not None and row["cagr_diff_pp"] > -2.0)
        row["pass"] = bool(ok)
        if ok:
            best.append(row)
    best.sort(key=lambda r: -(r["sharpe_diff"] or -9.0))
    return {
        "sampled": len(combos),
        "pass_count": len(best),
        "pass_combos": best[:10],
    }


def _fmt_pct(x, nd=2):
    return None if x is None else round(x, nd)


def main() -> None:
    ap = argparse.ArgumentParser(description="门控参数敏感性分析")
    ap.add_argument("--start", default="2026-03-12")
    ap.add_argument("--end", default="2026-09-04")
    ap.add_argument("--n-grid", type=int, default=200)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    ds = load_dataset(args.start, args.end)
    if not ds.get("ok"):
        print(json.dumps({"ok": False, "error": ds.get("error")}))
        sys.exit(1)

    scans = one_at_a_time(ds)
    spreads = sensitivity_spread(scans)
    grid = grid_search(ds, n=args.n_grid)

    # 打印扫描表
    for param, rows in scans.items():
        lab = PARAM_LABEL.get(param, param)
        print(f"\n=== 单参数扫描: {lab} ===")
        print("值 | Sharpe差 | CAGR差(pp) | 回撤改善(pp) | risk占比 | 平均暴露 | 达标组合")
        for r in rows:
            cur = " *" if r["is_current"] else ""
            dd = r["dd_improve_pp"]
            ok_sharpe = (r["sharpe_diff"] or -9) > -0.05
            ok_cagr = (r["cagr_diff_pp"] or -99) > -2.0
            ok_dd = dd is not None and dd >= 0.5
            ok_share = r["risk_share"] is not None and 0.15 <= r["risk_share"] <= 0.25
            mark = "PASS" if (ok_sharpe and ok_cagr and ok_dd and ok_share) else "—"
            print(f"{r['value']}{cur} | {_fmt_pct(r['sharpe_diff'],4)} | "
                  f"{_fmt_pct(r['cagr_diff_pp'])} | {_fmt_pct(dd)} | "
                  f"{_fmt_pct(r['risk_share']*100,1)}% | {_fmt_pct(r['avg_exposure'],3)} | {mark}")

    print("\n=== 最敏感参数 (按 Sharpe差 变动幅度) ===")
    for s in spreads:
        print(f"{s['label']:<8} spread={s['sharpe_diff_spread']} "
              f"(min {s['sharpe_diff_min']} -> max {s['sharpe_diff_max']}) "
              f"CAGR spread {s['cagr_diff_spread_pp']}pp")

    print(f"\n=== 随机网格搜索 {grid['sampled']} 组, 达标 {grid['pass_count']} 组 ===")
    if grid["pass_combos"]:
        print("达标组合示例 (Top5 by Sharpe差):")
        for c in grid["pass_combos"][:5]:
            print(f"  {c['cfg']} sharpe_diff={c['sharpe_diff']} "
                  f"cagr={_fmt_pct(c['cagr_diff_pp'])}pp "
                  f"dd_impr={_fmt_pct(c['dd_improve_pp'])}pp "
                  f"risk_share={_fmt_pct(c['risk_share']*100,1)}%")

    out_path = args.output or OUT_JSON
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "ok": True, "current": CURRENT, "grid": {k: v for k, v in GRID.items()},
            "scans": scans, "spreads": spreads, "grid_search": grid,
            "note": "基于B路径等权主链路重放; 非实盘承诺",
        }, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n已保存: {out_path}")


if __name__ == "__main__":
    main()
