# ============================================================
# degradation.py -- 策略退化检测
#
# 输入: ArcticDB 历史 (perf_report + reward_curve + trade_records + factor_ic)
# 输出: 退化指标 + 一致性偏差 + 退化分级 + 增量学习样本生成
#
# 检测维度:
#   1) 收益退化:    rolling sharpe / sortino 衰减 vs 历史均值
#   2) 一致性偏差:  (a) 个股收益 vs 组合收益的相关系数下降
#                  (b) 因子 IC 与实际因子收益的同向率
#                  (c) 不同持有期 IC 的内部一致性
#   3) 活跃度衰减:  trade count / turnover / 持仓换手率下降
#   4) 分布漂移:    收益偏度/峰度突变 (Kolmogorov-Smirnov 简版)
#   5) 策略退化指数: 加权综合分 (0-100), 越低越差
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from typing import Any

import numpy as np
import pandas as pd

_LOG = logging.getLogger("degradation")

from config import DATA_DIR
from arctic_store import get_store
from spc import (
    IndicatorCfg, spc_check, DEFAULT_CFGS, cusum,
    P0, P1, P2, P3, OK,
)


# =============================================================================
# 数据加载
# =============================================================================
def _load_perf_series(days: int = 60) -> pd.DataFrame:
    """从 ArcticDB perf_report 取最近 N 日 metrics, 索引=day."""
    store = get_store()
    df = store.read_perf_reports(days=days)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def _load_reward_curve(days: int = 30) -> pd.DataFrame | None:
    """从 ArcticDB reward_curve 取最近 N 日 DRL 训练 reward 序列 (拼接多天)."""
    store = get_store()
    syms = sorted(store._lib("reward_curve").list_symbols(), reverse=True)[:days]
    if not syms:
        return None
    rows = []
    for s in syms:
        try:
            d = store.read_reward_curve(s)
            if d is not None and not d.empty:
                d["day"] = s
                rows.append(d)
        except Exception:
            continue
    return pd.concat(rows) if rows else None


def _load_trades(days: int = 30) -> pd.DataFrame:
    """从 ArcticDB trade_records 取最近 N 日成交."""
    store = get_store()
    cutoff = (dt.date.today() - dt.timedelta(days=days + 30)).isoformat()
    df = store.read_trades(date_range=(cutoff, None))
    if df is None or df.empty:
        return pd.DataFrame()
    return df


# =============================================================================
# 退化指标
# =============================================================================
def compute_consistency_deviation(trades: pd.DataFrame) -> dict:
    """一致性偏差: 个股 vs 组合的方向一致性.
    每个交易日, 比较每日个股涨跌方向 vs 组合涨跌幅方向, 计算一致率.
    """
    if trades is None or trades.empty:
        return {"ok": False, "reason": "无成交数据"}
    # trade_records 中按 day 聚合每只 symbol 当日已实现 pnl (近似用 price * qty * direction)
    if "direction" not in trades.columns or "qty" not in trades.columns:
        return {"ok": False, "reason": "成交字段缺失"}
    trades = trades.copy()
    if "pnl" not in trades.columns:
        # 估算: 多=正贡献, 空=负贡献, 英文/中文都处理
        def _sign(r):
            d = str(r["direction"]).strip().lower()
            if d in ("buy", "long", "多", "long_entry", "open"):
                return 1
            if d in ("sell", "short", "空", "short_entry", "close"):
                return -1
            return 0
        trades["pnl"] = trades.apply(
            lambda r: _sign(r) * float(r["qty"]),
            axis=1,
        )
    agg = trades.groupby("day").agg(
        trades=("pnl", "count"),
        signals=("pnl", lambda s: int((s > 0).sum()) - int((s < 0).sum())),
    ).reset_index()
    if agg.empty or (agg["trades"] == 0).all():
        return {"ok": False, "reason": "无聚合"}
    agg["consistency"] = agg["signals"].abs() / agg["trades"].replace(0, 1)
    last20 = agg.tail(20)["consistency"]
    return {
        "ok": True,
        "n_days": len(agg),
        "consistency_recent20_mean": float(last20.mean()) if len(last20) else 0.0,
        "consistency_recent20_std": float(last20.std()) if len(last20) > 1 else 0.0,
        "consistency_min_recent20": float(last20.min()) if len(last20) else 0.0,
        "history_mean": float(agg["consistency"].mean()),
        "degradation_score": float(1.0 - last20.mean()) if len(last20) else 0.0,
        "raw_tail": agg.tail(20).to_dict("records"),
    }


