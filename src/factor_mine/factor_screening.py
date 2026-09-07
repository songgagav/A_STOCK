# -*- coding: utf-8 -*-
"""因子筛选管线 (3.2) + 因子去冗余 (3.3).

工作流:
  1. 对候选因子逐个执行 validate_factor → 筛选标准
  2. 对通过筛选的因子计算相关性矩阵
  3. 保留相关性 < 0.6 的因子子集, 每对高相关保留 IC 高的

CLI:
  python factor_mine/factor_screening.py screen <factor1, factor2, ...> [-d day] [-w 60]
  python factor_mine/factor_screening.py dedup <factor1, factor2, ...> [-d day]
  python factor_mine/factor_screening.py report candidates.json
"""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _BASE)

from factor_mine.evaluator import (
    evaluate, daily_ic, ic_summary, rolling_ic,
    calc_long_short_spread, calc_ic_decay, calc_turnover,
)


# ===================================================================
# 3.2 筛选流程
# ===================================================================

def validate_factor(
    frame: pd.DataFrame,
    factor: str,
    fwd: str = "fwd5",
    min_ic: float = 0.02,
    min_icir: float = 0.5,
    min_t_stat: float = 1.0,
    min_win_rate: float = 0.60,
    max_turnover: float = 0.50,
    min_spread_annual: float = 0.0,
) -> dict:
    """完整因子验证管线.

    参照 ai-factor-lab 的验证管线:
      1. 滚动 IC (每10个交易日采样)
      2. ICIR / t值 / 胜率
      3. 多空价差 (Top 20% vs Bottom 20%)
      4. IC 衰减 (5/10/20/30日持有期)
      5. 换手率

    Args:
        frame: DataFrame with date, symbol, factor, fwd, fwd10, fwd20, fwd30
        factor: 因子列名
        fwd: 主预测期列名
        min_ic, min_icir, min_t_stat, min_win_rate: 筛选门槛
        max_turnover: 最大换手率
        min_spread_annual: 最小年化多空价差

    Returns:
        dict with {factor, passed, gates, ic_mean, icir, t_stat, win_rate,
                    long_short, ic_decay, turnover, rolling_ic, ...}
    """
    # 1) 综合评估
    report = evaluate(frame, factor, fwd=fwd)

    # 2) 筛选判定
    gates = report.get("gates", {})
    ic_mean = report.get("ic_mean") or 0
    icir = report.get("icir") or 0
    t_stat = report.get("t_stat") or 0
    win_rate = report.get("win_rate") or 0
    ls = report.get("long_short", {})
    to = report.get("turnover", {})

    pass_ic = ic_mean >= min_ic
    pass_icir = icir >= min_icir
    pass_t = abs(t_stat) >= min_t_stat
    pass_win = win_rate >= min_win_rate
    pass_spread = (ls.get("spread_annual") or 0) >= min_spread_annual
    pass_turnover = (to.get("avg_turnover") or 1) <= max_turnover

    passed = pass_ic and pass_win and pass_t and pass_icir and pass_spread and pass_turnover

    # 3) 滚动 IC (每10个交易日)
    roll_ic = rolling_ic(frame, factor, fwd=fwd, window=60, rebalance_freq=10)

    # 4) IC 衰减
    decay = calc_ic_decay(frame, factor)

    result = {
        "factor": factor,
        "passed": passed,
        "ic_mean": ic_mean,
        "icir": icir,
        "t_stat": t_stat,
        "win_rate": win_rate,
        "gates": {
            "pass_ic": pass_ic,
            "pass_icir": pass_icir,
            "pass_t": pass_t,
            "pass_win": pass_win,
            "pass_spread": pass_spread,
            "pass_turnover": pass_turnover,
        },
        "long_short": ls,
        "turnover": to,
        "ic_decay": decay,
        "rolling_ic_windows": roll_ic,
        "n_rolling_windows": len(roll_ic),
    }
    return result


def batch_validate(
    frame: pd.DataFrame,
    factors: list[str],
    fwd: str = "fwd5",
    **kwargs,
) -> list[dict]:
    """批量验证多个因子.

    Returns: [{factor, passed, ...}, ...]
    """
    results = []
    for f in factors:
        if f not in frame.columns:
            continue
        r = validate_factor(frame, f, fwd=fwd, **kwargs)
        results.append(r)
    return results


# ===================================================================
# 3.3 因子去冗余
# ===================================================================

