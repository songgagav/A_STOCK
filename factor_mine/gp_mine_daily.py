# -*- coding: utf-8 -*-
# ============================================================
# gp_mine_daily.py -- 日频 GP 因子自动挖掘引擎
#
# 基于 gplearn + 自定义算子库(时序算子+截面算子) 进化可读因子公式.
# 以 RankIC 为优化目标, 输出 top-k 候选因子表达式.
#
# 算子库:
#   时序算子(滚动窗口): ts_mean, ts_std, ts_rank, ts_delta, ts_zscore, ts_max, ts_min
#   截面算子:            cs_rank, cs_demean, cs_zscore
#   基本算子:            add, sub, mul, div, neg, abs, sqrt, log, inv
#
# 工作流:
#   1. 加载日频截面数据 (factor_library 24 因子 + 原始特征)
#   2. 用预计算特征矩阵作为 X (每列 = 某因子在指定窗口的统计量)
#   3. gplearn 进化 -> 输出可读因子表达式
#   4. 用 ai_factor_lab 对候选做 OOS 验证
#
# CLI:
#   python factor_mine/gp_mine_daily.py run [--days 60] [--pop 200] [--gen 10]
#   python factor_mine/gp_mine_daily.py smoke              # 冒烟测试
#   python factor_mine/gp_mine_daily.py list                # 列出可用特征
# ============================================================

from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gp_sklearn_patch as _patch  # noqa: E402
_patch.apply()

FC = os.path.join(_BASE, "data", "factor_mine")

# 默认特征列 (来自 factor_library 的24个因子)
FEATURE_COLS = [
    "ret_5", "ret_10", "ret_20", "ret_60",
    "mom_5_20", "mom_10_60",
    "rsi_14", "kdj_k", "kdj_d", "kdj_j",
    "hist_vol_10", "hist_vol_20", "hist_vol_60",
    "atr_14", "max_drawdown_20",
    "volume_change_5", "turnover", "volume_ratio",
    "ep", "bp", "roe", "roe_yy_chg", "rev_yoy", "np_yoy",
]

# 滚动窗口配置: 为每个特征生成的滚动统计
ROLLING_WINDOWS = [5, 10, 20]
ROLLING_OPS = ["mean", "std", "rank"]


# ===================================================================
# 算子库 (依法注册到 gplearn)
# ===================================================================

def _cs_rank_1d(x: np.ndarray) -> np.ndarray:
    """截面排序 rank (0~1)."""
    from scipy.stats import rankdata
    r = rankdata(x)
    return r / max(len(r), 1)


def _cs_demean_1d(x: np.ndarray) -> np.ndarray:
    """截面去均值."""
    return x - np.nanmean(x)


def _cs_zscore_1d(x: np.ndarray) -> np.ndarray:
    """截面 z-score."""
    mu = np.nanmean(x)
    sd = np.nanstd(x, ddof=1)
    if sd <= 1e-12:
        return np.zeros_like(x)
    return (x - mu) / sd


def _safe_div(a, b):
    """安全除法: 被零除返回 1.0 (gplearn closure 要求)."""
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(np.abs(b) > 0.001, np.divide(a, b), 1.0)


def _safe_log(x):
    """安全对数: 零/负数返回 0.0 (gplearn closure 要求)."""
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(np.abs(x) > 0.001, np.log(np.abs(x)), 0.0)


def _safe_sqrt(x):
    """安全平方根: 负数取绝对值 (gplearn closure 要求)."""
    return np.sqrt(np.abs(x))


def _safe_inv(x):
    """安全倒数: 零返回 0.0 (gplearn closure 要求)."""
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(np.abs(x) > 0.001, 1.0 / x, 0.0)


# gplearn 函数注册格式: (name, func, arity)
# arity=1 一元, arity=2 二元
GP_FUNCTIONS = [
    # 基本算子
    ("add", lambda a, b: a + b, 2),
    ("sub", lambda a, b: a - b, 2),
    ("mul", lambda a, b: a * b, 2),
    ("div", _safe_div, 2),
    ("neg", lambda a: -a, 1),
    ("abs", np.abs, 1),
    ("sqrt", _safe_sqrt, 1),
    ("log", _safe_log, 1),
    ("inv", _safe_inv, 1),
    # 截面算子
    ("cs_rank", _cs_rank_1d, 1),
    ("cs_demean", _cs_demean_1d, 1),
    ("cs_zscore", _cs_zscore_1d, 1),
    # 非线性变换
    ("square", lambda a: a ** 2, 1),
    ("cube", lambda a: a ** 3, 1),
    ("sign", np.sign, 1),
    ("clip", lambda a: np.clip(a, -3, 3), 1),
]