def compute_activity_decay(perf: pd.DataFrame) -> dict:
    """活跃度衰减: 持仓数 / 换手率."""
    if perf is None or perf.empty or "open_positions" not in perf.columns:
        return {"ok": False, "reason": "无持仓数据"}
    pos = perf["open_positions"].dropna().astype(int).tail(30)
    return {
        "ok": True,
        "positions_recent_mean": float(pos.mean()) if len(pos) else 0.0,
        "positions_recent_std": float(pos.std()) if len(pos) > 1 else 0.0,
        "positions_history_mean": float(perf["open_positions"].mean()),
        "decay_ratio": float(pos.mean() / perf["open_positions"].mean()) if perf["open_positions"].mean() else 1.0,
    }


def compute_distribution_shift(perf: pd.DataFrame) -> dict:
    """收益分布漂移: 偏度/峰度突变 (近 20 日 vs 前 40 日)."""
    if perf is None or perf.empty:
        return {"ok": False, "reason": "无收益序列"}
    # 用 total_return 序列或 daily return 近似
    df = perf.copy()
    if "total_return" in df.columns:
        df["daily_ret"] = df["total_return"].astype(float).pct_change().fillna(0)
    else:
        return {"ok": False, "reason": "缺 total_return 列"}
    s = df["daily_ret"].dropna()
    if len(s) < 10:
        return {"ok": False, "reason": f"样本不足 ({len(s)})"}
    recent = s.tail(20) if len(s) >= 20 else s
    history = s.head(max(1, len(s) - len(recent)))
    if len(history) < 3:
        return {"ok": False, "reason": "历史段过短"}
    recent_skew = float(recent.skew()) if len(recent) >= 3 else 0.0
    history_skew = float(history.skew()) if len(history) >= 3 else 0.0
    skew_drift = abs(recent_skew - history_skew)
    return {
        "ok": True,
        "skew_recent": recent_skew,
        "skew_history": history_skew,
        "skew_drift": skew_drift,
        "drift_score": float(min(1.0, skew_drift / 2.0)),
    }


def compute_reward_trend(reward_df: pd.DataFrame | None) -> dict:
    """DRL reward 序列趋势: 近 N 日 mean_reward 是否在衰减."""
    if reward_df is None or reward_df.empty:
        return {"ok": False, "reason": "无 DRL reward 数据"}
    if "reward" not in reward_df.columns:
        return {"ok": False, "reason": "缺 reward 列"}
    # 按 day 分组取 mean
    by_day = reward_df.groupby("day")["reward"].mean().tail(20)
    if len(by_day) < 3:
        return {"ok": False, "reason": f"DRL 天数不足 ({len(by_day)})"}
    # 线性回归斜率
    x = np.arange(len(by_day))
    y = by_day.values.astype(float)
    if np.std(x) == 0:
        slope = 0.0
    else:
        slope = float(np.polyfit(x, y, 1)[0])
    # 近期均值 vs 历史均值
    half = len(by_day) // 2
    recent_mean = float(y[half:].mean()) if half > 0 else 0.0
    history_mean = float(y[:half].mean()) if half > 0 else 0.0
    return {
        "ok": True,
        "n_days": len(by_day),
        "slope": slope,
        "recent_mean": recent_mean,
        "history_mean": history_mean,
        "degradation_ratio": float((history_mean - recent_mean) / (abs(history_mean) + 1e-6)),
    }


