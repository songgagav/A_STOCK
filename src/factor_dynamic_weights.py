# -*- coding: utf-8 -*-
"""PPO 动态因子权重分配: 融合因子 (pb_inv/ep/ocf_ps/roe_yy_chg) + gp4 动态权重优化.

参考: 2025-2026 年研究用 PPO 动态优化 50 个 LLM 生成因子的权重,
虽不总是最高累计收益, 但在大多数股票上实现更高夏普比率和更小最大回撤.

本模块:
  1. 从 h5i-db 加载融合因子 + gp4 的截面 z-score 值
  2. 以 FactorValueEnv 为环境, 用 CVaR_PPO 训练动态权重
  3. 输出权重到因子融合模块, 替代静态 ICIR 加权
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

from config import DATA_DIR  # noqa: E402
from drl_train import CVaR_PPO, FactorValueEnv, _compute_regime_features, _log  # noqa: E402
from drl_v2_contract import (  # noqa: E402
    cross_sectional_rank_ic,
    factor_long_short_return,
)

_LOG = logging.getLogger("factor_dynamic_weights")

# 动态加权因子列表
DYNAMIC_FACTORS = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg", "gp4"]


def compute_factor_day_metrics(
    factor_scores: dict[str, dict[str, float]],
    forward_returns: dict[str, float],
    *,
    directions: dict[str, int] | None = None,
    min_samples: int = 20,
    quantile: float = 0.2,
) -> dict[str, dict[str, float]]:
    """Compute PIT factor metrics from one aligned cross-section.

    The score maps are formed at the decision date.  ``forward_returns`` are
    labels only; callers must not expose them in the decision-date state.
    Missing labels remain missing and therefore make a day unusable when a
    factor cannot meet ``min_samples``.
    """
    directions = directions or {}
    result: dict[str, dict[str, float]] = {}
    for name, score_map in factor_scores.items():
        symbols = sorted(set(score_map) | set(forward_returns))
        scores = np.array([score_map.get(s, np.nan) for s in symbols], dtype=float)
        labels = np.array([forward_returns.get(s, np.nan) for s in symbols], dtype=float)
        ic = cross_sectional_rank_ic(scores, labels, min_samples=min_samples)
        ls = factor_long_short_return(
            scores,
            labels,
            direction=int(directions.get(name, 1)),
            quantile=quantile,
            min_samples=min_samples,
        )
        coverage = int((np.isfinite(scores) & np.isfinite(labels)).sum())
        result[name] = {
            "rank_ic": float(ic),
            "long_short_return": float(ls),
            "n_labeled": coverage,
        }
    return result


# ===================================================================
# 数据加载: 从 h5i-db 获取 PIT 因子截面 + 成熟未来收益
# ===================================================================
def load_factor_snapshot(
    as_of: str,
    lookback_days: int = 90,
) -> dict[str, Any]:
    """从 h5i-db 和因子融合模块加载因子数据.

    Args:
        as_of: 基准日期 YYYY-MM-DD.
        lookback_days: 回看交易日数.

    Returns:
        dict: {
            "ok": bool,
            "factor_history": ndarray (T, n_factors), matured PIT factor Rank IC state
            "future_returns": ndarray (T, n_factors), directional factor long-short returns
            "returns": ndarray (T,), 全 A 平均收益
            "dates": list[str], 交易日列表
            "factor_names": list[str],
            "coverage": float, 数据覆盖率
        }
    """
    try:
        from factor_fusion import (
            _calendar, _active_szsh, _fin_frame, _val_frame, _Snap,
            _assemble_snapshot, _bar_day, _residualize, _winsor,
            FACTOR_WEIGHTS, DIRECTIONS, _score_df, gp4_watch_scores,
        )
    except ImportError:
        return {"ok": False, "error": "factor_fusion 不可用"}

    cal = _calendar()
    if not cal:
        return {"ok": False, "error": "无交易日历"}

    end_ts = pd.Timestamp(str(as_of)[:10]).normalize()
    if end_ts > cal[-1]:
        end_ts = cal[-1]
    if end_ts < cal[0]:
        return {"ok": False, "error": f"as_of {end_ts} 在日历之外"}

    hist = [d for d in cal if d <= end_ts][-lookback_days:]
    if len(hist) < 30:
        return {"ok": False, "error": f"数据不足 ({len(hist)} < 30)"}

    # 读取 bar 窗口 (含未来 5 日用于计算 fwd5)
    hi_idx = min(cal.index(end_ts) + 6, len(cal) - 1)
    hi_bar = cal[hi_idx]
    bars = _sql(
        f"SELECT CAST(ts AS DATE) d, symbol, close, change_pct "
        f"FROM daily_bars WHERE CAST(ts AS DATE) >= DATE '{hist[0].strftime('%Y-%m-%d')}' "
        f"AND CAST(ts AS DATE) <= DATE '{hi_bar.strftime('%Y-%m-%d')}'"
    )
    bars["d"] = pd.to_datetime(bars["d"])
    bars["symbol"] = bars["symbol"].astype(str).str.zfill(6)
    bars = bars.drop_duplicates(["symbol", "d"], keep="last")
    bars = bars.sort_values(["symbol", "d"]).reset_index(drop=True)

    # 计算 fwd5 收益
    r1 = (1.0 + bars["change_pct"] / 100.0).to_numpy(dtype=float)
    bars = bars.assign(r1=r1)
    g = bars.groupby("symbol", sort=False)["r1"]
    cump = g.cumprod()
    fwd5 = g.cumprod().groupby(bars["symbol"], sort=False).shift(-5) / cump - 1.0  # lookahead-ok: 未来 5 日收益标签(IC 评估用), 非特征
    bars["fwd5"] = np.where(np.isfinite(fwd5), fwd5, np.nan)

    active = _active_szsh()
    fin = _fin_frame()
    val = _val_frame(hist[0] - pd.Timedelta(days=730))
    snap = _Snap(fin, val)

    # 逐日截面出分
    raw_factor_ic = []
    factor_ls_returns = []
    all_rets = []
    date_labels = []
    forward_by_day = {
        (d, s): float(v)
        for d, s, v in bars[["d", "symbol", "fwd5"]].itertuples(index=False, name=None)
        if np.isfinite(v)
    }
    factor_directions = {f: int(DIRECTIONS.get(f, 1)) for f in DYNAMIC_FACTORS}

    for D in hist:
        snap.advance(D)
        day_bars = bars[bars["d"] == D]
        df = _assemble_snapshot(snap, day_bars, active)
        dstr = D.strftime("%Y-%m-%d")
        if len(df) < 30:
            continue

        # 计算融合因子 z-score
        z_by_factor = {}
        for f in FACTOR_WEIGHTS:
            if f not in df.columns:
                continue
            zm, _ = _residualize(df, f)
            z_by_factor[f] = zm

        # 计算 gp4 z-score
        gp4_sc, gp4_md = gp4_watch_scores(df)

        factor_scores = {f: dict(z_by_factor.get(f, {})) for f in DYNAMIC_FACTORS}
        if gp4_sc:
            factor_scores["gp4"] = dict(gp4_sc)
        else:
            factor_scores.pop("gp4", None)
        if set(factor_scores) != set(DYNAMIC_FACTORS):
            continue

        forward_map = {
            s: forward_by_day.get((D, s), np.nan)
            for scores in factor_scores.values()
            for s in scores
        }
        metrics = compute_factor_day_metrics(
            factor_scores,
            forward_map,
            directions=factor_directions,
            min_samples=20,
        )
        if any(
            not np.isfinite(metrics[f]["rank_ic"])
            or not np.isfinite(metrics[f]["long_short_return"])
            for f in DYNAMIC_FACTORS
        ):
            # The last `fwd5` rows are not mature.  Do not replace them with
            # zeros: they are excluded from the training sample entirely.
            continue

        day_ic = [metrics[f]["rank_ic"] for f in DYNAMIC_FACTORS]
        day_ls = [metrics[f]["long_short_return"] for f in DYNAMIC_FACTORS]

        # 全 A 平均收益
        day_rets = day_bars["change_pct"].to_numpy(dtype=float)
        day_rets = day_rets[np.isfinite(day_rets)]
        avg_ret = float(np.mean(day_rets)) / 100.0 if len(day_rets) > 0 else 0.0

        raw_factor_ic.append(day_ic)
        factor_ls_returns.append(day_ls)
        all_rets.append(avg_ret)
        date_labels.append(dstr)

    # An IC observed on D only becomes available after the five-day label
    # matures.  Align each decision row with the IC from D-5, then discard
    # the first five rows whose state cannot yet be known.
    label_horizon = 5
    if len(raw_factor_ic) <= label_horizon:
        return {"ok": False, "error": f"有效成熟截面数不足 ({len(raw_factor_ic)} <= {label_horizon})"}
    state_ic = np.asarray(raw_factor_ic[:-label_horizon], dtype=np.float32)
    factor_returns = np.asarray(factor_ls_returns[label_horizon:], dtype=np.float32)
    aligned_returns = np.asarray(all_rets[label_horizon:], dtype=np.float64)
    aligned_dates = date_labels[label_horizon:]
    if len(state_ic) < 20:
        return {"ok": False, "error": f"有效成熟截面数不足 ({len(state_ic)} < 20)"}

    return {
        "ok": True,
        "factor_history": state_ic,
        "future_returns": factor_returns,
        "returns": aligned_returns,
        "dates": aligned_dates,
        "factor_names": DYNAMIC_FACTORS,
        "n_days": len(state_ic),
        "coverage": round(len(state_ic) / len(hist), 4),
        "factor_state_semantics": "matured_pit_rank_ic",
        "factor_label_semantics": "five_day_directional_long_short_return",
        "label_horizon_trading_days": label_horizon,
        "factor_directions": factor_directions,
        "raw_factor_ic": np.asarray(raw_factor_ic, dtype=np.float32),
    }


def _sql(q: str) -> pd.DataFrame:
    from h5i_bar_store import H5iBarStore
    store = H5iBarStore()
    try:
        return store._db.sql(q).to_pandas()
    finally:
        store.close()


# ===================================================================
# 训练入口
# ===================================================================
def run_dynamic_weight_drl(
    day: str,
    lookback_days: int = 90,
    total_timesteps: int = 600,
    n_epochs: int = 5,
    lookback: int = 5,
    brief: dict | None = None,
) -> dict[str, Any]:
    """端到端训练: 从 h5i-db 加载数据 → FactorValueEnv → CVaR_PPO → 权重输出.

    Args:
        day: YYYYMMDD 格式.
        lookback_days: 加载数据的回看天数.
        total_timesteps: PPO 训练步数.
        n_epochs: PPO epochs per rollout.
        lookback: FactorValueEnv 回看窗口.
        brief: LLM 情绪因子 (可选).

    Returns:
        dict: train_meta 内容.
    """
    day_dt = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    day_dir = day

    _log(f"动态权重 DRL 开始: day={day}, factors={DYNAMIC_FACTORS}")

    # 加载数据
    data = load_factor_snapshot(day_dt, lookback_days)
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "数据加载失败")}

    factor_history = data["factor_history"]
    future_returns = data["future_returns"]
    returns = data["returns"]
    n_factors = factor_history.shape[1]

    _log(f"数据加载完成: {data['n_days']} 日, {n_factors} 因子")

    # 计算市场状态特征
    regime_features = _compute_regime_features(returns)

    # 训练
    from drl_train import run_factor_value_drl

    meta = run_factor_value_drl(
        day=day,
        factor_history=factor_history,
        future_returns=future_returns,
        returns=returns,
        brief=brief,
        regime_features=regime_features,
        total_timesteps=total_timesteps,
        n_epochs=n_epochs,
        lookback=lookback,
        factor_names=DYNAMIC_FACTORS,
    )

    meta["factor_names"] = DYNAMIC_FACTORS
    meta["note"] = (
        "PPO 动态因子权重: matured PIT Rank IC state + directional long-short "
        "returns for pb_inv/ep/ocf_ps/roe_yy_chg/gp4"
    )
    meta["data_days"] = data["n_days"]
    meta["data_coverage"] = data["coverage"]

    # 回写最终权重到数据目录 (供 factor_fusion 消费)
    if meta.get("ok") and meta.get("final_weights"):
        out_path = os.path.join(DATA_DIR, "drl_factor_value", day_dir, "dynamic_weights.json")
        weight_map = dict(zip(DYNAMIC_FACTORS, meta["final_weights"]))
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({
                "day": day,
                "weights": weight_map,
                "algorithm": "CVaR_PPO_FactorValue",
                "mean_reward": meta.get("mean_reward"),
                "note": "动态因子权重, 供 factor_fusion 消费",
            }, f, ensure_ascii=False, indent=2)
        _log(f"动态权重已写入: {out_path}")

    return meta


# ===================================================================
# 推理: 加载训练好的动态权重
# ===================================================================
def load_dynamic_weights(day: str) -> dict[str, float] | None:
    """从 data/drl_factor_value/<day>/dynamic_weights.json 读取动态权重.

    Args:
        day: YYYYMMDD.

    Returns:
        {factor_name: weight} 或 None.
    """
    path = os.path.join(DATA_DIR, "drl_factor_value", day, "dynamic_weights.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("weights")
    except Exception:
        return None


def apply_dynamic_weights(
    static_weights: dict[str, float],
    dynamic_weights: dict[str, float] | None,
    blend_ratio: float = 0.3,
    market_regime: str | None = None,
    regime_multipliers: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    """将动态权重与静态权重融合.

    Args:
        static_weights: 静态 ICIR 加权权重.
        dynamic_weights: PPO 动态权重 (可为 None).
        blend_ratio: 动态权重占比 [0, 1].
        market_regime: 可选的规则市场状态; 提供时再应用因子路由.
        regime_multipliers: 可选的状态->因子倍率覆盖.

    Returns:
        {factor: blended_weight}.
    """
    if dynamic_weights is None:
        result = dict(static_weights)
    else:
        result = {}
        for factor, sw in static_weights.items():
            dw = dynamic_weights.get(factor, sw)
            result[factor] = sw * (1 - blend_ratio) + dw * blend_ratio

    # 归一化
    total = sum(result.values()) or 1.0
    result = {k: v / total for k, v in result.items()}
    if market_regime:
        from regime_detector import route_factor_weights
        result = route_factor_weights(result, market_regime,
                                      multipliers=regime_multipliers)
    return result