# 函数名列表 (用于 gplearn function_set)
FUNCTION_NAMES = [f[0] for f in GP_FUNCTIONS]


# ===================================================================
# 数据准备: 构建日频特征矩阵
# ===================================================================

def _build_panel(end_day: str | None = None,
                 days: int = 240,
                 add_rolling: bool = True) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """构建日频面板数据 (批量加载优化版).

    Returns:
        X: (n_samples, n_features) 特征矩阵
        y: (n_samples,) 未来收益
        masks: 各列的有效性标记
        feature_names: 特征列名列表
    """
    from factor_library import _fetch_bars, _calendar, _active_szsh, FACTOR_REGISTRY
    from factor_library import (
        _ret_N, _mom_fast_slow, _rsi, _kdj, _hist_vol, _atr,
        _max_drawdown, _volume_change, _volume_ratio,
        FUNDAMENTAL_FACTORS, MAX_HISTORY_NEEDED,
    )

    cal = _calendar()
    if end_day:
        end_idx = cal.index(end_day) if end_day in cal else -1
    else:
        end_idx = -1
    if end_idx < 0:
        end_idx = len(cal) - 1

    # 多拉 MAX_HISTORY_NEEDED+5 天给因子计算
    data_start = max(0, end_idx - days - MAX_HISTORY_NEEDED - 5)
    eval_start = max(0, end_idx - days)
    eval_days = cal[eval_start:end_idx + 1]
    full_start = cal[data_start]

    # 一次性加载所有 bars
    bars = _fetch_bars(full_start, cal[end_idx])
    if bars.empty:
        return np.array([]), np.array([]), np.array([]), []

    # 逐日计算因子值 (用同一份 bars 数据, 避免重复查库)
    ts_factors = [f for f in FEATURE_COLS if f not in FUNDAMENTAL_FACTORS]
    fd_factors = [f for f in FEATURE_COLS if f in FUNDAMENTAL_FACTORS]

    # 预加载基本面因子 (仅需最新值)
    fd_cache = {}
    if fd_factors:
        _load_fd_cache(end_idx, cal, fd_factors, fd_cache)

    all_rows = []
    for d in eval_days:
        d_idx = cal.index(d) if d in cal else -1
        if d_idx < 0:
            continue
        # 只取到 d 为止的 bars
        day_bars = bars[bars["d"] <= pd.Timestamp(d).date()].copy()
        if day_bars.empty:
            continue

        row_map = {}
        for sym, g in day_bars.groupby("symbol", sort=False):
            g = g.reset_index(drop=True)
            if len(g) < 5:
                continue
            out = {}
            for fname in ts_factors:
                meta = FACTOR_REGISTRY.get(fname, {})
                cat = meta.get("category", "")
                val = np.nan
                try:
                    if cat == "momentum":
                        if fname.startswith("ret_"):
                            val = _ret_N(g, meta["window"])
                        elif fname.startswith("mom_"):
                            val = _mom_fast_slow(g, meta["fast"], meta["slow"])
                        elif fname == "rsi_14":
                            val = _rsi(g, 14)
                        elif fname.startswith("kdj_"):
                            k, dd, j = _kdj(g, 9)
                            val = {"kdj_k": k, "kdj_d": dd, "kdj_j": j}.get(fname, np.nan)
                    elif cat == "volatility":
                        if fname.startswith("hist_vol_"):
                            val = _hist_vol(g, meta["window"])
                        elif fname == "atr_14":
                            val = _atr(g, 14)
                        elif fname == "max_drawdown_20":
                            val = _max_drawdown(g, 20)
                    elif cat == "volume":
                        if fname == "volume_change_5":
                            val = _volume_change(g, 5)
                        elif fname == "turnover":
                            val = float(g["turnover"].iloc[-1]) if pd.notna(g["turnover"].iloc[-1]) else np.nan
                        elif fname == "volume_ratio":
                            val = _volume_ratio(g, 5)
                except Exception:
                    val = np.nan
                if np.isfinite(val):
                    out[fname] = val
            # 基本面因子
            for fname in fd_factors:
                fd_val = fd_cache.get(d, {}).get(sym, {}).get(fname, np.nan)
                if np.isfinite(fd_val):
                    out[fname] = fd_val
            if out:
                out["symbol"] = sym
                out["day"] = d
                row_map[sym] = out
        all_rows.extend(row_map.values())

    if not all_rows:
        return np.array([]), np.array([]), np.array([]), []

    panel = pd.DataFrame(all_rows)
    avail = [c for c in FEATURE_COLS if c in panel.columns]
    panel = panel[["symbol", "day"] + avail].dropna(subset=avail, how="all").reset_index(drop=True)

    feature_names = list(avail)
    if add_rolling and len(panel) > 100:
        rolling_feats = _add_rolling_features(panel, avail)
        for name, series in rolling_feats.items():
            panel[name] = series
            feature_names.append(name)

    panel = panel.sort_values(["symbol", "day"]).reset_index(drop=True)

    fwd_ret = _compute_fwd_returns(panel)
    panel = panel.merge(fwd_ret, on=["symbol", "day"], how="left")
    panel = panel.dropna(subset=feature_names + ["fwd5"])
    if len(panel) < 200:
        return np.array([]), np.array([]), np.array([]), []

    X = panel[feature_names].values.astype(float)
    y = panel["fwd5"].values.astype(float)
    X = np.clip(X, -8, 8)
    lo, hi = np.nanpercentile(y, [1, 99])
    y = np.clip(y, lo, hi)
    months = panel["day"].astype(str).str[:7].values

    return X, y, months, feature_names


