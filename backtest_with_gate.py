# -*- coding: utf-8 -*-
"""路径B: 门控历史重放回测 (backtest_with_gate).

在 vnpy 121 日主链路 (10 只等权买入持有) 的历史日频数据上, 用融合因子 IC
缓存逐日还原"若当日门控生效"的暴露变化, 估计理论改善幅度 (含调仓成本).

假设与边界 (如实标注, 非实盘承诺):
  1. 基准组合 = vnpy 主链路 10 只标的的等权日收益均值 (无成分换手近似).
  2. 门控决策 = 当日开盘用截至 6 个交易日前的 IC (无前视) 判定 regime
     (联合 risk 判据 + risk 3 日滞后); exposure 取 factor_gate 配置.
  3. 换仓成本: 档位切换按 |Δe| × 0.15% 计提.
  4. 冻结新买入在静态等权组合上无额外效果 (不追买), 仅影响暴露档.

用法:
  python backtest_with_gate.py --start 2026-03-12 --end 2026-09-04 \
      --gate-enabled True --output backtest_gated.json

内部拆分为 load_dataset() -> simulate() 供 gate_sensitivity.py 批量扫参复用
(一次载数据, 多次模拟).
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

DATA_DIR = os.path.join(_BASE, "data")
IC_CACHE = os.path.join(DATA_DIR, "factor_mine", "fusion_ic_121d.json")
VNPY_SUMMARY = os.path.join(DATA_DIR, "vnpy_backtest", "20260904", "summary.json")
COST_PER_SHIFT = 0.0015          # 档位切换换仓成本 (单边 0.15%)
GATE_LAG_DAYS = 6                # IC as_of 至少早 6 个交易日, 规避 fwd5 前视


# ------------------------------------------------------------------
# 数据集 (一次加载)
# ------------------------------------------------------------------
def load_dataset(start: str = "2026-03-12", end: str = "2026-09-04") -> dict:
    import factor_fusion as ff
    from factor_gate import _window_stats

    v = json.load(open(VNPY_SUMMARY, encoding="utf-8"))
    universe = [str(s)[:6] for s in (v.get("universe") or [])]

    if not os.path.exists(IC_CACHE):
        return {"ok": False, "error": "IC 缓存缺失 (先运行 refresh_gate_ic.py)"}
    with open(IC_CACHE, encoding="utf-8") as f:
        ic_raw = (json.load(f) or {}).get("daily_ic") or []

    syms = ", ".join(f"'{s}'" for s in universe)
    q = (f"SELECT CAST(ts AS DATE) d, symbol, change_pct FROM daily_bars "
         f"WHERE CAST(ts AS DATE) >= DATE '{start}' AND CAST(ts AS DATE) <= DATE '{end}' "
         f"AND symbol IN ({syms})")
    bars = ff._sql(q)
    bars["d"] = pd_dt(bars["d"])
    if bars.empty:
        return {"ok": False, "error": "窗口内无主链路 bar 数据"}

    cal = sorted(bars["d"].unique())
    R = []
    for d in cal:
        seg = bars.loc[bars["d"] == d, "change_pct"].to_numpy(dtype=float)
        seg = seg[np.isfinite(seg)]
        R.append(float(np.mean(seg) / 100.0) if len(seg) else 0.0)
    R = np.asarray(R, dtype=float)
    cal_str = [str(d)[:10] for d in cal]

    pos_of = {str(d)[:10]: i for i, d in enumerate(cal)}
    ic_entries = [(pos_of[e.get("date")], e.get("fwd5_ic"))
                  for e in ic_raw if str(e.get("date"))[:10] in pos_of]
    ic_entries.sort(key=lambda x: x[0])
    ic_pos_arr = np.asarray([p for p, _ in ic_entries], dtype=int)
    ic_vals = [v for _, v in ic_entries]

    # date(YYYY-MM-DD) → 日历位置 (供因子 IC 曲线对齐, 2026-09-07)
    cal_pos = {str(d)[:10]: i for i, d in enumerate(cal)}

    return {
        "ok": True,
        "universe": universe, "start": start, "end": end,
        "cal_str": cal_str, "R": R,
        "cal_pos": cal_pos,
        "ic_pos_arr": ic_pos_arr, "ic_vals": ic_vals,
        "_window_stats": _window_stats,
    }


def pd_dt(x):
    import pandas as pd
    return pd.to_datetime(x)


# ------------------------------------------------------------------
# 模拟 (可批量调参)
# ------------------------------------------------------------------
def _make_cfg(overrides: dict | None):
    from factor_gate import _Cfg
    cfg = _Cfg()
    for k, v in (overrides or {}).items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


def simulate(ds: dict, cfg_overrides: dict | None = None,
             detail: bool = False,
             use_change_detection: bool = True,
             use_factor_drift: bool = True) -> dict:
    """在已加载数据集上按给定 cfg 覆盖跑一次带门控模拟.

    use_change_detection / use_factor_drift (2026-09-07):
      是否在逐日 compute_plan 中注入无前视的滚动 Sharpe 变点检测与个体因子
      IC 漂移监控上下文 (读 factor_gate 配置: FG_SHARPE_CHANGE_THRESHOLD 等).
      默认开启 — 使重放与生产 gate 行为一致.

    Returns: {ok, baseline{..}, gated{..}, delta{..}, gate_stats{..},
              per_day(可选), assumptions{..}, cfg_overrides}
    """
    from factor_gate import compute_plan, detect_sharpe_change_point, check_factor_ic_drift
    from strategy_validation import curve_metrics
    from attribution_analysis import segment_report

    if not ds.get("ok"):
        return {"ok": False, "error": ds.get("error")}
    R = ds["R"]
    cal_str = ds["cal_str"]
    ic_pos_arr = ds["ic_pos_arr"]
    ic_vals = ds["ic_vals"]
    n = len(R)
    cfg = _make_cfg(cfg_overrides)
    _window_stats = ds["_window_stats"]

    # ---- (2026-09-07) 无前视上下文 ----
    # 滚动 Sharpe: sr[j] = 截至第 j 日的 20 日年化 Sharpe (仅用 R[0..j]).
    def _rolling_sharpe(win: int = 20) -> list:
        out = []
        for j in range(n):
            a = R[max(0, j - win + 1):j + 1]
            if len(a) >= 4 and np.std(a, ddof=1) > 1e-12:
                mu, sd = float(np.mean(a)), float(np.std(a, ddof=1))
                out.append((mu / sd) * np.sqrt(252))
            else:
                out.append(float("nan"))
        return out

    sr = _rolling_sharpe()

    # 个体因子 IC 曲线 (vol/mom_20/reversal) → 对齐到回放日历位置
    import os as _os
    _ic_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "data", "ic")
    _factor_curves: dict[str, dict] = {}
    for name in ("vol", "mom_20", "reversal"):
        fp = _os.path.join(_ic_dir, f"ic_curve_{name}_k20.csv")
        if not _os.path.exists(fp):
            continue
        try:
            import csv
            pos_vals = []
            with open(fp, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    d = str(row.get("day", "")).strip()
                    if d not in ds.get("cal_pos", {}):
                        continue
                    try:
                        v = float(row["ic_h20"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    if np.isfinite(v):
                        pos_vals.append((ds["cal_pos"][d], v))
            if pos_vals:
                pos_vals.sort()
                _factor_curves[name] = {
                    "pos": [p for p, _ in pos_vals],
                    "val": [v for _, v in pos_vals],
                }
        except Exception:
            continue

    def _drift_at(i: int) -> dict:
        """截至决策日 i (用 ≤ i-GATE_LAG_DAYS 的因子 IC) 的个体因子漂移."""
        limit = i - GATE_LAG_DAYS
        ics = {}
        for name, cv in _factor_curves.items():
            import bisect
            k = bisect.bisect_right(cv["pos"], limit)
            vals = cv["val"][:k]
            if len(vals) < 30:
                continue
            long_m = float(np.mean(vals[-cfg.ic_drift_long_window:])) \
                if len(vals) >= cfg.ic_drift_long_window else float(np.mean(vals))
            short_m = float(np.mean(vals[-20:]))
            ics[name] = {"long_mean": long_m, "short_mean": short_m}
        if not ics:
            return {}
        res = check_factor_ic_drift(ics, cfg=cfg)
        res["unstable_limit"] = cfg.unstable_factor_limit
        return res

    def _gate_at(pos: int, prev_ret: float, hyst: dict):
        upto_idx = int(np.searchsorted(ic_pos_arr, pos - GATE_LAG_DAYS, side="right"))
        st = _window_stats(ic_vals[:upto_idx], cfg.ic_window)
        kwargs = {}
        if use_change_detection:
            # 决策日只可用到 pos-1 的滚动 Sharpe (无前视)
            hist = [x for x in sr[:max(pos, 0)] if x == x]
            if len(hist) >= 4:
                kwargs["sharpe_change_result"] = detect_sharpe_change_point(hist, cfg=cfg)
        if use_factor_drift:
            dr = _drift_at(pos)
            if dr:
                kwargs["factor_ic_drift_result"] = dr
        return compute_plan(st["mean"], st["neg"], daily_loss=prev_ret,
                            base_interval=3, ic_ir=st["ir"], hyst=hyst,
                            cfg=cfg, **kwargs)

    eq_base = np.ones(n)
    eq_gate = np.ones(n)
    exposure = np.ones(n)
    regimes, costs = [], []
    cur_e = 1.0
    hyst = {"risk_streak": 0, "exit_streak": 0, "in_risk": False}
    for i in range(n):
        if i > 0:
            eq_base[i] = eq_base[i - 1] * (1.0 + R[i])
        plan = _gate_at(i, (R[i - 1] if i > 0 else 0.0), hyst)
        e = float(plan.get("exposure_mult", 1.0) or 1.0)
        cost = abs(e - cur_e) * COST_PER_SHIFT
        eq_gate[i] = eq_gate[i - 1] * (1.0 + e * R[i]) - eq_gate[i - 1] * cost
        cur_e = e
        exposure[i] = e
        regimes.append({"date": cal_str[i], "regime": plan.get("regime"),
                        "raw_regime": plan.get("raw_regime"),
                        "exposure": e, "freeze": plan.get("freeze_new_buys")})
        costs.append(cost)

    base_curve = [{"day": cal_str[i], "equity": float(eq_base[i] * 100.0)}
                  for i in range(n)]
    gate_curve = [{"day": cal_str[i], "equity": float(eq_gate[i] * 100.0)}
                  for i in range(n)]
    bm = curve_metrics([c["equity"] for c in base_curve],
                       dates=[c["day"] for c in base_curve])
    gm = curve_metrics([c["equity"] for c in gate_curve],
                       dates=[c["day"] for c in gate_curve])
    bs = segment_report(base_curve)
    gs = segment_report(gate_curve)

    def _pick(m, seg):
        return {"sharpe": m.get("sharpe"), "cagr": m.get("cagr"),
                "max_drawdown": m.get("max_drawdown"), "recovery": m.get("recovery"),
                "pos_day_share": seg.get("pos_day_share"),
                "top5_share": seg.get("top5_gain_days_share")}

    b, g = _pick(bm, bs), _pick(gm, gs)
    delta = {k: (round(float(g[k]) - float(b[k]), 4)
                 if b[k] is not None and g[k] is not None else None)
             for k in b}
    counts = {}
    for x in regimes:
        counts[x["regime"]] = counts.get(x["regime"], 0) + 1
    ex = np.asarray(exposure)
    out = {
        "ok": True,
        "baseline": b,
        "gated": g,
        "delta": delta,
        "gate_stats": {
            "regime_counts": counts,
            "risk_day_share": round(counts.get("risk", 0) / n, 4),
            "avg_exposure": round(float(ex.mean()), 4),
            "total_shift_cost": round(float(np.sum(costs)), 6),
        },
        "assumptions": {
            "universe": ds["universe"], "start": ds["start"], "end": ds["end"],
            "n_days": n, "gate_lag_days": GATE_LAG_DAYS,
            "cost_per_shift": COST_PER_SHIFT,
            "cfg_overrides": cfg_overrides or {},
        },
        "cfg_overrides": dict(cfg_overrides or {}),
    }
    if detail:
        out["per_day"] = regimes
    return out


# ------------------------------------------------------------------
# 单次完整回放 (兼容 CLI)
# ------------------------------------------------------------------
def replay(start: str = "2026-03-12", end: str = "2026-09-04",
           gate_enabled: bool = True, cfg_overrides: dict | None = None) -> dict:
    if not gate_enabled:
        return {"ok": True, "note": "gate 关闭, 无 gated 结果"}
    ds = load_dataset(start, end)
    if not ds.get("ok"):
        return {"ok": False, "error": ds.get("error")}
    return simulate(ds, cfg_overrides=cfg_overrides, detail=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="门控历史重放回测 (路径B)")
    ap.add_argument("--start", default="2026-03-12")
    ap.add_argument("--end", default="2026-09-04")
    ap.add_argument("--gate-enabled", default=True, action="store_true")
    ap.add_argument("--gate-disabled", dest="gate_enabled", action="store_false")
    ap.add_argument("--output", default="backtest_gated.json")
    args = ap.parse_args()

    res = replay(args.start, args.end, args.gate_enabled)
    if not res.get("ok"):
        print(json.dumps({"ok": False, "error": res.get("error")}))
        sys.exit(1)
    out_path = os.path.join(DATA_DIR, args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: v for k, v in res.items() if k != "per_day"},
                     ensure_ascii=False, indent=2, default=str))
    print(f"\n已保存: {out_path}")


if __name__ == "__main__":
    main()
