# -*- coding: utf-8 -*-
"""统一因子评估器 v2: 评估指标体系(3.1) + 滚动IC/ICIR/t值/多空价差/IC衰减/换手率."""
from __future__ import annotations

import math
import warnings
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

# ===================================================================
# 基础指标
# ===================================================================

def daily_ic(frame: pd.DataFrame, factor: str, fwd: str = "fwd5") -> list[dict]:
    """逐日 Spearman IC.

    frame: 含 date, symbol, factor, fwd 列的 DataFrame.
    Returns: [{date, n, ic, p}, ...]
    """
    out = []
    for d, g in frame.groupby("date", sort=False):
        s = g[[factor, fwd]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) < 30:
            continue
        ic, p = spearmanr(s[factor], s[fwd])
        out.append({"date": str(d), "n": int(len(s)), "ic": float(ic), "p": float(p)})
    return out


def ic_summary(ic_records: list[dict]) -> dict:
    """从 daily_ic 输出汇总 IC 统计.

    Returns: {n_days, ic_mean, ic_std, icir, t_stat, win_rate}
    """
    ics = np.array([r["ic"] for r in ic_records], dtype=float)
    n = len(ics)
    if n == 0:
        return {"n_days": 0, "ic_mean": None, "ic_std": None,
                "icir": None, "t_stat": None, "win_rate": None}
    mean_ic = float(ics.mean())
    std_ic = float(ics.std(ddof=1)) if n > 1 else 0.0
    icir = mean_ic / std_ic if std_ic > 1e-12 else 0.0
    t_stat = mean_ic / (std_ic / math.sqrt(n)) if std_ic > 1e-12 else 0.0
    win_rate = float((ics > 0).mean())
    return {
        "n_days": n,
        "ic_mean": round(mean_ic, 4),
        "ic_std": round(std_ic, 4),
        "icir": round(icir, 4),
        "t_stat": round(t_stat, 4),
        "win_rate": round(win_rate, 4),
    }


# ===================================================================
# 3.1 滚动 IC（每10个交易日采样一个再平衡日）
# ===================================================================

def rolling_ic(frame: pd.DataFrame, factor: str, fwd: str = "fwd5",
               window: int = 60, rebalance_freq: int = 10) -> list[dict]:
    """滚动窗口 IC, 每 rebalance_freq 个交易日采样一次再平衡日.

    在每个再平衡日, 用过去 window 个交易日的 IC 序列计算:
    - 均值IC / ICIR / t值 / 胜率

    Returns: [{rebalance_date, window_start, window_end, n_days, ic_mean, icir, t_stat, win_rate}, ...]
    """
    dates = sorted(frame["date"].unique())
    if len(dates) < window + rebalance_freq:
        return []

    results = []
    # 从 window 位置开始, 每 rebalance_freq 天采样
    for i in range(window, len(dates), rebalance_freq):
        w_end = dates[i]
        w_start = dates[i - window]
        sub = frame[(frame["date"] >= w_start) & (frame["date"] <= w_end)]
        ic_rows = daily_ic(sub, factor, fwd)
        if len(ic_rows) < 10:
            continue
        summary = ic_summary(ic_rows)
        results.append({
            "rebalance_date": w_end,
            "window_start": w_start,
            "window_end": w_end,
            **summary,
        })
    return results


# ===================================================================
# 3.1 多空价差 (Top 20% vs Bottom 20%)
# ===================================================================