# =============================================================================
# 综合退化评分
# =============================================================================
def compute_strategy_degradation_index(perf: pd.DataFrame,
                                      trades: pd.DataFrame,
                                      reward_df: pd.DataFrame | None) -> dict:
    """策略退化指数 0-100 (越高越健康). 五个维度加权."""
    parts = []

    # 1) 收益 SPC (基于 perf 中的 total_return)
    if not perf.empty and "total_return" in perf.columns:
        ret_series = perf["total_return"].astype(float).tail(40)
        spc_ret = spc_check(ret_series, DEFAULT_CFGS["daily_return"])
        parts.append(("return_spc", spc_ret["level"], _score_from_level(spc_ret["level"])))

    # 2) 回撤 SPC (rolling 60d max_drawdown)
    if not perf.empty and "max_drawdown" in perf.columns:
        dd_series = perf["max_drawdown"].astype(float).tail(40)
        spc_dd = spc_check(dd_series, DEFAULT_CFGS["max_drawdown"])
        parts.append(("drawdown_spc", spc_dd["level"], _score_from_level(spc_dd["level"])))

    # 3) 一致性偏差
    cons = compute_consistency_deviation(trades)
    if cons.get("ok"):
        # deviation > 0.4 视为退化
        score = max(0.0, 1.0 - cons["degradation_score"])
        if score < 0.5:
            level = P1
        elif score < 0.7:
            level = P2
        else:
            level = OK
        parts.append(("consistency", level, score))

    # 4) 活跃度
    act = compute_activity_decay(perf)
    if act.get("ok"):
        # decay_ratio < 0.5 视为显著退化
        if act["decay_ratio"] < 0.5:
            level = P1
        elif act["decay_ratio"] < 0.7:
            level = P2
        else:
            level = OK
        parts.append(("activity", level, act["decay_ratio"]))

    # 5) Reward 趋势
    rwd = compute_reward_trend(reward_df)
    if rwd.get("ok"):
        # degradation_ratio > 0.5 视为显著退化 (reward 跌了一半)
        if rwd["degradation_ratio"] > 0.5:
            level = P1
        elif rwd["degradation_ratio"] > 0.2:
            level = P2
        else:
            level = OK
        parts.append(("reward_trend", level, 1.0 - rwd["degradation_ratio"]))

    # 综合分 (加权平均, P0=0, P1=25, P2=70, P3=85, OK=100)
    weights = {"return_spc": 0.3, "drawdown_spc": 0.25, "consistency": 0.15,
               "activity": 0.1, "reward_trend": 0.2}
    overall = 0.0
    total_w = 0.0
    for name, level, score in parts:
        w = weights.get(name, 0.1)
        # 优先级: 真实 SPC 等级压过局部 score
        ls = _score_from_level(level)
        overall += w * ls
        total_w += w
    overall /= total_w if total_w else 1.0
    # 最差等级
    worst_level = OK
    for _, level, _ in parts:
        if _level_rank(level) < _level_rank(worst_level):
            worst_level = level

    return {
        "overall_score": round(overall, 1),
        "worst_level": worst_level,
        "components": [
            {"name": n, "level": lv, "score": round(s, 4)} for n, lv, s in parts
        ],
        "consistency_detail": cons,
        "activity_detail": act,
        "reward_detail": rwd,
        "distribution_detail": compute_distribution_shift(perf),
    }


def _score_from_level(level: str) -> float:
    """等级转 0-100 健康分."""
    return {"P0": 0.0, "P1": 25.0, "P2": 70.0, "P3": 85.0, "OK": 100.0}.get(level, 100.0)


def _level_rank(level: str) -> int:
    """严重度排序: P0 < P1 < P2 < P3 < OK (数字越小越严重)."""
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "OK": 4}.get(level, 4)