def _load_fd_cache(end_idx: int, cal: list[str], fd_factors: list[str],
                   fd_cache: dict[str, dict]):
    """预加载基本面因子到缓存 {day: {symbol: {factor: value}}}."""
    from factor_library import _sql, _active_szsh, _avail_date
    import pandas as pd
    active = _active_szsh()

    D = cal[end_idx]
    fin = _sql("SELECT CAST(ts AS DATE) ts, symbol, roe, rev_yoy, np_yoy "
               "FROM financials")
    fin["symbol"] = fin["symbol"].astype(str).str.zfill(6)
    fin["ts"] = pd.to_datetime(fin["ts"])
    fin["avail"] = fin["ts"].map(_avail_date)
    fin = fin[fin["avail"] <= pd.Timestamp(D)].copy()
    fin = fin.sort_values("ts").drop_duplicates(["symbol", "ts"], keep="last")
    fin["kk"] = fin["ts"].dt.year * 4 + (fin["ts"].dt.month // 3 - 1)
    fin["kk_prev"] = fin["kk"] - 4
    lag = fin[["symbol", "kk", "roe"]].rename(columns={"kk": "kk_prev", "roe": "roe_yy_ago"})
    fin = fin.merge(lag, on=["symbol", "kk_prev"], how="left", suffixes=("", "_lag"))
    fin["roe_yy_chg"] = fin["roe"] - fin["roe_yy_ago"]
    fin = fin.sort_values(["symbol", "avail", "ts"]).drop_duplicates("symbol", keep="last")
    fd_map = fin.set_index("symbol")[["roe", "roe_yy_chg", "rev_yoy", "np_yoy"]].to_dict("index")

    val = _sql(f"SELECT CAST(ts AS DATE) d, symbol, pe_ttm, pb "
               f"FROM valuation WHERE CAST(ts AS DATE) <= DATE '{D}'")
    val["symbol"] = val["symbol"].astype(str).str.zfill(6)
    val["d"] = pd.to_datetime(val["d"])
    val = val.sort_values("d").drop_duplicates("symbol", keep="last")
    val_map = val.set_index("symbol")[["pe_ttm", "pb"]].to_dict("index")

    # 构建每日基本面快照 (基本面在季度内不变, 所以对所有 eval day 用同一份)
    for d in cal[max(0, end_idx - 240):end_idx + 1]:
        day_cache = {}
        day_bars = _sql(f"SELECT symbol FROM daily_bars "
                        f"WHERE CAST(ts AS DATE)=DATE '{d}'")
        day_bars["symbol"] = day_bars["symbol"].astype(str).str.zfill(6)
        day_bars = day_bars[day_bars["symbol"].isin(active)].copy()
        for _, r in day_bars.iterrows():
            sym = r["symbol"]
            entry = {}
            fv = fd_map.get(sym, {})
            vv = val_map.get(sym, {})
            if "ep" in fd_factors:
                pe = vv.get("pe_ttm")
                entry["ep"] = 1.0 / pe if pe and pe > 0 else np.nan
            if "bp" in fd_factors:
                pb = vv.get("pb")
                entry["bp"] = 1.0 / pb if pb and pb > 0 else np.nan
            if "roe" in fd_factors:
                entry["roe"] = fv.get("roe", np.nan)
            if "roe_yy_chg" in fd_factors:
                entry["roe_yy_chg"] = fv.get("roe_yy_chg", np.nan)
            if "rev_yoy" in fd_factors:
                entry["rev_yoy"] = fv.get("rev_yoy", np.nan)
            if "np_yoy" in fd_factors:
                entry["np_yoy"] = fv.get("np_yoy", np.nan)
            if entry:
                day_cache[sym] = entry
        fd_cache[d] = day_cache


def _add_rolling_features(panel: pd.DataFrame, base_cols: list[str]) -> dict:
    """为每个特征添加滚动窗口统计量。

    Returns: {feature_name: pd.Series}
    """
    out = {}
    # 对每个 symbol 计算滚动统计
    for sym, g in panel.groupby("symbol", sort=False):
        g = g.sort_values("day").reset_index(drop=True)
        idx = g.index
        for col in base_cols:
            vals = g[col].values
            for w in ROLLING_WINDOWS:
                for op in ROLLING_OPS:
                    name = f"{col}_ts_{op}_{w}"
                    if op == "mean":
                        result = pd.Series(np.full(len(vals), np.nan),
                                           index=idx, name=name)
                        if len(vals) > w:
                            rm = pd.Series(vals).rolling(w, min_periods=w // 2 + 1).mean()
                            result.iloc[:] = rm.values
                        out.setdefault(name, []).append(result)
                    elif op == "std":
                        result = pd.Series(np.full(len(vals), np.nan),
                                           index=idx, name=name)
                        if len(vals) > w:
                            rs = pd.Series(vals).rolling(w, min_periods=w // 2 + 1).std(ddof=1)
                            result.iloc[:] = rs.values
                        out.setdefault(name, []).append(result)
                    elif op == "rank":
                        result = pd.Series(np.full(len(vals), np.nan),
                                           index=idx, name=name)
                        if len(vals) > w:
                            rr = pd.Series(vals).rolling(w, min_periods=w // 2 + 1).apply(
                                lambda x: _cs_rank_1d(x)[-1] if len(x) > 1 else np.nan, raw=True)
                            result.iloc[:] = rr.values
                        out.setdefault(name, []).append(result)
    # 拼接
    result = {}
    for name, series_list in out.items():
        combined = pd.concat(series_list).sort_index()
        result[name] = combined
    return result


def _compute_fwd_returns(panel: pd.DataFrame) -> pd.DataFrame:
    """计算未来5日收益。"""
    from factor_library import _sql
    days = panel["day"].unique()
    if len(days) == 0:
        return pd.DataFrame(columns=["symbol", "day", "fwd5"])

    start = min(days)
    # 多拉 horizon+5 天
    from h5i_bar_store import H5iBarStore
    cal = H5iBarStore().trading_days()
    last_idx = cal.index(max(days)) if max(days) in cal else -1
    hi_idx = min(len(cal) - 1, last_idx + 10)
    hi_date = cal[hi_idx]

    bars = _sql(
        f"SELECT CAST(ts AS DATE) d, symbol, change_pct "
        f"FROM daily_bars WHERE CAST(ts AS DATE) >= DATE '{start}' "
        f"AND CAST(ts AS DATE) <= DATE '{hi_date}'")
    bars["symbol"] = bars["symbol"].astype(str).str.zfill(6)
    bars["d"] = pd.to_datetime(bars["d"])
    bars = bars.drop_duplicates(["symbol", "d"], keep="last").sort_values(["symbol", "d"])
    bars["r1"] = 1.0 + bars["change_pct"].fillna(0) / 100.0
    g = bars.groupby("symbol", sort=False)["r1"]
    cump = g.cumprod()
    fwd5 = g.cumprod().groupby(bars["symbol"], sort=False).shift(-5) / cump - 1.0
    bars["fwd5"] = np.where(np.isfinite(fwd5), fwd5, np.nan)

    result = bars[bars["d"].isin(pd.to_datetime(list(days)))].copy()
    result["day"] = result["d"].astype(str)
    return result[["symbol", "day", "fwd5"]]


# ===================================================================
# 适应度函数: 逐月 RankIC 均值
# ===================================================================

def _monthly_rankic_metric(y_true, y_pred, months):
    """逐月 RankIC 均值 (越大越好)."""
    y_true = np.asarray(y_true, float).ravel()
    y_pred = np.asarray(y_pred, float).ravel()
    months = np.asarray(months, object).ravel()

    if len(y_true) < 200 or len(y_true) != len(y_pred):
        return -1.0

    df = pd.DataFrame({"y": y_true, "p": y_pred, "m": months})
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    if len(df) < 120 or df["p"].nunique() < 10 or df["y"].nunique() < 10:
        return -1.0

    ics = df.groupby("m").apply(
        lambda g: g["p"].corr(g["y"], method="spearman") if len(g) > 30 else np.nan,
        include_groups=False).dropna()
    if len(ics) < 3:
        return -1.0
    return float(ics.mean())


# ===================================================================
# 主挖掘流程
# ===================================================================

def run_mine(end_day: str | None = None,
             days: int = 240,
             pop: int = 200,
             gen: int = 10,
             parsimony: float = 0.01,
             add_rolling: bool = True,
             verbose: bool = True) -> dict:
    """运行日频 GP 因子挖掘.

    Returns:
        dict with keys: {built_at, params, n_features, n_rows, top_candidates, ...}
    """
    t0 = time.time()
    if verbose:
        print(f"[GP Daily] 构建面板: days={days} pop={pop} gen={gen}")

    X, y, months, feat_names = _build_panel(end_day, days, add_rolling=add_rolling)
    if len(X) == 0:
        return {"ok": False, "error": "面板数据为空"}

    if verbose:
        print(f"  面板: {X.shape[0]} 行 × {X.shape[1]} 列 ({len(feat_names)} 个特征)")
        print(f"  月份: {np.unique(months).size} 个")

    from gplearn.genetic import SymbolicRegressor
    from gplearn.fitness import make_fitness

    # 封装适应度
    _months = months.copy()

    def _metric_wrapper(y_t, y_p, w):
        return _monthly_rankic_metric(y_t, y_p, _months)

    fit = make_fitness(function=_metric_wrapper, greater_is_better=True, wrap=False)

    # 构建 function_set
    from gplearn.functions import make_function
    func_set = []
    for name, fn, arity in GP_FUNCTIONS:
        func_set.append(make_function(function=fn, name=name, arity=arity))

    est = SymbolicRegressor(
        population_size=pop,
        generations=gen,
        tournament_size=min(20, pop // 10),
        function_set=func_set,
        parsimony_coefficient=parsimony,
        p_crossover=0.7,
        p_subtree_mutation=0.1,
        p_hoist_mutation=0.05,
        p_point_mutation=0.1,
        metric=fit,
        random_state=11,
        verbose=1 if verbose else 0,
        n_jobs=-1,  # 多核并行
        feature_names=feat_names,
    )

    # 处理 NaN/Inf
    X_clean = np.where(np.isfinite(X), X, 0.0)
    y_clean = np.where(np.isfinite(y), y, 0.0)

    if verbose:
        print(f"\n[GP Daily] 开始进化 ({gen} 代)...")
    est.fit(X_clean, y_clean)

    # 提取 top-k
    pool = list(est._programs[-1])
    pool.sort(key=lambda p: p.raw_fitness_, reverse=True)

    top = []
    for i, prog in enumerate(pool[:20]):
        expr_str = str(prog)
        # 翻译为可读表达式
        readable = _to_readable(expr_str, feat_names)
        top.append({
            "rank": i + 1,
            "program": expr_str,
            "readable": readable,
            "length": prog.length_,
            "fitness": round(float(prog.raw_fitness_), 5),
        })

    elapsed = round(time.time() - t0, 1)

    report = {
        "ok": True,
        "built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "framework": "gplearn (QuantGplearn 范式等价, 日频)",
        "params": {
            "population_size": pop,
            "generations": gen,
            "parsimony": parsimony,
            "n_features": len(feat_names),
            "n_rows": X.shape[0],
            "n_months": int(np.unique(months).size),
            "add_rolling_features": add_rolling,
        },
        "feature_names": feat_names,
        "metric": "逐月 RankIC 均值",
        "top_candidates": top,
        "elapsed_s": elapsed,
    }

    # 保存
    os.makedirs(FC, exist_ok=True)
    fp = os.path.join(FC, "gp_daily_mine_report.json")
    json.dump(report, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    if verbose:
        print(f"\n[GP Daily] 完成! 耗时 {elapsed}s")
        for r in top[:5]:
            print(f"  #{r['rank']:2d}  fitness={r['fitness']:.5f}  {r['readable'][:80]}")

    return report


def _to_readable(expr: str, feat_names: list[str]) -> str:
    """将 GP 表达式翻译为可读形式."""
    result = expr
    for i, name in enumerate(feat_names):
        result = result.replace(f"X{i}", name)
    return result


# ===================================================================
# 冒烟测试
# ===================================================================

def smoke_test():
    """最小冒烟测试: 小种群快速验证."""
    print("[GP Daily Smoke] 冒烟测试...")
    X = np.random.randn(500, 10)
    y = X[:, 0] * 0.5 + X[:, 1] * -0.3 + np.random.randn(500) * 0.1
    months = np.array(["2026-01"] * 250 + ["2026-02"] * 250)

    from gplearn.genetic import SymbolicRegressor
    from gplearn.fitness import make_fitness
    from gplearn.functions import make_function

    def _metric(y_t, y_p, w):
        return _monthly_rankic_metric(y_t, y_p, months)

    fit = make_fitness(function=_metric, greater_is_better=True, wrap=False)
    func_set = [make_function(function=fn, name=name, arity=arity)
                for name, fn, arity in GP_FUNCTIONS[:6]]  # 只用基本算子

    est = SymbolicRegressor(
        population_size=30, generations=2, tournament_size=5,
        function_set=func_set,
        parsimony_coefficient=0.01,
        metric=fit, random_state=11, verbose=0, n_jobs=1,
    )
    est.fit(X, y)
    pool = list(est._programs[-1])
    pool.sort(key=lambda p: p.raw_fitness_, reverse=True)
    print(f"  最佳 fitness: {pool[0].raw_fitness_:.5f}")
    print(f"  表达式: {pool[0]}")
    print("[GP Daily Smoke] OK")
    return True


# ===================================================================
# CLI
# ===================================================================

def main():
    if len(sys.argv) < 2:
        print("用法: python factor_mine/gp_mine_daily.py <command> [args]")
        print("命令: run, smoke, list")
        return 1

    cmd = sys.argv[1]

    if cmd == "smoke":
        smoke_test()
        return 0

    if cmd == "list":
        print(f"基础特征 ({len(FEATURE_COLS)} 个):")
        for c in FEATURE_COLS:
            print(f"  {c}")
        print(f"\n滚动窗口: {ROLLING_WINDOWS}")
        print(f"滚动算子: {ROLLING_OPS}")
        print(f"每个特征生成滚动特征: {len(ROLLING_WINDOWS) * len(ROLLING_OPS)} 个")
        print(f"\nGP 算子 ({len(GP_FUNCTIONS)} 个):")
        for name, _, arity in GP_FUNCTIONS:
            print(f"  {name} (arity={arity})")
        return 0

    if cmd == "run":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--end_day", default=None, help="截止日 YYYY-MM-DD")
        ap.add_argument("--days", type=int, default=240, help="回看天数")
        ap.add_argument("--pop", type=int, default=200, help="种群大小")
        ap.add_argument("--gen", type=int, default=10, help="进化代数")
        ap.add_argument("--parsimony", type=float, default=0.01, help="简约系数")
        ap.add_argument("--no-rolling", action="store_true", help="不使用滚动特征")
        args = ap.parse_args(sys.argv[2:])

        r = run_mine(
            end_day=args.end_day,
            days=args.days,
            pop=args.pop,
            gen=args.gen,
            parsimony=args.parsimony,
            add_rolling=not args.no_rolling,
        )
        if r.get("ok"):
            print(f"\n候选因子 ({len(r.get('top_candidates', []))} 个):")
            for c in r["top_candidates"][:10]:
                print(f"  #{c['rank']:2d}  IC={c['fitness']:.5f}  len={c['length']:2d}  "
                      f"{c['readable'][:100]}")
        else:
            print(f"错误: {r.get('error')}")
        return 0

    print(f"未知命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())