def calc_long_short_spread(frame: pd.DataFrame, factor: str, fwd: str = "fwd5",
                           top_pct: float = 0.2) -> dict:
    """计算多空价差: Top 20% - Bottom 20% 的未来收益差.

    按日截面分组, 组内等权平均, 再按期平均.
    年化 = 日平均 * 242.

    Returns: {n_days, long_avg, short_avg, spread_avg, spread_annual, spread_std, t_stat}
    """
    g = frame[["date", factor, fwd]].replace([np.inf, -np.inf], np.nan).dropna().copy()
    spreads = []
    for d, gg in g.groupby("date", sort=False):
        n = len(gg)
        if n < 50:
            continue
        sorted_idx = gg[factor].argsort()
        n_top = max(1, int(n * top_pct))
        n_bot = max(1, int(n * top_pct))
        top_mean = gg[fwd].iloc[sorted_idx[-n_top:]].mean()
        bot_mean = gg[fwd].iloc[sorted_idx[:n_bot]].mean()
        spreads.append(float(top_mean - bot_mean))

    if len(spreads) == 0:
        return {"n_days": 0, "long_avg": None, "short_avg": None,
                "spread_avg": None, "spread_annual": None,
                "spread_std": None, "t_stat": None}

    arr = np.array(spreads)
    spread_avg = float(arr.mean())
    spread_std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    t_stat = spread_avg / (spread_std / math.sqrt(len(arr))) if spread_std > 1e-12 else 0.0

    # 重新计算 long_avg / short_avg (避免 listcomp 作用域问题)
    long_vals, short_vals = [], []
    for d, gg in g.groupby("date", sort=False):
        n = len(gg)
        if n < 50:
            continue
        si = gg[factor].argsort()  # 按因子值排序
        n_top = max(1, int(n * top_pct))
        long_vals.append(float(gg[fwd].iloc[si[-n_top:]].mean()))
        short_vals.append(float(gg[fwd].iloc[si[:n_top]].mean()))

    return {
        "n_days": len(spreads),
        "long_avg": round(float(np.mean(long_vals)), 5) if long_vals else None,
        "short_avg": round(float(np.mean(short_vals)), 5) if short_vals else None,
        "spread_avg": round(spread_avg, 5),
        "spread_annual": round(spread_avg * 242, 4),
        "spread_std": round(spread_std, 5),
        "t_stat": round(t_stat, 4),
    }


# ===================================================================
# 3.1 IC 衰减（多持有期）
# ===================================================================

def calc_ic_decay(frame: pd.DataFrame, factor: str,
                  horizons: list[int] | None = None) -> list[dict]:
    """计算因子在不同持有期下的 IC 及衰减.

    frame 须包含 fwd5, fwd10, fwd20, fwd30 等列.
    horizons: 默认 [5, 10, 20, 30]

    Returns: [{horizon, ic_mean, icir, win_rate, n_days}, ...]
    """
    if horizons is None:
        horizons = [5, 10, 20, 30]
    results = []
    for h in horizons:
        fwd_col = f"fwd{h}"
        if fwd_col not in frame.columns:
            continue
        ic_rows = daily_ic(frame, factor, fwd=fwd_col)
        if len(ic_rows) < 5:
            continue
        s = ic_summary(ic_rows)
        s["horizon"] = h
        results.append(s)
    return results


# ===================================================================
# 3.1 换手率（分组调仓的日均换手）
# ===================================================================

def calc_turnover(frame: pd.DataFrame, factor: str, n_groups: int = 5,
                  top_pct: float = 0.2) -> dict:
    """计算因子分组调仓的日均换手率.

    模拟多空组合 (Top 20% 多头, Bottom 20% 空头),
    每期按因子值重新分组, 计算与前一期相比的成员变动比例.

    Returns: {n_periods, long_turnover, short_turnover, avg_turnover}
    """
    g = frame[["date", "symbol", factor]].replace([np.inf, -np.inf], np.nan).dropna().copy()
    dates = sorted(g["date"].unique())
    if len(dates) < 2:
        return {"n_periods": 0, "long_turnover": None, "short_turnover": None, "avg_turnover": None}

    long_turnovers = []
    short_turnovers = []
    prev_long: set = set()
    prev_short: set = set()

    for i, d in enumerate(dates):
        day = g[g["date"] == d]
        n = len(day)
        if n < 50:
            continue
        sorted_idx = day[factor].argsort()
        n_top = max(1, int(n * top_pct))
        n_bot = max(1, int(n * top_pct))
        curr_long = set(day["symbol"].iloc[sorted_idx[-n_top:]])
        curr_short = set(day["symbol"].iloc[sorted_idx[:n_bot]])

        if i > 0 and prev_long and prev_short:
            if len(curr_long | prev_long) > 0:
                l_t = 1.0 - len(curr_long & prev_long) / len(curr_long | prev_long)
                long_turnovers.append(l_t)
            if len(curr_short | prev_short) > 0:
                s_t = 1.0 - len(curr_short & prev_short) / len(curr_short | prev_short)
                short_turnovers.append(s_t)
        prev_long = curr_long
        prev_short = curr_short

    if not long_turnovers:
        return {"n_periods": 0, "long_turnover": None, "short_turnover": None, "avg_turnover": None}

    lt = float(np.mean(long_turnovers))
    st = float(np.mean(short_turnovers))
    return {
        "n_periods": len(long_turnovers),
        "long_turnover": round(lt, 4),
        "short_turnover": round(st, 4),
        "avg_turnover": round((lt + st) / 2, 4),
    }