# =============================================================================
# 增量学习样本生成 (供 P4 使用)
# =============================================================================
def generate_incremental_samples(perf: pd.DataFrame,
                                  trades: pd.DataFrame,
                                  days: int = 10) -> list[dict]:
    """从退化期间 (近 days 日) 提取样本, 供 DRL 增量学习 / LLM 复盘.
    每个样本: {
        "day": str,
        "indicators": {sharpe, max_dd, total_return, ...},
        "degradation_signals": [...],
        "labels": {"action": "freeze|reweight|rebuild", "priority": "P0|P1|P2"}
    }
    """
    if perf is None or perf.empty:
        return []
    samples = []
    recent = perf.tail(days).reset_index()
    for i, row in recent.iterrows():
        day = str(row.get("day") or "")
        if not day:
            continue
        ret = float(row.get("total_return") or 0)
        dd = float(row.get("max_drawdown") or 0)
        sharpe = float(row.get("sharpe_annual") or 0)
        excess = float(row.get("excess_total") or 0)
        # 判定
        if dd < -10 or sharpe < 0:
            action = "rebuild"; prio = P0
        elif ret < -5 or excess < -3:
            action = "reweight"; prio = P1
        elif dd < -6:
            action = "freeze"; prio = P1
        else:
            action = "hold"; prio = OK
        samples.append({
            "day": day,
            "indicators": {
                "total_return": ret,
                "max_drawdown": dd,
                "sharpe_annual": sharpe,
                "excess_total": excess,
            },
            "degradation_signals": [
                s for s in (
                    "回撤深" if dd < -8 else None,
                    "夏普转负" if sharpe < 0 else None,
                    "跑输基准" if excess < -2 else None,
                    "单日大跌" if ret < -3 else None,
                ) if s
            ],
            "label": {"action": action, "priority": prio},
        })
    return samples


# =============================================================================
# CLI
# =============================================================================
def run_full_check(days: int = 30) -> dict:
    """一次性运行: 加载 ArcticDB 数据 + 计算退化指数 + SPC + 增量样本."""
    perf = _load_perf_series(days=days + 30)
    trades = _load_trades(days=days)
    reward_df = _load_reward_curve(days=days)

    index = compute_strategy_degradation_index(perf, trades, reward_df)
    samples = generate_incremental_samples(perf, trades, days=days)

    # 落盘到 ArcticDB daily_summary 顶层
    try:
        from arctic_store import get_store
        store = get_store()
        latest_day = perf.index[-1] if not perf.empty else dt.date.today().strftime("%Y-%m-%d")
        if hasattr(latest_day, "strftime"):
            latest_day = latest_day.strftime("%Y-%m-%d")
        # 注意: 不覆盖 daily_summary, 用独立 symbol 存 degradation
        df = pd.DataFrame([{
            "overall_score": index["overall_score"],
            "worst_level": index["worst_level"],
            "n_components": len(index["components"]),
            "consistency_drift": (index["consistency_detail"] or {}).get("degradation_score"),
            "activity_decay": (index["activity_detail"] or {}).get("decay_ratio"),
            "reward_slope": (index["reward_detail"] or {}).get("slope"),
            "samples_p0_p1": sum(1 for s in samples if s["label"]["priority"] in (P0, P1)),
        }], index=pd.to_datetime([latest_day]))
        df.index.name = "day"
        store._lib("daily_summary")
        lib = store._lib("daily_summary")
        if lib:
            # 用 degradation_{day} 作为子 symbol
            try:
                existing = lib.read(f"degradation_{latest_day}").data
                df = pd.concat([existing[existing.index != df.index[0]], df])
                df = df[~df.index.duplicated(keep="last")]
            except Exception:
                pass
            lib.write(f"degradation_{latest_day}", df)
    except Exception as e:
        _LOG.warning(f"退化指数落盘失败: {e}")

    return {
        "degradation_index": index,
        "incremental_samples": samples,
    }


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    r = run_full_check(days=days)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))