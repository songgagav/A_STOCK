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
import time
from typing import Any

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)

from config import DATA_DIR  # noqa: E402
from drl_train import CVaR_PPO, FactorValueEnv, _compute_regime_features, _log  # noqa: E402

_LOG = logging.getLogger("factor_dynamic_weights")

# 动态加权因子列表
DYNAMIC_FACTORS = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg", "gp4"]


# ===================================================================
# 数据加载: 从 h5i-db 获取因子截面 z-score + 未来收益
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
            "factor_history": ndarray (T, n_factors), z-score 因子值
            "future_returns": ndarray (T, n_factors), 未来收益
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
    fwd5 = g.cumprod().groupby(bars["symbol"], sort=False).shift(-5) / cump - 1.0
    bars["fwd5"] = np.where(np.isfinite(fwd5), fwd5, np.nan)

    active = _active_szsh()
    fin = _fin_frame()
    val = _val_frame(hist[0] - pd.Timedelta(days=730))
    snap = _Snap(fin, val)

    # 逐日截面出分
    factor_hist = []
    fwd_returns = []
    all_rets = []
    date_labels = []

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

        # 取当日所有标的的公共交集
        common = set.union(*[set(z.keys()) for z in z_by_factor.values()])
        if gp4_sc:
            common = common & set(gp4_sc.keys())
        common = sorted(common)
        if len(common) < 20:
            continue

        # 当日因子向量 (截面均值)
        fv = np.zeros(len(DYNAMIC_FACTORS), dtype=np.float32)
        for i, f in enumerate(DYNAMIC_FACTORS):
            if f in z_by_factor and f != "gp4":
                vals = [z_by_factor[f].get(s, np.nan) for s in common]
                vals = [v for v in vals if np.isfinite(v)]
                fv[i] = float(np.mean(vals)) if vals else 0.0
            elif f == "gp4" and gp4_sc:
                vals = [gp4_sc.get(s, np.nan) for s in common]
                vals = [v for v in vals if np.isfinite(v)]
                fv[i] = float(np.mean(vals)) if vals else 0.0

        # 当日未来收益向量 (截面均值)
        fr = np.zeros(len(DYNAMIC_FACTORS), dtype=np.float32)
        for i, f in enumerate(DYNAMIC_FACTORS):
            if f in z_by_factor and f != "gp4":
                syms_with_fwd = []
                for s in common:
                    hit = bars[(bars["d"] == D) & (bars["symbol"] == s)]
                    if not hit.empty and pd.notna(hit["fwd5"].iloc[0]):
                        syms_with_fwd.append(hit["fwd5"].iloc[0])
                fr[i] = float(np.nanmean(syms_with_fwd)) if syms_with_fwd else 0.0
            elif f == "gp4" and gp4_sc:
                syms_with_fwd = []
                for s in common:
                    hit = bars[(bars["d"] == D) & (bars["symbol"] == s)]
                    if not hit.empty and pd.notna(hit["fwd5"].iloc[0]):
                        syms_with_fwd.append(hit["fwd5"].iloc[0])
                fr[i] = float(np.nanmean(syms_with_fwd)) if syms_with_fwd else 0.0

        # 全 A 平均收益
        day_rets = day_bars["change_pct"].to_numpy(dtype=float)
        day_rets = day_rets[np.isfinite(day_rets)]
        avg_ret = float(np.mean(day_rets)) / 100.0 if len(day_rets) > 0 else 0.0

        factor_hist.append(fv)
        fwd_returns.append(fr)
        all_rets.append(avg_ret)
        date_labels.append(dstr)

    if len(factor_hist) < 20:
        return {"ok": False, "error": f"有效截面数不足 ({len(factor_hist)} < 20)"}

    return {
        "ok": True,
        "factor_history": np.array(factor_hist, dtype=np.float32),
        "future_returns": np.array(fwd_returns, dtype=np.float32),
        "returns": np.array(all_rets, dtype=np.float64),
        "dates": date_labels,
        "factor_names": DYNAMIC_FACTORS,
        "n_days": len(factor_hist),
        "coverage": round(len(factor_hist) / len(hist), 4),
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
    )

    meta["factor_names"] = DYNAMIC_FACTORS
    meta["note"] = "PPO 动态因子权重: 融合因子(pb_inv/ep/ocf_ps/roe_yy_chg) + gp4"
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
) -> dict[str, float]:
    """将动态权重与静态权重融合.

    Args:
        static_weights: 静态 ICIR 加权权重.
        dynamic_weights: PPO 动态权重 (可为 None).
        blend_ratio: 动态权重占比 [0, 1].

    Returns:
        {factor: blended_weight}.
    """
    if dynamic_weights is None:
        return static_weights

    result = {}
    for factor, sw in static_weights.items():
        dw = dynamic_weights.get(factor, sw)
        result[factor] = sw * (1 - blend_ratio) + dw * blend_ratio

    # 归一化
    total = sum(result.values()) or 1.0
    return {k: v / total for k, v in result.items()}