def factor_correlation(frame: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """计算因子间截面相关性矩阵 (Spearman).

    逐日计算截面 Spearman 相关, 再取时间平均.

    Returns: DataFrame (n_factors x n_factors) with mean Spearman correlation.
    """
    avail = [f for f in factors if f in frame.columns]
    if len(avail) < 2:
        return pd.DataFrame()

    # 逐日相关 -> 再平均
    corr_list = []
    for d, g in frame.groupby("date", sort=False):
        sub = g[avail].replace([np.inf, -np.inf], np.nan).dropna()
        if len(sub) < 50:
            continue
        corr_list.append(sub.rank().corr(method="spearman"))

    if not corr_list:
        return pd.DataFrame(index=avail, columns=avail, dtype=float)

    mean_corr = sum(corr_list) / len(corr_list)
    return mean_corr


def deduplicate_factors(
    frame: pd.DataFrame,
    factor_scores: dict[str, dict],
    threshold: float = 0.6,
    tie_breaker: str = "ic_mean",
) -> list[dict]:
    """因子去冗余: 保留相关性 < threshold 的因子子集.

    Args:
        frame: DataFrame with date, symbol, and factor columns
        factor_scores: {factor_name: {ic_mean, icir, ...}, ...} 来自 validate 结果
        threshold: 相关性阈值 (默认 0.6)
        tie_breaker: 高相关对保留谁的依据 (ic_mean 或 icir)

    Returns:
        [{factor, ic_mean, icir, retained, reason}, ...]
    """
    factors = list(factor_scores.keys())
    if len(factors) < 2:
        return [{"factor": f, "retained": True, "reason": "唯一因子"}
                for f in factors]

    corr = factor_correlation(frame, factors)
    if corr.empty:
        return [{"factor": f, "retained": True, "reason": "无法计算相关"}
                for f in factors]

    # 贪心选择: 按 tie_breaker 排序, 保留高分的, 剔除与其高相关的
    sorted_factors = sorted(factors, key=lambda f: factor_scores.get(f, {}).get(tie_breaker, 0) or 0, reverse=True)

    selected = []
    rejected = set()

    for f in sorted_factors:
        if f in rejected:
            continue
        selected.append(f)
        # 找到与 f 高相关的因子
        for g in sorted_factors:
            if g == f or g in rejected:
                continue
            c = corr.loc[f, g] if f in corr.index and g in corr.columns else 0
            if abs(c) >= threshold:
                rejected.add(g)

    results = []
    for f in factors:
        retained = f in selected
        if retained:
            reason = "纳入"
        else:
            # 找出与谁高相关
            for s in selected:
                c = corr.loc[f, s] if f in corr.index and s in corr.columns else 0
                if abs(c) >= threshold:
                    reason = f"与 {s} 相关={c:.2f} (>{threshold}), 被剔除"
                    break
            else:
                reason = "未知原因"
        results.append({
            "factor": f,
            "ic_mean": factor_scores.get(f, {}).get("ic_mean"),
            "icir": factor_scores.get(f, {}).get("icir"),
            "retained": retained,
            "reason": reason,
        })
    return results


# ===================================================================
# 报告生成
# ===================================================================

def screen_report(results: list[dict]) -> str:
    """生成可读的筛选报告文本."""
    lines = [
        "=" * 60,
        "因子筛选报告",
        "=" * 60,
        "",
    ]
    passed = [r for r in results if r.get("passed")]
    failed = [r for r in results if not r.get("passed")]

    lines.append(f"通过筛选: {len(passed)}/{len(results)}")
    lines.append("")

    if passed:
        lines.append("--- 通过 ---")
        for r in passed:
            gates = r.get("gates", {})
            n_pass = sum(1 for v in gates.values() if v is True)
            lines.append(f"  [{r['factor']}] IC={r.get('ic_mean', '?'):.4f} "
                         f"ICIR={r.get('icir', '?'):.2f} t={r.get('t_stat', '?'):.2f} "
                         f"win={r.get('win_rate', '?'):.2%} pass={n_pass}/6")
            ls = r.get("long_short", {})
            if ls.get("spread_annual"):
                lines.append(f"    多空年化={ls['spread_annual']:.2%}")
            to = r.get("turnover", {})
            if to.get("avg_turnover"):
                lines.append(f"    换手率={to['avg_turnover']:.2%}")
            decay = r.get("ic_decay", [])
            if decay:
                ic_str = "  ".join(f"H{d['horizon']}={d.get('ic_mean', '?'):.4f}" for d in decay)
                lines.append(f"    IC衰减: {ic_str}")
        lines.append("")

    if failed:
        lines.append("--- 未通过 ---")
        for r in failed:
            lines.append(f"  [{r['factor']}] IC={r.get('ic_mean', '?'):.4f} "
                         f"win={r.get('win_rate', '?'):.2%} 原因: "
                         + ", ".join(k for k, v in r.get("gates", {}).items() if not v))
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def dedup_report(results: list[dict]) -> str:
    """生成去冗余报告文本."""
    lines = [
        "=" * 60,
        "因子去冗余报告 (阈值 0.6)",
        "=" * 60,
        "",
    ]
    retained = [r for r in results if r.get("retained")]
    removed = [r for r in results if not r.get("retained")]

    lines.append(f"保留: {len(retained)}/{len(results)}")
    lines.append("")
    lines.append("--- 保留 ---")
    for r in retained:
        lines.append(f"  [{r['factor']}] IC={r.get('ic_mean', '?'):.4f} {r.get('reason', '')}")
    lines.append("")
    lines.append("--- 剔除 ---")
    for r in removed:
        lines.append(f"  [{r['factor']}] IC={r.get('ic_mean', '?'):.4f} {r.get('reason', '')}")
    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ===================================================================
# 数据准备助手 (从 ai_factor_lab 获取面板)
# ===================================================================

def _build_screen_frame(end_day: str | None = None,
                        days: int = 60,
                        multi_horizon: bool = True) -> pd.DataFrame:
    """构建带多持有期未来收益的评估面板 (同 gp_mine 批量加载优化).

    从因子库加载日频数据, 附加 fwd5/fwd10/fwd20/fwd30 未来收益.
    """
    from factor_library import _fetch_bars, _calendar, FACTOR_REGISTRY, _active_szsh
    from factor_library import _ret_N, _mom_fast_slow, _rsi, _kdj, _hist_vol, _atr
    from factor_library import _max_drawdown, _volume_change, _volume_ratio
    from factor_library import FUNDAMENTAL_FACTORS, MAX_HISTORY_NEEDED

    FEATURE_COLS = [
        "ret_5", "ret_10", "ret_20", "ret_60",
        "mom_5_20", "mom_10_60", "rsi_14",
        "kdj_k", "kdj_d", "kdj_j",
        "hist_vol_10", "hist_vol_20", "hist_vol_60",
        "atr_14", "max_drawdown_20",
        "volume_change_5", "turnover", "volume_ratio",
        "ep", "bp", "roe", "roe_yy_chg", "rev_yoy", "np_yoy",
    ]

    cal = _calendar()
    if end_day:
        end_idx = cal.index(end_day) if end_day in cal else -1
    else:
        end_idx = -1
    if end_idx < 0:
        end_idx = len(cal) - 1
    data_start = max(0, end_idx - days - MAX_HISTORY_NEEDED - 5)
    eval_start = max(0, end_idx - days)
    eval_days = cal[eval_start:end_idx + 1]

    # 一次性加载所有 bars
    bars = _fetch_bars(cal[data_start], cal[end_idx])
    if bars.empty:
        return pd.DataFrame()

    # 预计算多持有期未来收益, 用 dict 索引实现 O(1) 查找
    max_h = 30 if multi_horizon else 5
    hi_idx = min(len(cal) - 1, end_idx + max_h + 5)
    hi_date = cal[hi_idx]
    fwd_bars = _fetch_bars(cal[eval_start], hi_date)
    fwd_bars = fwd_bars.drop_duplicates(["symbol", "d"], keep="last").sort_values(["symbol", "d"])
    # 从 change_pct 直接计算 multi-horizon fwd return
    fwd_bars["r1"] = 1.0 + fwd_bars["change_pct"].fillna(0) / 100.0
    fwd_bars["cumprod"] = fwd_bars.groupby("symbol", sort=False)["r1"].cumprod()
    fwd_bars = fwd_bars.sort_values(["symbol", "d"]).reset_index(drop=True)

    for h in [5, 10, 20, 30]:
        fwd_bars[f"fwd{h}"] = fwd_bars.groupby("symbol", sort=False)["cumprod"].transform(
            lambda x: x.shift(-h) / x - 1.0).values

    # 构建 fwd 索引: {(d, symbol) -> {fwd5, fwd10, ...}}
    fwd_index = {}
    for _, row in fwd_bars.iterrows():
        key = (row["d"], row["symbol"])
        fwd_index[key] = {f"fwd{h}": row[f"fwd{h}"] for h in [5, 10, 20, 30]}

    # 所有时序列因子 (非基本面), 用同一份 bars 逐日计算
    ts_factors = [f for f in FEATURE_COLS if f not in FUNDAMENTAL_FACTORS]
    fd_factors = [f for f in FEATURE_COLS if f in FUNDAMENTAL_FACTORS]

    # 预加载基本面因子 (对于 eval_days 内不变)
    fd_cache = {}
    if fd_factors:
        _load_fd_cache(end_idx, cal, fd_factors, fd_cache)

    all_rows = []
    for d in eval_days:
        d_idx = cal.index(d) if d in cal else -1
        if d_idx < 0:
            continue
        day_bars = bars[bars["d"] <= pd.Timestamp(d).date()].copy()
        if day_bars.empty:
            continue
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
                            k, dd, jj = _kdj(g, 9)
                            val = {"kdj_k": k, "kdj_d": dd, "kdj_j": jj}.get(fname, np.nan)
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
                out["date"] = d
                # O(1) 字典查找 fwd 收益
                fwd_row = fwd_index.get((pd.Timestamp(d).date(), sym))
                if fwd_row:
                    for h in [5, 10, 20, 30]:
                        val = fwd_row.get(f"fwd{h}")
                        if val is not None and np.isfinite(val):
                            out[f"fwd{h}"] = float(val)
                all_rows.append(out)

    if not all_rows:
        return pd.DataFrame()
    return pd.DataFrame(all_rows)


def _load_fd_cache(end_idx: int, cal: list[str], fd_factors: list[str],
                   fd_cache: dict[str, dict]):
    """预加载基本面因子到缓存 {day: {symbol: {factor: value}}} (同 gp_mine)."""
    from factor_library import _sql, _active_szsh, _avail_date
    D = cal[end_idx]
    active = _active_szsh()
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


# ===================================================================
# CLI
# ===================================================================

def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python factor_mine/factor_screening.py screen <factors> [-d day] [-w 60]")
        print("  python factor_mine/factor_screening.py dedup <factors> [-d day]")
        print("  python factor_mine/factor_screening.py report candidates.json")
        return 1

    cmd = sys.argv[1]

    if cmd == "screen":
        if len(sys.argv) < 3:
            print("需指定因子列表, 逗号分隔")
            return 1
        factors = sys.argv[2].split(",")
        day = None
        window = 60
        if "-d" in sys.argv:
            day = sys.argv[sys.argv.index("-d") + 1]
        if "-w" in sys.argv:
            window = int(sys.argv[sys.argv.index("-w") + 1])
        print(f"构建面板 (days={window})...")
        frame = _build_screen_frame(day, days=window, multi_horizon=True)
        if frame.empty:
            print("面板为空")
            return 1
        print(f"面板: {len(frame)} 行, {len(frame['date'].unique())} 天")
        results = batch_validate(frame, factors)
        print(screen_report(results))
        return 0

    if cmd == "dedup":
        if len(sys.argv) < 3:
            print("需指定因子列表, 逗号分隔")
            return 1
        factors = sys.argv[2].split(",")
        day = None
        if "-d" in sys.argv:
            day = sys.argv[sys.argv.index("-d") + 1]
        print(f"构建面板...")
        frame = _build_screen_frame(day, days=60, multi_horizon=False)
        if frame.empty:
            print("面板为空")
            return 1
        print(f"面板: {len(frame)} 行, {len(frame['date'].unique())} 天")
        # 先验证
        results = batch_validate(frame, factors, fwd="fwd5")
        scores = {r["factor"]: r for r in results}
        dedup = deduplicate_factors(frame, scores, threshold=0.6)
        print(dedup_report(dedup))
        # 保存结果
        out_path = os.path.join(_BASE, "data", "factor_mine", "factor_dedup_report.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        json.dump({"validate": results, "dedup": dedup}, open(out_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2, default=str)
        print(f"\n报告已保存: {out_path}")
        return 0

    if cmd == "report":
        path = sys.argv[2] if len(sys.argv) > 2 else "candidates.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            print(screen_report(data))
        elif isinstance(data, dict):
            if "validate" in data:
                print(screen_report(data["validate"]))
            if "dedup" in data:
                print(dedup_report(data["dedup"]))
        return 0

    print(f"未知命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())