# ===================================================================
# 3.1 综合评估（单因子完整报告）
# ===================================================================

def evaluate(frame: pd.DataFrame, factor: str, fwd: str = "fwd5",
             gates: dict | None = None) -> dict:
    """单因子综合评估 (3.1 指标体系).

    包含: 逐日IC, ICIR, t值, 胜率, 五分组, 多空价差, 换手率, IC衰减.

    Returns:
        dict with all evaluation metrics.
    """
    gates = gates or {"ic_mean": 0.02, "icir": 0.5, "t_stat": 1.0, "win_rate": 0.60, "mono": True}

    # 1) 逐日 IC
    ic_rows = daily_ic(frame, factor, fwd)
    ic_s = ic_summary(ic_rows)

    # 2) 五分组
    g = frame[["date", factor, fwd]].replace([np.inf, -np.inf], np.nan).dropna().copy()
    per = []
    for d, gg in g.groupby("date", sort=False):
        if len(gg) < 50:
            continue
        try:
            q = pd.qcut(gg[factor], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        per.append(gg[fwd].groupby(q).mean().reindex(range(5)))
    tab = pd.concat(per, axis=1).T if per else pd.DataFrame()
    if len(tab):
        qmean = tab.mean(axis=0)
        q_series = [round(float(qmean.get(i, np.nan)), 5) if pd.notna(qmean.get(i)) else None for i in range(5)]
        spread_q = float(qmean.get(4, np.nan) - qmean.get(0, np.nan)) if len(qmean) >= 5 else None
        monotone = bool(len(qmean) == 5 and all(
            qmean.iloc[i] <= qmean.iloc[i + 1] + 1e-9 for i in range(4)))
    else:
        q_series = [None] * 5
        spread_q = None
        monotone = False

    # 3) 多空价差
    ls = calc_long_short_spread(frame, factor, fwd)

    # 4) IC 衰减
    decay = calc_ic_decay(frame, factor)

    # 5) 换手率
    to = calc_turnover(frame, factor)

    report = {
        "factor": factor,
        "n_days": ic_s["n_days"],
        "ic_mean": ic_s["ic_mean"],
        "ic_std": ic_s["ic_std"],
        "icir": ic_s["icir"],
        "t_stat": ic_s["t_stat"],
        "win_rate": ic_s["win_rate"],
        "q_means": q_series,
        "spread_top_minus_bottom": spread_q,
        "monotone": monotone,
        "long_short": ls,
        "ic_decay": decay,
        "turnover": to,
        "gates": {
            "pass_ic_mean": bool(ic_s["ic_mean"] and ic_s["ic_mean"] > gates["ic_mean"]) if ic_s["ic_mean"] else False,
            "pass_icir": bool(ic_s["icir"] and ic_s["icir"] > gates["icir"]) if ic_s["icir"] else False,
            "pass_t_stat": bool(ic_s["t_stat"] and abs(ic_s["t_stat"]) >= gates["t_stat"]) if ic_s["t_stat"] else False,
            "pass_win_rate": bool(ic_s["win_rate"] and ic_s["win_rate"] >= gates["win_rate"]) if ic_s["win_rate"] else False,
            "pass_spread": bool(ls.get("spread_avg") and ls["spread_avg"] > 0) if ls.get("spread_avg") else False,
            "pass_mono": monotone if gates.get("mono") else None,
            "pass_turnover": bool(to.get("avg_turnover") and to["avg_turnover"] < 0.50) if to.get("avg_turnover") else None,
        },
    }
    # 准入判定
    g = report["gates"]
    n_pass = sum(1 for k, v in g.items() if v is True)
    report["verdict"] = "ADOPT" if n_pass >= 4 else "WATCH" if n_pass >= 2 else "REJECT"
    report["gates_summary"] = f"pass {n_pass}/7 (IC>{gates['ic_mean']} ICIR>{gates['icir']} |t|>={gates['t_stat']} win>={gates['win_rate']} spread>0 mono turnover<0.5)"
    return report