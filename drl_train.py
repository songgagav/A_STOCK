# ============================================================
# drl_train.py -- 基于 vnpy 主链路真实回测的因子权重微调 (sb3 PPO)
#
# State  : 过去 N 日全 A 6 类因子 (signal/trend/govern/liquidity/vol/mom_rev) 的近似 IC 序列
#          + LLM pre_drl_brief 提供的 4 维情绪因子 (risk_on_off / rotation_intensity /
#          liquidity_stress / policy_catalyst) + 1 维 stance 标量 (扰动幅度)
# Action : 6 维权重增量, 经 tanh 后归一 (幅度受 stance 调节)
# Reward : Sortino 比率 + 波动率自适应 + CVaR 惩罚 (2026-09-06 升级)
#          vnpy 奖励: 用 max_dd 近似下行风险, 计算 Sortino ≈ + 波动率缩放 - CVaR
#          归因奖励: 从 benchmark.daily 日收益率序列计算真实 Sortino + 波动率缩放 - CVaR
#          IC 步奖励: IC 改进 × 10 × IC 波动率自适应缩放
#          (data/vnpy_backtest/<YYYYMMDD>/summary.json -> 对应权重下的 stats)
#
# 落盘 : data/drl/<YYYYMMDD>/{model.zip, train_meta.json, reward_curve.png, pre_drl_brief.json}
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from typing import Any, List

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, DUCKDB_PATH, MAX_STOCKS  # noqa: E402

import gymnasium  # noqa: E402
import gymnasium.spaces as spaces  # noqa: E402
import torch as th  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from stable_baselines3 import PPO  # noqa: E402
from stable_baselines3.common.utils import explained_variance  # noqa: E402

# Risk-First 和 Logic-Q 可选导入
try:
    from risk_first import RiskFirstLayer, LLMVarianceFilter, RiskExposurePenalty, CircuitBreaker
    _RISK_FIRST_AVAILABLE = True
except ImportError:
    _RISK_FIRST_AVAILABLE = False

try:
    from logic_q import LogicQ, compute_logic_q_tuning
    _LOGIC_Q_AVAILABLE = True
except ImportError:
    _LOGIC_Q_AVAILABLE = False

# 多尺度信号分解 + Hybrid-GRPO 可选导入
try:
    from wavelet_decomposition import (
        extract_wavelet_features, smooth_reward_with_wavelet,
        compute_grpo_gae, hybrid_grpo_loss,
    )
    _WAVELET_AVAILABLE = True
except ImportError:
    _WAVELET_AVAILABLE = False

# Hi-DARTS 层次化多智能体 可选导入
try:
    from hierarchical_agents import MetaAgent, DailyAgent, WeeklyAgent, EventAgent
    _HIDARTS_AVAILABLE = True
except ImportError:
    _HIDARTS_AVAILABLE = False

# StockMARL 多智能体模拟 可选导入
try:
    from multi_agent_sim import HeterogeneousAgentSim
    _STOCKMARL_AVAILABLE = True
except ImportError:
    _STOCKMARL_AVAILABLE = False

# 可解释 RL 可选导入
try:
    from explainable_rl import (
        FeatureImportanceTracker, AdaptiveFeatureSelector,
        DecisionTrace, compute_explainability_penalty,
    )
    _XRL_AVAILABLE = True
except ImportError:
    _XRL_AVAILABLE = False

SCORE_FACTORS = ["signal", "trend", "govern", "liquidity", "vol", "mom_rev"]
# stance -> 权重扰动幅度乘子. 加仓放大 (积极调权), 减仓/观望收缩 (保守)
STANCE_DELTA = {
    "加仓": 1.4,
    "维持": 1.0,
    "减仓": 0.7,
    "观望": 0.5,
}

# NeSy-TA 默认调优参数 (fallback, 等价于"维持" stance)
_DEFAULT_TUNING = {
    "delta_scale": 1.0,
    "temperature": 1.0,
    "weight_clip": 0.6,
}


# ============================================================
# Sortino + 波动率自适应 + CVaR 奖励函数核心
# 2025 年 A 股研究表明: 下行风险远重于上行波动,
# Sortino 比率在"只做多"约束下显著优于 Sharpe.
# ============================================================
def _sortino_reward_from_returns(
    daily_returns: list[float],
    target_return: float = 0.0,
    cvar_percentile: float = 0.05,
) -> float:
    """从日收益率序列计算 Sortino + 波动率自适应缩放 + CVaR 惩罚.

    Args:
        daily_returns: 日收益率列表 (小数, 如 0.01 = 1%).
        target_return: 目标收益率 (默认 0, 即无风险利率为 0).
        cvar_percentile: CVaR 分位 (默认 0.05 = 95% CVaR).

    Returns:
        float: 组合奖励值, 经 np.clip 限制在 [-5, 5].
    """
    arr = np.array(daily_returns, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 5:
        return 0.0

    mean_ret = float(arr.mean())
    total_std = float(arr.std())

    # ---- Sortino 比率 ----
    downside = arr[arr < target_return]
    downside_std = float(downside.std()) if len(downside) > 1 else 1e-6
    sortino = (mean_ret - target_return) / downside_std if downside_std > 1e-12 else 0.0

    # ---- 波动率自适应缩放 ----
    # 高波动市场自动降权, 避免策略在震荡市中过度交易
    vol_scaling = 1.0 / (1.0 + total_std * 10.0)

    # ---- CVaR 惩罚 ----
    # 尾部风险越大, 惩罚越重 (仅当样本 >= 20 时计算, 否则跳过)
    if len(arr) >= 20:
        cvar = float(np.percentile(arr, cvar_percentile * 100))
        cvar_penalty = max(0.0, abs(cvar) * 0.5)
    else:
        cvar_penalty = 0.0

    reward = sortino * vol_scaling - cvar_penalty
    return float(np.clip(reward, -5.0, 5.0))


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [drl] {msg}", flush=True)


# ============================================================
# 市场状态感知 (Regime-Aware) 特征计算
#
# 从全 A 平均收益率序列提取 3 维市场状态特征:
#   market_regime       (0/1/2): 0=震荡, 1=上涨趋势, 2=下跌趋势
#   volatility_quantile (0~1):  当前波动率在历史中的分位数
#   trend_strength      (0~1):  趋势强度 (20日/60日均线比)
# ============================================================
def _compute_regime_features(rets: np.ndarray) -> np.ndarray:
    """从全 A 平均收益率序列计算市场状态特征.

    Args:
        rets: 形状 (T,), 日频全 A 平均收益率 (小数).

    Returns:
        形状 (T, 3), 每列: [market_regime, volatility_quantile, trend_strength].
    """
    T = len(rets)
    out = np.zeros((T, 3), dtype=np.float64)

    for t in range(T):
        # ---- market_regime: 20 日滚动收益符号 ----
        lo = max(0, t - 19)
        ret_20 = float(rets[lo:t + 1].sum()) if t >= 19 else 0.0
        # 阈值 ±2%
        if ret_20 > 0.02:
            regime = 1.0
        elif ret_20 < -0.02:
            regime = 2.0
        else:
            regime = 0.0

        # ---- volatility_quantile: 20 日波动率在 252 日历史中的分位 ----
        if t >= 19:
            vol_20 = float(rets[lo:t + 1].std(ddof=1))
        else:
            vol_20 = 0.0
        hist_lo = max(0, t - 251)
        hist_vols = np.array([float(rets[max(0, i - 19):i + 1].std(ddof=1))
                              for i in range(hist_lo + 19, t + 1)])
        if len(hist_vols) > 5 and vol_20 > 1e-12:
            vol_quantile = float(np.mean(hist_vols <= vol_20))
        else:
            vol_quantile = 0.5

        # ---- trend_strength: 20日/60日均线比 ----
        if t >= 59:
            ma20 = float(rets[t - 19:t + 1].mean())
            ma60 = float(rets[t - 59:t + 1].mean())
            trend = ma20 / ma60 if abs(ma60) > 1e-12 else 1.0
            # 归一化到 [0, 1]: 比值为 1 时 trend_strength = 0.5
            trend_strength = float(np.clip((trend - 0.95) / 0.1, 0.0, 1.0))
        else:
            trend_strength = 0.5

        out[t] = [regime, vol_quantile, trend_strength]

    return out


# ============================================================
# Gymnasium 环境: state=IC 序列 + 情绪因子 + stance 标量 + 市场状态,
#                action=权重 delta, reward 走 vnpy
# ============================================================
class FactorWeightEnv(gymnasium.Env):
    def __init__(self, ic_history: np.ndarray, base_weights: np.ndarray,
                 lookback: int = 10, day: dt.date | None = None,
                 brief: dict | None = None,
                 tuning: dict | None = None,
                 regime_features: np.ndarray | None = None):
        super().__init__()
        from gymnasium import spaces

        self.ic_history = ic_history.astype(np.float32)
        self.base_weights = base_weights.astype(np.float32)
        self.lookback = lookback
        self.n_factors = len(base_weights)
        self.t = self.lookback
        self.weights = base_weights.copy()
        self.day = day

        # 市场状态特征 (Regime-Aware): [market_regime, vol_quantile, trend_strength]
        if regime_features is not None:
            self.regime_features = regime_features.astype(np.float32)
        else:
            # 无数据时默认中性值
            self.regime_features = np.full((len(ic_history), 3), 0.5, dtype=np.float32)

        # LLM pre_drl_brief: 4 维情绪因子 + 1 维 stance 标量
        brief = brief or {}
        sf = brief.get("sentiment_factors") if isinstance(brief, dict) else None
        stance = brief.get("stance") if isinstance(brief, dict) else None
        self.sentiment_vec = np.array([
            float((sf or {}).get("risk_on_off", 0.0)),
            float((sf or {}).get("rotation_intensity", 0.0)),
            float((sf or {}).get("liquidity_stress", 0.0)),
            float((sf or {}).get("policy_catalyst", 0.0)),
        ], dtype=np.float32)
        self.stance_scalar = np.array([
            float(STANCE_DELTA.get(stance, 1.0)),
        ], dtype=np.float32)

        # NeSy-TA 动态调优参数 (P2): 优先用 tuning, 否则用 LLM stance 固定映射
        tuning = tuning or {}
        if tuning and tuning.get("mode") != "fallback":
            self.delta_scale = float(tuning.get("delta_scale", _DEFAULT_TUNING["delta_scale"]))
            self.temperature = float(tuning.get("temperature", _DEFAULT_TUNING["temperature"]))
            self.weight_clip = float(tuning.get("weight_clip", _DEFAULT_TUNING["weight_clip"]))
            self.tuning_mode = tuning.get("mode", "unknown")
        else:
            self.delta_scale = float(STANCE_DELTA.get(stance, 1.0))
            self.temperature = 1.0
            self.weight_clip = 0.6
            self.tuning_mode = "stance_fixed"

        # 观测: IC 历史(10×6=60) + 4 情绪 + 1 stance + 3 市场状态 = 68
        obs_dim = lookback * self.n_factors + 4 + 1 + 3
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf,
            shape=(obs_dim,),
            dtype=np.float32,
        )
        self.action_space = spaces.Box(
            low=-1.0, high=1.0, shape=(self.n_factors,), dtype=np.float32,
        )

    def reset(self, *, seed=None, options=None):
        if seed is not None:
            np.random.random.seed(seed)
        self.t = self.lookback
        self.weights = self.base_weights.copy()
        return self._state(), {}

    def _state(self) -> np.ndarray:
        ic_part = self.ic_history[self.t - self.lookback:self.t].flatten()
        return np.concatenate(
            [ic_part, self.sentiment_vec, self.stance_scalar,
             self.regime_features[self.t]]
        ).astype(np.float32)

    def step(self, action):
        action = np.asarray(action, dtype=np.float32)
        # NeSy-TA 动态调优: delta_scale 控制幅度, temperature 控制噪声
        noise = np.random.randn(self.n_factors).astype(np.float32) * (self.temperature - 1.0) * 0.02
        delta = (np.tanh(action) + noise) * 0.05 * self.delta_scale
        new_w = self.weights + delta
        lo = max(0.01, 0.02 - (self.weight_clip - 0.3) * 0.05)
        hi = self.weight_clip
        new_w = np.clip(new_w, lo, hi)
        new_w = new_w / new_w.sum()
        self.weights = new_w
        # 用 IC 加权近似"该日因子组合预测得分", 越接近后续真实收益越好.
        # 加入波动率自适应缩放: IC 高波动时降低单步奖励权重, 避免策略在
        # 不稳定信号期过度调整.
        if self.t < len(self.ic_history):
            pred_score = float(np.dot(self.weights, self.ic_history[self.t]))
            next_score = float(np.dot(self.base_weights, self.ic_history[self.t]))
            ic_diff = pred_score - next_score

            # 滚动窗口 IC 波动率
            lo = max(0, self.t - 20)
            ic_window = self.ic_history[lo:self.t]
            ic_vol = float(ic_window.std()) if len(ic_window) > 1 else 0.01
            ic_vol = max(0.001, ic_vol)
            vol_scaling = 1.0 / (1.0 + ic_vol * 10.0)

            reward = ic_diff * 10.0 * vol_scaling
        else:
            reward = 0.0
        self.t += 1
        done = self.t >= len(self.ic_history)
        truncated = False
        info = {"weights": new_w.tolist()}
        return self._state(), float(reward), bool(done), bool(truncated), info


# ============================================================
# PPO 动态因子权重优化: FactorValueEnv
#
# 与 FactorWeightEnv (IC 序列 → 权重增量) 不同,
# FactorValueEnv 直接用原始因子值 (z-score) 作为状态,
# 让 PPO 直接输出归一化因子权重, 实现动态复权.
#
# 参考: 西南证券 DTLC_RL 框架 —— 特征空间解耦实现动态复权
# ============================================================
class FactorValueEnv(gymnasium.Env):
    """原始因子值环境: 状态=因子 z-score, 动作=因子权重, 奖励=加权未来收益.

    State:
        factor_values (n_factors,) + market_regime (3,) + 情绪因子 (4,) + stance (1,)

    Action:
        (n_factors,) -> softmax 归一化为权重

    Reward:
        加权因子收益 × 波动率自适应缩放 - CVaR 惩罚 (借鉴 Sortino 奖励函数)
    """

    def __init__(
        self,
        factor_history: np.ndarray,         # (T, n_factors) z-score 因子值
        future_returns: np.ndarray,          # (T, n_factors) 未来收益
        returns: np.ndarray,                 # (T,) 全 A 平均收益 (用于市场状态)
        brief: dict | None = None,
        lookback: int = 5,
        regime_features: np.ndarray | None = None,
        risk_first_layer: RiskFirstLayer | None = None,
        factor_names: list[str] | None = None,
    ):
        super().__init__()
        self.factor_history = factor_history.astype(np.float32)
        self.future_returns = future_returns.astype(np.float32)
        self.n_factors = factor_history.shape[1]
        self.lookback = lookback
        self.t = self.lookback

        # Risk-First 约束层 (可选)
        self.risk_first_layer = risk_first_layer
        self.factor_names = factor_names or []

        # 市场状态特征
        if regime_features is not None:
            self.regime_features = regime_features.astype(np.float32)
        else:
            self.regime_features = np.full((len(returns), 3), 0.5, dtype=np.float32)

        # LLM 情绪因子
        brief = brief or {}
        sf = brief.get("sentiment_factors") if isinstance(brief, dict) else None
        stance = brief.get("stance") if isinstance(brief, dict) else None
        self.sentiment_vec = np.array([
            float((sf or {}).get("risk_on_off", 0.0)),
            float((sf or {}).get("rotation_intensity", 0.0)),
            float((sf or {}).get("liquidity_stress", 0.0)),
            float((sf or {}).get("policy_catalyst", 0.0)),
        ], dtype=np.float32)
        self.stance_scalar = np.array([
            float(STANCE_DELTA.get(stance, 1.0)),
        ], dtype=np.float32)

        # 观测: 因子值(扁平 lookback × n_factors) + 情绪(4) + stance(1) + regime(3)
        feat_dim = lookback * self.n_factors + 4 + 1 + 3
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(feat_dim,), dtype=np.float32,
        )
        self.action_space = spaces.Box(
            low=-1.0, high=1.0, shape=(self.n_factors,), dtype=np.float32,
        )

    def reset(self, *, seed=None, options=None):
        if seed is not None:
            np.random.seed(seed)
        self.t = self.lookback
        return self._state(), {}

    def _state(self) -> np.ndarray:
        lo = self.t - self.lookback
        fv_part = self.factor_history[lo:self.t].flatten()
        return np.concatenate([
            fv_part, self.sentiment_vec, self.stance_scalar,
            self.regime_features[self.t],
        ]).astype(np.float32)

    def step(self, action):
        action = np.asarray(action, dtype=np.float32)
        # 动作: tanh 归一化到 [-1, 1], 再映射到 [0.05, 0.95] → softmax 归一化
        weights = np.tanh(action) * 0.45 + 0.5  # [0.05, 0.95]
        weights = np.clip(weights, 0.05, 0.95)
        weights = weights / weights.sum()

        # 奖励: 加权未来收益 × 波动率自适应缩放
        if self.t < len(self.future_returns):
            port_ret = float(np.dot(weights, self.future_returns[self.t]))
            # 波动率自适应: 因子收益高波动时降权
            lo = max(0, self.t - 20)
            ret_window = self.future_returns[lo:self.t]
            if len(ret_window) > 1:
                vol = float(ret_window.std()) + 1e-8
            else:
                vol = 0.01
            vol_scaling = 1.0 / (1.0 + vol * 10.0)

            # CVaR 惩罚: 尾部风险
            if self.t >= 20:
                cvar_percentile = 0.05
                all_ret = self.future_returns[:self.t] @ weights
                cvar = float(np.percentile(all_ret, cvar_percentile * 100))
                cvar_penalty = max(0.0, abs(cvar) * 0.5)
            else:
                cvar_penalty = 0.0

            reward = port_ret * 100.0 * vol_scaling - cvar_penalty
        else:
            reward = 0.0

        # ---- Risk-First 风险暴露惩罚 (2026-09-07 升级) ----
        risk_penalty = 0.0
        if self.risk_first_layer is not None:
            risk_penalty = self.risk_first_layer.step_reward_penalty(
                weights, factor_names=self.factor_names)
            reward -= risk_penalty

        self.t += 1
        done = self.t >= len(self.factor_history) - 1
        truncated = False
        info: dict = {"weights": weights.tolist()}
        if self.risk_first_layer is not None:
            info["risk_penalty"] = risk_penalty
        return self._state(), float(np.clip(reward, -5.0, 5.0)), bool(done), bool(truncated), info


# ============================================================
# 数据: 从 DuckDB 取近 N 日 daily_bars, 计算 6 维近似 IC 序列
# ============================================================
def _load_factor_state(day: dt.date, lookback_days: int = 60):
    import duckdb
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    rows = con.execute(
        "SELECT DISTINCT date FROM daily_bars "
        "WHERE date<=? AND date>=? ORDER BY date",
        [day, day - dt.timedelta(days=lookback_days)],
    ).fetchall()
    dates = [r[0] for r in rows]
    if len(dates) < 15:
        return None, None, None

    # 每日平均涨幅 (近似"全 A 收益"). 排除 close<=0 的退市/停牌/异常股,
    # 否则 AVG(Inf) 会污染整条 IC 序列, 导致 PPO 观测含 NaN -> 网络参数
    # 全 NaN -> 训练崩溃 (2026-09-02 修复).
    rets = []
    for i, d in enumerate(dates):
        prev = dates[i - 1] if i > 0 else None
        if prev:
            r = con.execute(
                "SELECT AVG(b.close/p.close - 1.0) FROM daily_bars b "
                "JOIN daily_bars p ON b.symbol=p.symbol AND p.date=? "
                "WHERE b.date=? AND b.close > 0 AND p.close > 0",
                [prev, d],
            ).fetchone()
            v = r[0] if r and r[0] is not None else 0.0
            rets.append(v if np.isfinite(v) else 0.0)
        else:
            rets.append(0.0)
    con.close()
    arr = np.array(rets, dtype=np.float64)
    # 兜底: NaN/Inf 归一化为 0, 防止污染后续 corrcoef/std 计算
    arr = np.where(np.isfinite(arr), arr, 0.0)
    n = len(arr)
    ic = np.zeros((n, 6), dtype=np.float64)
    for t in range(n):
        if t >= 5:
            window = arr[max(0, t - 19):t + 1]
            if len(window) > 1 and np.all(np.isfinite(window)):
                c = np.corrcoef(window, np.arange(len(window)))[0, 1]
                ic[t, 0] = c if np.isfinite(c) else 0.0
            else:
                ic[t, 0] = 0.0
            ic[t, 1] = arr[t]
            ic[t, 2] = (arr[t] - arr[max(0, t - 5):t].mean()) if t >= 5 else 0.0
            std_w = np.std(window) if len(window) > 1 and np.all(np.isfinite(window)) else 0.0
            ic[t, 3] = std_w if np.isfinite(std_w) else 0.0
            ic[t, 4] = -ic[t, 3]  # 低波动 alpha
            ic[t, 5] = -arr[t]   # 反转动量
    # 最终兜底: 全矩阵 NaN/Inf 归一化 (防御后续 np.concatenate 污染观测)
    ic = np.where(np.isfinite(ic), ic, 0.0)
    return ic, np.array(rets, dtype=np.float64), dates


def _load_base_weights() -> np.ndarray:
    try:
        with open(os.path.join(DATA_DIR, "weights.json"), encoding="utf-8") as f:
            w = json.load(f)
        return np.array([w.get(k, 1 / 6) for k in SCORE_FACTORS], dtype=np.float64)
    except Exception:
        return np.ones(6, dtype=np.float64) / 6


def _apply_brief_multiplier(base_weights: np.ndarray, brief: dict | None) -> tuple[np.ndarray, dict]:
    """把 LLM factor_recommendations (0~2 乘子) 应用到 base_weights, 再归一.
    返回 (调整后权重, 应用明细 {factor: 原 base / 乘子 / 调整后}).

    若 brief 缺失/无效则原样返回, 应用明细空.
    """
    if not brief:
        return base_weights, {}
    fr = brief.get("factor_recommendations") if isinstance(brief, dict) else None
    if not isinstance(fr, dict):
        return base_weights, {}
    adj = []
    detail = {}
    for i, name in enumerate(SCORE_FACTORS):
        m = fr.get(name)
        if m is None:
            v = float(base_weights[i])
        else:
            try:
                m = max(0.0, min(2.0, float(m)))
            except (TypeError, ValueError):
                m = 1.0
            v = float(base_weights[i]) * m
        adj.append(v)
        detail[name] = {
            "base": float(base_weights[i]),
            "multiplier": float(fr.get(name, 1.0)) if fr.get(name) is not None else 1.0,
            "adjusted": v,
        }
    arr = np.array(adj, dtype=np.float64)
    s = arr.sum()
    if s > 0:
        arr = arr / s
    return arr.astype(np.float64), detail


def _load_vnpy_signal(day_dir: str) -> dict:
    """从 vnpy 主链路产物读取真实回测统计作为奖励信号"""
    p = os.path.join(DATA_DIR, "vnpy_backtest", day_dir, "summary.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _vnpy_reward(stats: dict) -> float:
    """vnpy 主链路成交统计 -> reward (Sortino 近似 + 波动率自适应 + CVaR 惩罚).

    vnpy summary.json 只存汇总统计, 无日收益率序列, 因此用可用指标近似:
      - Sortino ≈ (total_return / 100) / max(0.01, |max_dd| / 100)
        用最大回撤作为下行风险代理 (回撤越深 → downside_risk 越大).
      - 波动率自适应: 1 / (1 + return_std × 10)
      - CVaR 惩罚: max_dd / 200 (回撤尾部惩罚)
    """
    if not stats or stats.get("fallback"):
        return -1.0  # fallback 时视为负信号
    s = stats.get("stats", {}) if "stats" in stats else stats
    total_return = s.get("total_return", 0) or 0
    max_dd = s.get("max_ddpercent", 0) or 0
    return_std = s.get("return_std", 0) or 0

    if not (isinstance(total_return, (int, float)) and total_return > 0):
        return -1.0

    # Sortino 近似: 用 max_dd 作为 downside risk 代理
    downside_risk = max(0.01, abs(max_dd) / 100.0) if abs(max_dd) > 0.01 else 0.01
    sortino_approx = (total_return / 100.0) / downside_risk

    # 波动率自适应缩放
    vol = max(0.001, return_std / 100.0)
    vol_scaling = 1.0 / (1.0 + vol * 10.0)

    # CVaR 近似惩罚: 最大回撤越深, 尾部风险惩罚越重
    cvar_penalty = max(0.0, abs(max_dd) / 200.0)

    reward = sortino_approx * vol_scaling - cvar_penalty
    return float(np.clip(reward, -5.0, 5.0))


def _load_perf_report() -> dict:
    """读绩效归因报告 performance_report.json (前一交易日版本, run_daily 顺序所致).
    该报告含 metrics(收益/回撤/Sharp/Calmar) / benchmark(超额) / attribution / ic_summary.
    """
    p = os.path.join(DATA_DIR, "performance_report.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _attribution_reward(perf: dict) -> float:
    """绩效归因报告 -> reward (Sortino + 波动率自适应 + CVaR 惩罚).

    从 benchmark.daily 提取组合日收益率序列, 计算真实 Sortino 比率,
    叠加波动率自适应缩放和 CVaR 尾部风险惩罚.

    日收益率不足 5 日时回退到原逻辑 (总收益 + 超额 + 风险调整 - 回撤惩罚).
    """
    if not perf or not perf.get("ok"):
        return 0.0

    # ---- 优先用日收益率序列计算真实 Sortino ----
    bench = perf.get("benchmark") or {}
    daily_list = bench.get("daily") or []
    port_rets = [d.get("port_ret") for d in daily_list
                 if d.get("port_ret") is not None]

    if len(port_rets) >= 5:
        return _sortino_reward_from_returns(
            port_rets, target_return=0.0, cvar_percentile=0.05)

    # ---- 日收益率不足时回退到原逻辑 ----
    m = perf.get("metrics") or {}
    bench = perf.get("benchmark") or {}
    total_return = float(m.get("total_return") or 0)      # % (0.441 = 0.441%)
    max_dd = float(m.get("max_drawdown") or 0)            # % (负值, -0.121 = -0.121%)
    sharpe = float(m.get("sharpe_annual") or 0)
    calmar = float(m.get("calmar") or 0)
    excess = float(bench.get("excess_total") or 0)        # 小数 (-0.0278 = -2.78%)
    # 风险调整项: Sharpe 与 Calmar 都放大到可感知的量级, 再揉入有界到 [0,1]
    risk_adj = max(0.0, min(1.0, (sharpe / 20.0 + max(0.0, calmar) / 200.0)))
    r = total_return / 100.0                     # 收益 (0.441% -> 0.00441)
    r = r + excess                               # 超额收益 (小数, 直接加)
    r = r + risk_adj * 0.5                       # 风险调整 (上限 +0.5)
    r = r - max(0.0, abs(max_dd) / 200.0)        # 回撤惩罚 (0.121% -> -0.0006)
    return max(-2.0, min(2.0, r))


# ============================================================
# CVaR-PPO: 尾部风险直接嵌入优化目标
#
# 在标准 PPO loss 中增加 CVaR 约束项:
#   total_loss = ppo_loss + cvar_coef * cvar_loss
# 其中 cvar_loss = -mean(worst α% returns) 对高损失轨迹施加额外惩罚,
# 让模型在训练时直接学习规避极端亏损路径.
#
# 超参数自适应 (2026-09-06 升级):
#   监控训练过程中的策略熵值, 当熵值低于阈值时自动增加 ent_coef
#   (鼓励探索) 并降低学习率 (稳定更新), 实现上下文感知的策略更新.
#   参考: UG-CPPO 的"不确定性门控"机制, 2025 RL-based execution optimization.
# ============================================================
class CVaR_PPO(PPO):
    """PPO with CVaR constraint + entropy monitoring + Risk-First constraint layer."""

    def __init__(
        self,
        *args,
        cvar_alpha: float = 0.05,
        cvar_coef: float = 0.1,
        entropy_threshold: float = -1.0,
        lr_decay_factor: float = 0.8,
        ent_coef_boost: float = 1.5,
        risk_first_coef: float = 0.0,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.cvar_alpha = cvar_alpha
        self.cvar_coef = cvar_coef
        # 超参数自适应
        self.entropy_threshold = entropy_threshold
        self.lr_decay_factor = lr_decay_factor
        self.ent_coef_boost = ent_coef_boost
        self.adaptation_count = 0
        self.original_ent_coef = float(self.ent_coef)
        # Risk-First 约束层: risk_first_coef > 0 时启用
        self.risk_first_coef = risk_first_coef
        self._risk_first_layer = None
        if risk_first_coef > 0 and _RISK_FIRST_AVAILABLE:
            from config import RISK_FIRST as _RF_CFG
            self._risk_first_layer = RiskFirstLayer(
                variance_filter=LLMVarianceFilter(
                    window=_RF_CFG["variance_filter_window"],
                    n_std=_RF_CFG["variance_filter_n_std"],
                ),
                exposure_penalty=RiskExposurePenalty(
                    penalty_coef=_RF_CFG["exposure_penalty_coef"],
                ),
                circuit_breaker=CircuitBreaker(
                    drawdown_threshold=_RF_CFG["circuit_breaker_drawdown"],
                    vol_threshold=_RF_CFG["circuit_breaker_vol"],
                    cvar_threshold=_RF_CFG["circuit_breaker_cvar"],
                ),
            )

    @staticmethod
    def _compute_cvar_loss(returns: th.Tensor, alpha: float = 0.05) -> th.Tensor:
        """CVaR = 最差 alpha% 部分的平均损失的相反数.

        Args:
            returns: 批次内各时间步的 TD(lambda) 回报.
            alpha: 尾部百分位 (默认 0.05 = 95% CVaR).

        Returns:
            scalar Tensor, 非负值表示尾部损失惩罚.
        """
        n = returns.shape[0]
        if n < 2:
            return th.tensor(0.0, device=returns.device)
        sorted_ret, _ = th.sort(returns)
        tail_idx = max(1, int(n * alpha))
        tail = sorted_ret[:tail_idx]
        # 若尾部分为正收益, 不惩罚
        cvar = -tail.mean()
        return th.clamp(cvar, min=0.0)

    def _adapt_hyperparameters(self, mean_entropy: float) -> bool:
        """检查熵值, 低于阈值时自适应调整超参数.

        Returns:
            bool: 是否执行了自适应调整.
        """
        if self.entropy_threshold is None or mean_entropy > self.entropy_threshold:
            return False
        # 熵值过低 -> 增加探索系数, 降低学习率
        new_ent_coef = float(self.ent_coef) * self.ent_coef_boost
        self.ent_coef = min(new_ent_coef, self.original_ent_coef * 5.0)
        new_lr = self.learning_rate * self.lr_decay_factor
        self.learning_rate = max(new_lr, 1e-6)
        for g in self.policy.optimizer.param_groups:
            g["lr"] = self.learning_rate
        self.adaptation_count += 1
        if self.verbose >= 1:
            print(f"[CVaR_PPO] 熵过低 ({mean_entropy:.4f} < {self.entropy_threshold:.4f}), "
                  f"调整: ent_coef={self.ent_coef:.6f}, lr={self.learning_rate:.2e} "
                  f"(#{self.adaptation_count})")
        return True

    def train(self) -> None:
        """
        Update policy using the currently gathered rollout buffer.
        Overrides PPO.train() to add CVaR constraint term + hyperparameter adaptation.
        """
        self.policy.set_training_mode(True)
        self._update_learning_rate(self.policy.optimizer)
        clip_range = self.clip_range(self._current_progress_remaining)
        if self.clip_range_vf is not None:
            clip_range_vf = self.clip_range_vf(self._current_progress_remaining)

        entropy_losses, pg_losses, value_losses = [], [], []
        clip_fractions, cvar_losses = [], []
        continue_training = True

        for epoch in range(self.n_epochs):
            approx_kl_divs = []
            for rollout_data in self.rollout_buffer.get(self.batch_size):
                actions = rollout_data.actions
                if isinstance(self.action_space, spaces.Discrete):
                    actions = rollout_data.actions.long().flatten()

                values, log_prob, entropy = self.policy.evaluate_actions(
                    rollout_data.observations, actions
                )
                values = values.flatten()
                advantages = rollout_data.advantages
                if self.normalize_advantage and len(advantages) > 1:
                    advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

                ratio = th.exp(log_prob - rollout_data.old_log_prob)
                policy_loss_1 = advantages * ratio
                policy_loss_2 = advantages * th.clamp(ratio, 1 - clip_range, 1 + clip_range)
                policy_loss = -th.min(policy_loss_1, policy_loss_2).mean()
                pg_losses.append(policy_loss.item())

                clip_fraction = th.mean((th.abs(ratio - 1) > clip_range).float()).item()
                clip_fractions.append(clip_fraction)

                if self.clip_range_vf is None:
                    values_pred = values
                else:
                    values_pred = rollout_data.old_values + th.clamp(
                        values - rollout_data.old_values, -clip_range_vf, clip_range_vf
                    )
                value_loss = F.mse_loss(rollout_data.returns, values_pred)
                value_losses.append(value_loss.item())

                if entropy is None:
                    entropy_loss = -th.mean(-log_prob)
                else:
                    entropy_loss = -th.mean(entropy)
                entropy_losses.append(entropy_loss.item())

                # ---- CVaR 约束项 ----
                cvar_loss = self._compute_cvar_loss(rollout_data.returns, self.cvar_alpha)
                cvar_losses.append(cvar_loss.item())

                # ---- Risk-First 约束项 (2026-09-07 升级) ----
                risk_loss = 0.0
                if self.risk_first_coef > 0 and self._risk_first_layer is not None:
                    # 从 rollout 数据中近似计算暴露惩罚
                    obs_np = rollout_data.observations.cpu().numpy()
                    if obs_np.ndim == 2 and obs_np.shape[1] >= 6:
                        # 假设观测前 6 维近似代表因子权重
                        approx_weights = np.abs(obs_np[:, :6]).mean(axis=0)
                        approx_weights = approx_weights / (approx_weights.sum() + 1e-8)
                        risk_penalty = self._risk_first_layer.step_reward_penalty(
                            approx_weights,
                            factor_names=["signal", "trend", "govern",
                                          "liquidity", "vol", "mom_rev"],
                        )
                        risk_loss = risk_penalty
                risk_loss_t = th.tensor(risk_loss, device=rollout_data.returns.device,
                                        dtype=th.float32)

                loss = (
                    policy_loss
                    + self.ent_coef * entropy_loss
                    + self.vf_coef * value_loss
                    + self.cvar_coef * cvar_loss
                    + self.risk_first_coef * risk_loss_t
                )

                with th.no_grad():
                    log_ratio = log_prob - rollout_data.old_log_prob
                    approx_kl_div = th.mean((th.exp(log_ratio) - 1) - log_ratio).cpu().numpy()
                    approx_kl_divs.append(approx_kl_div)

                if self.target_kl is not None and approx_kl_div > 1.5 * self.target_kl:
                    continue_training = False
                    if self.verbose >= 1:
                        print(f"Early stopping at step {epoch} due to reaching max kl: {approx_kl_div:.2f}")
                    break

                self.policy.optimizer.zero_grad()
                loss.backward()
                th.nn.utils.clip_grad_norm_(self.policy.parameters(), self.max_grad_norm)
                self.policy.optimizer.step()

            # ---- 超参数自适应: 本轮 epoch 平均熵值低于阈值时调整 ----
            if entropy_losses:
                epoch_mean_entropy = float(np.mean(entropy_losses))
                self._adapt_hyperparameters(epoch_mean_entropy)

            self._n_updates += 1
            if not continue_training:
                break

        explained_var = explained_variance(
            self.rollout_buffer.values.flatten(), self.rollout_buffer.returns.flatten()
        )

        self.logger.record("train/entropy_loss", np.mean(entropy_losses))
        self.logger.record("train/policy_gradient_loss", np.mean(pg_losses))
        self.logger.record("train/value_loss", np.mean(value_losses))
        self.logger.record("train/approx_kl", np.mean(approx_kl_divs))
        self.logger.record("train/clip_fraction", np.mean(clip_fractions))
        self.logger.record("train/loss", loss.item())
        self.logger.record("train/explained_variance", explained_var)
        # CVaR 日志
        self.logger.record("train/cvar_loss", np.mean(cvar_losses))
        self.logger.record("train/cvar_alpha", self.cvar_alpha)
        self.logger.record("train/cvar_coef", self.cvar_coef)
        # Risk-First 日志
        self.logger.record("train/risk_loss", risk_loss if isinstance(risk_loss, float) else 0.0)
        self.logger.record("train/risk_first_coef", self.risk_first_coef)
        # 超参数自适应日志
        self.logger.record("train/entropy_threshold", self.entropy_threshold)
        self.logger.record("train/adaptation_count", self.adaptation_count)
        self.logger.record("train/adapted_ent_coef", self.ent_coef)
        if hasattr(self.policy, "log_std"):
            self.logger.record("train/std", th.exp(self.policy.log_std).mean().item())
        self.logger.record("train/n_updates", self._n_updates, exclude="tensorboard")
        self.logger.record("train/clip_range", clip_range)
        if self.clip_range_vf is not None:
            self.logger.record("train/clip_range_vf", clip_range_vf)


# ============================================================
# 主训练函数
# ============================================================
def run_drl_train(day: str, total_timesteps: int = 800, n_epochs: int = 4,
                  use_vnpy_reward: bool = True,
                  brief: dict | None = None,
                  cvar_alpha: float | None = None,
                  cvar_coef: float | None = None) -> dict:
    """DRL 微调 (CVaR-PPO).

    Args:
        day: 交易日 YYYY-MM-DD.
        total_timesteps: PPO 训练步数.
        n_epochs: PPO epochs per rollout.
        use_vnpy_reward: 是否使用 vnpy 真实回测奖励.
        brief: LLM pre_drl_brief 的 dict.
        cvar_alpha: CVaR 尾部百分位 (默认从 config.CVAR_PPO 读取, 最终回退到 0.05).
        cvar_coef:  CVaR 约束项权重 (默认从 config.CVAR_PPO 读取, 最终回退到 0.1).

    brief: 可选, LLM pre_drl_brief 的 dict (含 sentiment_factors / stance /
           factor_recommendations / market_summary / confidence). 若不传则自动
           从 data/drl/<YYYYMMDD>/pre_drl_brief.json 读取.
    """
    day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
    day_dir = day.replace("-", "")

    # ---- 心跳: 证明 DRL 正在处理 (卡死时 last_seen 停更, 检查方据此告警) ----
    from heartbeat import Heartbeat
    heartbeat_dir = os.path.join(DATA_DIR, "drl", day_dir)
    hb = Heartbeat(heartbeat_dir, "drl_train",
                   extra={"day": day_dir, "total_timesteps": total_timesteps})
    hb.start(phase="loading_factor_state")

    ic, rets, dates = _load_factor_state(day_dt, 60)
    if ic is None or len(rets) < 15:
        hb.stop(phase="data_insufficient", ok=False,
                error=f"数据不足 (<15 日): {0 if ic is None else len(rets)} 行")
        return {"ok": False, "error": "数据不足 (<15 日)", "rows": 0}

    # 市场状态感知特征: 从全 A 平均收益率序列计算 3 维市场状态
    regime_features = _compute_regime_features(rets)
    _log(f"市场状态特征已计算: {len(rets)} 日, "
         f"regime 分布={np.bincount(regime_features[:, 0].astype(int), minlength=3).tolist()}")

    # 读取 LLM brief (LLM 在前序 run_daily 已生成)
    if brief is None:
        try:
            from pre_drl_brief import load_pre_drl_brief
            brief = load_pre_drl_brief(day_dir) or {}
        except Exception:
            brief = {}
    brief_meta_for_log = {
        "market_summary": (brief.get("market_summary") if isinstance(brief, dict) else None),
        "regime": (brief.get("regime") if isinstance(brief, dict) else None),
        "stance": (brief.get("stance") if isinstance(brief, dict) else None),
        "confidence": (brief.get("confidence") if isinstance(brief, dict) else None),
    }

    base_w = _load_base_weights()
    # 应用 LLM 推荐的先验乘子
    prior_w, brief_detail = _apply_brief_multiplier(base_w, brief)
    _log(f"LLM brief 已加载: stance={brief_meta_for_log['stance']} "
         f"regime={brief_meta_for_log['regime']} conf={brief_meta_for_log['confidence']}")

    # NeSy-TA 动态调优 (P2): 从 symbolic_ta + neural_ta 计算 tuning
    nesy_tuning = None
    try:
        from neural_ta import compute_tuning
        nesy_tuning = compute_tuning(day_dir, use_heuristic=False)
        _log("NeSy-TA tuning: delta=" + str(round(nesy_tuning.get("delta_scale", 0), 4)) +
             " temp=" + str(round(nesy_tuning.get("temperature", 0), 4)) +
             " clip=" + str(round(nesy_tuning.get("weight_clip", 0), 4)) +
             " mode=" + str(nesy_tuning.get("mode", "?")))
    except Exception as e:
        _log("NeSy-TA tuning 不可用, 回退 stance 固定模式: " + str(e))

    env = FactorWeightEnv(ic, prior_w, day=day_dt, brief=brief or {},
                           tuning=nesy_tuning,
                           regime_features=regime_features)

    vnpy_stats = _load_vnpy_signal(day_dir) if use_vnpy_reward else {}
    vnpy_reward = _vnpy_reward(vnpy_stats)
    # 改造②: 绩效归因报告写回奖励. 报告为前一交易日版本 (run_daily 顺序所致),
    # 语义 = "历史归因校准当日奖励". 缺失时 attr_reward=0.0 中性.
    perf_report = _load_perf_report()
    attr_reward = _attribution_reward(perf_report)
    # 增量学习闭环: 读取 data/reward_config.json 调整奖励权重 (vnpy/ic/attr 三权).
    # 默认 0.6 (基础), P0 退化时由 incremental_learn 自动提到 0.8+ 强调真实信号.
    reward_weights = {"vnpy_weight": 0.6, "ic_weight": 0.4, "attr_weight": 0.15}
    try:
        from incremental_learn import get_reward_weights
        reward_weights = get_reward_weights()
    except Exception:
        pass
    vnpy_w = reward_weights["vnpy_weight"]
    ic_w = reward_weights["ic_weight"]
    attr_w = reward_weights["attr_weight"]

    try:
        hb.ping(phase="building_env")
        # CVaR-PPO 参数: CLI > 函数参数 > config > 类默认值
        from config import CVAR_PPO as _CVAR_CFG
        from config import DRL_ADAPT as _ADAPT_CFG
        _cvar_alpha = cvar_alpha if cvar_alpha is not None else _CVAR_CFG["cvar_alpha"]
        _cvar_coef = cvar_coef if cvar_coef is not None else _CVAR_CFG["cvar_coef"]
        _log(f"CVaR-PPO: alpha={_cvar_alpha}, coef={_cvar_coef}")
        _log(f"DRL 自适应: entropy_threshold={_ADAPT_CFG['entropy_threshold']}, "
             f"lr_decay={_ADAPT_CFG['lr_decay_factor']}, ent_boost={_ADAPT_CFG['ent_coef_boost']}")
        model = CVaR_PPO("MlpPolicy", env,
                         n_steps=min(64, len(rets) - 10),
                         learning_rate=3e-4, n_epochs=n_epochs, verbose=0,
                         cvar_alpha=_cvar_alpha, cvar_coef=_cvar_coef,
                         entropy_threshold=_ADAPT_CFG["entropy_threshold"],
                         lr_decay_factor=_ADAPT_CFG["lr_decay_factor"],
                         ent_coef_boost=_ADAPT_CFG["ent_coef_boost"])
        hb.ping(phase=f"learn({total_timesteps})")  # 长阻塞前打点, 守护线程持续刷新
        model.learn(total_timesteps=total_timesteps)
        hb.ping(phase="learned")

        # 回放收集 reward_curve
        obs, _ = env.reset()
        rewards = []
        weights_trace = []
        for _ in range(len(rets) - env.lookback):
            action, _ = model.predict(obs, deterministic=True)
            obs, r, done, truncated, info = env.step(action)
            rewards.append(r)
            weights_trace.append(info["weights"])
            if done or truncated:
                break

        # 叠加 vnpy 主链路真实回测信号: 把 vnpy_reward 作为最后一步的额外奖励
        # (代表: 若用最终值最终行权重去跑 vnpy 回测, 结果有多好).
        # 权重由 incremental_learn 写入 reward_config.json 动态控制.
        if vnpy_stats and not vnpy_stats.get("fallback") and rewards:
            # IC 内部奖励 + vnpy 真实奖励 + 绩效归因奖励 加权组合
            # (替代裸加, 避免真实信号被冲掉). attr 项缺失时为 0 不影响组合.
            ic_step = rewards[-1]
            rewards[-1] = (ic_step * ic_w + vnpy_reward * vnpy_w
                           + attr_reward * attr_w)

        # 保存产物
        out_dir = os.path.join(DATA_DIR, "drl", day_dir)
        os.makedirs(out_dir, exist_ok=True)
        model_path = os.path.join(out_dir, "model.zip")
        model.save(model_path)

        meta = {
            "ok": True,
            "day": day,
            "algorithm": "PPO",
            "total_timesteps": total_timesteps,
            "n_epochs": n_epochs,
            "n_obs_steps": len(rewards),
            "base_weights": {k: float(v) for k, v in zip(SCORE_FACTORS, base_w)},
            "prior_weights": {k: float(v) for k, v in zip(SCORE_FACTORS, prior_w)},
            "final_weights": {k: float(v) for k, v in zip(SCORE_FACTORS, env.weights)},
            "mean_reward": float(np.mean(rewards)) if rewards else 0.0,
            "sum_reward": float(np.sum(rewards)) if rewards else 0.0,
            # LLM brief 元信息
            "llm_brief": {
                "market_summary": brief_meta_for_log["market_summary"],
                "regime": brief_meta_for_log["regime"],
                "stance": brief_meta_for_log["stance"],
                "confidence": brief_meta_for_log["confidence"],
                "sentiment_factors": (brief.get("sentiment_factors") if isinstance(brief, dict) else None),
                "factor_recommendations": (
                    brief.get("factor_recommendations") if isinstance(brief, dict) else None
                ),
                "applied_multiplier": brief_detail,
                "delta_scale": env.delta_scale,
                "temperature": env.temperature,
                "weight_clip": env.weight_clip,
                "tuning_mode": env.tuning_mode,
                "nesy_tuning": nesy_tuning,
                "obs_dim": env.observation_space.shape[0],
            },
            # 增量学习 reward 权重 (由 incremental_learn 动态调整)
            "reward_weights": {"vnpy_weight": vnpy_w, "ic_weight": ic_w,
                               "attr_weight": attr_w},
            "vnpy_stats": {
                "engine": vnpy_stats.get("engine"),
                "total_return": vnpy_stats.get("stats", {}).get("total_return"),
                "sharpe_ratio": vnpy_stats.get("stats", {}).get("sharpe_ratio"),
                "max_ddpercent": vnpy_stats.get("stats", {}).get("max_ddpercent"),
                "trades": vnpy_stats.get("stats", {}).get("total_trade_count"),
            } if vnpy_stats else None,
            "vnpy_reward": vnpy_reward,
            "attr_reward": attr_reward,
            "perf_report": {
                "ok": bool(perf_report.get("ok")),
                "day": perf_report.get("period", {}).get("end") if isinstance(perf_report.get("period"), dict) else None,
                "metrics": perf_report.get("metrics"),
                "excess_total": (perf_report.get("benchmark") or {}).get("excess_total"),
            } if perf_report and perf_report.get("ok") else None,
            # 市场状态感知 (Regime-Aware) 特征统计
            "regime_aware": {
                "range": [int(regime_features[:, 0].min()), int(regime_features[:, 0].max())],
                "vol_quantile_mean": float(regime_features[:, 1].mean()),
                "trend_strength_mean": float(regime_features[:, 2].mean()),
                "note": "market_regime: 0=震荡, 1=上涨趋势, 2=下跌趋势; "
                        "volatility_quantile: 0~1; trend_strength: 0~1",
            },
            # Sortino 奖励指标 (2026-09-06 升级: 替代原 Sharpe 奖励)
            "sortino_reward": {
                "vnpy_sortino_approx": vnpy_reward,
                "attr_sortino": attr_reward,
                "note": "vnpy_reward 为 Sortino 近似 (利用 max_dd 代理下行风险), "
                        "attr_reward 为真实 Sortino + 波动率自适应 + CVaR (利用日收益率序列)",
            },
            # CVaR-PPO 参数 (2026-09-06 升级: 尾部风险嵌入优化目标)
            "cvar_ppo": {
                "cvar_alpha": _cvar_alpha,
                "cvar_coef": _cvar_coef,
                "note": "PPO loss 中增加 CVaR 约束项, 对最差 α% 轨迹回报施加额外惩罚",
            },
            # DRL 超参数自适应 (2026-09-06 升级: 熵监控 + 动态调整)
            "drl_adapt": {
                "entropy_threshold": _ADAPT_CFG["entropy_threshold"],
                "lr_decay_factor": _ADAPT_CFG["lr_decay_factor"],
                "ent_coef_boost": _ADAPT_CFG["ent_coef_boost"],
                "note": "熵值低于阈值时自动增加 ent_coef(探索)并降低 lr(稳定)",
            },
            "model_path": model_path,
            "n_dates": len(dates),
            "last_dates": [str(d) for d in dates[-5:]],
        }
        with open(os.path.join(out_dir, "train_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        # reward_curve.png
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(8, 3))
            ax.plot(rewards, color="#1f77b4", label="PPO step reward (IC×vol_scaling)")
            if vnpy_reward:
                ax.axhline(vnpy_reward, color="r", linestyle="--",
                           label=f"vnpy Sortino≈ ({vnpy_reward:+.3f})")
            if attr_reward:
                ax.axhline(attr_reward, color="g", linestyle=":",
                           label=f"attr Sortino+CVaR ({attr_reward:+.3f})")
            ax.set_title(f"PPO Reward Curve day={day}")
            ax.set_xlabel("step")
            ax.set_ylabel("reward")
            ax.grid(True, alpha=0.3)
            ax.legend(loc="best")
            fig.tight_layout()
            fig.savefig(os.path.join(out_dir, "reward_curve.png"), dpi=110)
            plt.close(fig)
            meta["plot"] = "reward_curve.png"
        except Exception as e:
            meta["plot_error"] = str(e)

        with open(os.path.join(out_dir, "train_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        # ===== ArcticDB reward_curve 持久化 (供 P3 退化检测), 必须 meta 已就绪 =====
        try:
            from arctic_store import get_store
            store = get_store()
            if store.append_reward_curve(day, rewards, weights_trace):
                meta["arcticdb_reward_written"] = True
        except Exception as e:
            meta["arcticdb_reward_error"] = str(e)[:120]
        # 重新写盘 (含 arcticdb 标记)
        with open(os.path.join(out_dir, "train_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        # ===== DRL 目标计划生成: 用 final_weights × v_universe_snapshot 重打分 -> TopN =====
        # 这是"信号就绪"的关键产物: 盘中 realtime_engine 优先消费此文件.
        try:
            plan = _build_target_plan(
                day=day, day_dir=day_dir,
                final_weights={k: float(v) for k, v in zip(SCORE_FACTORS, env.weights)},
                top_n=MAX_STOCKS,
            )
            meta["target_plan"] = {
                "path": plan.get("path"),
                "ok": plan.get("ok"),
                "top_n": plan.get("top_n"),
                "method": plan.get("method"),
                "universe_size": plan.get("universe_size"),
                "error": plan.get("error"),
            }
        except Exception as e:
            meta["target_plan"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        # 二次写盘 (含 target_plan)
        with open(os.path.join(out_dir, "train_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        _log(f"DRL 训练完成: timesteps={total_timesteps}, mean_reward={meta['mean_reward']:.4f}, "
             f"vnpy_reward={vnpy_reward:+.4f}")
        hb.stop(phase="done", ok=True)
        return meta
    except Exception as e:
        import traceback
        tb = traceback.format_exc(limit=3)
        _log(f"DRL 训练异常: {type(e).__name__}: {e}\n{tb}")
        hb.stop(phase="error", ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")
        return {"ok": False, "error": str(e)[:300], "rows": 0}


# ============================================================
# DRL 动态因子权重优化: 训练 FactorValueEnv
#
# 用原始因子 z-score 作为状态, PPO 直接输出因子权重.
# 适用于已实现融合因子 (pb_inv/ep/ocf_ps/roe_yy_chg) 的动态复权.
# 输出: data/drl_factor_value/<day>/model.zip + train_meta.json
# ============================================================
def run_factor_value_drl(
    day: str,
    factor_history: np.ndarray,
    future_returns: np.ndarray,
    returns: np.ndarray,
    brief: dict | None = None,
    regime_features: np.ndarray | None = None,
    total_timesteps: int = 600,
    n_epochs: int = 5,
    lookback: int = 5,
    use_risk_factors: bool | None = None,
) -> dict:
    """训练 FactorValueEnv 学习动态因子权重.

    Parameters
    ----------
    factor_history : np.ndarray
        (T, n_factors) z-score 因子值历史.
    future_returns : np.ndarray
        (T, n_factors) 各因子的未来收益.
    returns : np.ndarray
        (T,) 全 A 平均收益 (用于市场状态).
    brief : dict | None
        LLM pre_drl_brief 情绪因子.
    regime_features : np.ndarray | None
        (T, 3) 市场状态特征.
    lookback : int
        FactorValueEnv 的回看窗口.
    use_risk_factors : bool | None
        是否把风险因子观测 (波动率/CVaR95/最大回撤/下行波动) 并入环境.
        None = 取 config RISK_FACTOR_PPO.rfp_enabled (默认开启);
        True/False 显式覆盖.

    Returns
    -------
    dict
        train_meta 内容 (含 risk_obs_augmented / obs_dim).
    """
    from config import DRL_ADAPT as _ADAPT_CFG

    day_dt = dt.date(int(day[:4]), int(day[4:6]), int(day[6:8]))
    day_dir = day

    out_dir = os.path.join(DATA_DIR, "drl_factor_value", day_dir)
    os.makedirs(out_dir, exist_ok=True)

    _log(f"FactorValue DRL: factors={factor_history.shape[1]}, "
         f"T={len(factor_history)}, lookback={lookback}")

    # Risk-First 约束层 (可选, 从环境变量启用)
    risk_first_layer = None
    factor_names = ["signal", "trend", "govern", "liquidity", "vol", "mom_rev"]
    if _RISK_FIRST_AVAILABLE and os.environ.get("RISK_FIRST_ENABLED", "1") != "0":
        try:
            from config import RISK_FIRST as _RF_CFG
            risk_first_layer = RiskFirstLayer(
                variance_filter=LLMVarianceFilter(
                    window=_RF_CFG["variance_filter_window"],
                    n_std=_RF_CFG["variance_filter_n_std"],
                ),
                exposure_penalty=RiskExposurePenalty(
                    penalty_coef=_RF_CFG["exposure_penalty_coef"],
                ),
                circuit_breaker=CircuitBreaker(
                    drawdown_threshold=_RF_CFG["circuit_breaker_drawdown"],
                    vol_threshold=_RF_CFG["circuit_breaker_vol"],
                    cvar_threshold=_RF_CFG["circuit_breaker_cvar"],
                ),
            )
        except Exception:
            pass

    # ---- 风险因子观测增强 (接入默认训练入口; config RISK_FACTOR_PPO 开关) ----
    _env_cls = FactorValueEnv
    _env_kwargs = dict(
        factor_history=factor_history,
        future_returns=future_returns,
        returns=returns,
        brief=brief,
        lookback=lookback,
        regime_features=regime_features,
        risk_first_layer=risk_first_layer,
        factor_names=factor_names,
    )
    _rf_applied = False
    _use_rf = use_risk_factors
    _rf_window, _rf_alpha = 20, 0.05
    try:
        from config import RISK_FACTOR_PPO as _RFP_CFG
        if _use_rf is None:
            _use_rf = bool(_RFP_CFG.get("rfp_enabled", True))
        _rf_window = int(_RFP_CFG.get("risk_factor_window", 20))
        _rf_alpha = float(_RFP_CFG.get("cvar_alpha", 0.05))
    except Exception:
        if _use_rf is None:
            _use_rf = True
    if _use_rf:
        try:
            from risk_factor_optimizer import RiskFactorAugmentedEnv
            _env_kwargs["risk_factor_window"] = _rf_window
            _env_kwargs["cvar_alpha"] = _rf_alpha
            _env_cls = RiskFactorAugmentedEnv
            _rf_applied = True
        except Exception as _e:
            _log(f"风险因子观测增强不可用, 回退 FactorValueEnv: {_e}")

    env = _env_cls(**_env_kwargs)
    if _rf_applied:
        _log(f"风险因子观测已启用: obs_dim={env.observation_space.shape[0]}")

    model = CVaR_PPO(
        "MlpPolicy", env,
        n_steps=min(64, len(factor_history) - lookback - 1),
        learning_rate=3e-4, n_epochs=n_epochs, verbose=0,
        cvar_alpha=0.05, cvar_coef=0.1,
        entropy_threshold=_ADAPT_CFG["entropy_threshold"],
        lr_decay_factor=_ADAPT_CFG["lr_decay_factor"],
        ent_coef_boost=_ADAPT_CFG["ent_coef_boost"],
    )

    try:
        model.learn(total_timesteps=total_timesteps)
    except Exception as e:
        _log(f"FactorValue DRL learn 异常: {e}")
        return {"ok": False, "error": str(e)[:200]}

    # Rollout 评估
    rewards, weights_history = [], []
    obs, _ = env.reset()
    done = False
    while not done:
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, done, _, info = env.step(action)
        rewards.append(reward)
        if info.get("weights"):
            weights_history.append(info["weights"])

    # 最终权重 = 最后 5 步平均 (去噪)
    final_weights = (
        np.mean(weights_history[-5:], axis=0).tolist()
        if len(weights_history) >= 5 else weights_history[-1]
    )

    meta = {
        "ok": True,
        "day": day,
        "algorithm": "CVaR_PPO_FactorValue",
        "total_timesteps": total_timesteps,
        "n_epochs": n_epochs,
        "n_factors": factor_history.shape[1],
        "lookback": lookback,
        "obs_steps": len(rewards),
        "final_weights": final_weights,
        "mean_reward": float(np.mean(rewards)) if rewards else 0.0,
        "sum_reward": float(np.sum(rewards)) if rewards else 0.0,
        "regime_aware": {
            "range": [int(regime_features[:, 0].min()), int(regime_features[:, 0].max())],
            "vol_quantile_mean": float(regime_features[:, 1].mean()),
            "trend_strength_mean": float(regime_features[:, 2].mean()),
        } if regime_features is not None else None,
        "note": "PPO 动态因子权重优化: 原始因子值→权重, 越日动态复权",
        "risk_obs_augmented": _rf_applied,
        "obs_dim": int(env.observation_space.shape[0]),
    }

    model_path = os.path.join(out_dir, "model.zip")
    model.save(model_path)
    with open(os.path.join(out_dir, "train_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    _log(f"FactorValue DRL 完成: mean_reward={meta['mean_reward']:.4f}, "
         f"weights={[f'{w:.3f}' for w in final_weights]}")
    return meta


# ============================================================
# DRL 目标计划生成: 用 final_weights × v_universe_snapshot 重打分 -> TopN
# ============================================================
FACTOR_TO_VIEW = {
    "signal": "f_signal",
    "trend": "f_trend",
    "govern": "f_govern",
    "liquidity": "f_liquidity",
    "vol": "f_vol",
    "mom_rev": "f_mom_rev",
}


def _build_target_plan(day: str, day_dir: str,
                       final_weights: dict[str, float],
                       top_n: int = MAX_STOCKS) -> dict:
    """读 v_universe_snapshot, 用 DRL final_weights 重打分, 写 target_plan.json.

    v_universe_snapshot 由 build_factor_views.py 物化, 含 canon / composite_score /
    signal / pass_basic. 但 composite_score 是用静态 SCORE_WEIGHTS 算的;
    这里改成 DRL final_weights, 体现"PPO 学到的权重"对选股的影响.

    Returns
    -------
    dict: {"ok": bool, "path": str, "top_n": int, "universe_size": int,
           "method": str, "error": str|None}
    """
    import duckdb
    out_dir = os.path.join(DATA_DIR, "drl", day_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "target_plan.json")

    # 归一化权重, 避免全是 0
    w_total = sum(max(0.0, float(v)) for v in final_weights.values()) or 1.0
    norm_w = {k: max(0.0, float(v)) / w_total for k, v in final_weights.items()}

    # (2026-09-07) 信号权重硬边界 [0.10, 0.39], 防止极端参数跳跃
    _SIGNAL_WEIGHT_MIN = 0.10
    _SIGNAL_WEIGHT_MAX = 0.39
    if "signal" in norm_w:
        orig_signal = norm_w["signal"]
        norm_w["signal"] = max(_SIGNAL_WEIGHT_MIN, min(_SIGNAL_WEIGHT_MAX, norm_w["signal"]))
        if abs(norm_w["signal"] - orig_signal) > 1e-6:
            # 约束后需要重新归一化其余权重
            _excess = orig_signal - norm_w["signal"]  # 被扣减的部分
            _others = {k: v for k, v in norm_w.items() if k != "signal"}
            _other_total = sum(_others.values()) or 1.0
            for k in _others:
                norm_w[k] = _others[k] / _other_total * (1.0 - norm_w["signal"])
            _log(f"signal 权重约束: {orig_signal:.4f} → {norm_w['signal']:.4f} "
                 f"(边界 [{_SIGNAL_WEIGHT_MIN}, {_SIGNAL_WEIGHT_MAX}])")

    res: dict = {
        "ok": False, "path": out_path,
        "top_n": 0, "universe_size": 0,
        "method": "drl_final_weights x v_universe_snapshot",
        "weights_used": norm_w,
        "error": None,
    }

    if not os.path.exists(DUCKDB_PATH):
        res["error"] = "DuckDB 不存在"
        return res

    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
    except Exception as e:
        res["error"] = f"DuckDB 打开失败: {e}"
        return res

    try:
        # 优先用 v_factor_scores_daily 取最新一日的全 A 6 因子分,
        # 再 LEFT JOIN daily_bars 取价/量. 该视图由 build_factor_views.py 物化.
        fsd_exists = con.execute(
            "SELECT 1 FROM duckdb_tables WHERE schema_name='main' "
            "AND table_name='v_factor_scores_daily'"
        ).fetchone()
        # symbols 表用于把纯6位补成 canon (带 .SH/.SZ/.BJ 后缀)
        sym_exists = con.execute(
            "SELECT 1 FROM duckdb_tables WHERE schema_name='main' "
            "AND table_name='symbols'"
        ).fetchone()
        sym_join = (
            "LEFT JOIN (SELECT symbol, market FROM symbols WHERE is_active) s "
            "ON s.symbol = v.canon"
            if sym_exists else ""
        )
        if fsd_exists:
            df = con.execute(f"""
                SELECT
                    CASE
                        WHEN s.market = 'sh' THEN v.canon || '.SH'
                        WHEN s.market = 'sz' THEN v.canon || '.SZ'
                        WHEN s.market = 'bj' THEN v.canon || '.BJ'
                        ELSE v.canon
                    END AS canon,
                    v.date, b.close, b.change_pct, b.turnover, b.amount,
                    v.f_signal, v.f_trend, v.f_govern, v.f_liquidity, v.f_vol, v.f_mom_rev,
                    CASE
                        WHEN v.f_trend > 0 AND v.f_signal > 0 THEN 'BUY'
                        WHEN v.f_trend < 0 AND v.f_signal < 0 THEN 'SELL'
                        ELSE 'HOLD'
                    END AS signal
                FROM v_factor_scores_daily v
                LEFT JOIN daily_bars b
                    ON b.symbol = v.canon AND b.date = v.date
                {sym_join}
                WHERE v.date = (SELECT MAX(date) FROM v_factor_scores_daily)
            """).df()
        else:
            # 物化表不在 -> 用 daily_bars 现算精简版 (0/1/turnover 近似)
            _log("v_factor_scores_daily 不存在, 用 daily_bars 现算精简版")
            sym_join2 = (
                "LEFT JOIN (SELECT symbol, market FROM symbols WHERE is_active) s "
                "ON s.symbol = b.symbol"
                if sym_exists else ""
            )
            df = con.execute(f"""
                SELECT
                    CASE
                        WHEN s.market = 'sh' THEN b.symbol || '.SH'
                        WHEN s.market = 'sz' THEN b.symbol || '.SZ'
                        WHEN s.market = 'bj' THEN b.symbol || '.BJ'
                        ELSE b.symbol
                    END AS canon,
                    b.date, b.close, b.change_pct, b.turnover, b.amount,
                    change_pct / 10.0 AS f_signal,
                    change_pct / 10.0 AS f_trend,
                    0.0 AS f_govern,
                    COALESCE(b.turnover, 0) AS f_liquidity,
                    0.0 AS f_vol,
                    0.0 AS f_mom_rev,
                    'HOLD' AS signal
                FROM daily_bars b
                {sym_join2}
                WHERE b.date = (SELECT MAX(date) FROM daily_bars)
            """).df()
    finally:
        try:
            con.close()
        except Exception:
            pass

    if df is None or df.empty:
        res["error"] = "候选池为空"
        return res

    res["universe_size"] = int(len(df))

    # 用 DRL final_weights 重打分
    score = np.zeros(len(df), dtype=np.float64)
    for factor, col in FACTOR_TO_VIEW.items():
        w = norm_w.get(factor, 0.0)
        if col in df.columns and w:
            v = df[col].astype(float).fillna(0.0).to_numpy()
            score += w * v
    df["drl_score"] = score
    df = df.sort_values("drl_score", ascending=False).head(int(top_n))

    # 构造 top_n, 权重等权 (后续可在 run_daily 处调整)
    items = []
    weight_each = 1.0 / max(1, len(df))
    for _, r in df.iterrows():
        items.append({
            "canon": str(r["canon"]),
            "price": float(r["close"]) if pd.notna(r.get("close")) else None,
            "change_pct": float(r["change_pct"]) if pd.notna(r.get("change_pct")) else None,
            "turnover": float(r["turnover"]) if pd.notna(r.get("turnover")) else None,
            "drl_score": float(r["drl_score"]),
            "target_weight": round(weight_each, 6),
            "source_signal": str(r.get("signal")) if r.get("signal") is not None else None,
        })

    res["ok"] = True
    res["top_n"] = len(items)
    payload = {
        "day": day,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": res["method"],
        "weights_used": norm_w,
        "universe_size": res["universe_size"],
        "top_n": items,
        "note": "盘中 realtime_engine 优先消费此文件; 缺失时回退 selection.json",
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _log(f"DRL target_plan 已生成: {out_path} (top_n={len(items)}, universe={res['universe_size']})")
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="YYYY-MM-DD; 默认昨日")
    ap.add_argument("--timesteps", type=int, default=800)
    ap.add_argument("--cvar_alpha", type=float, default=None,
                    help="CVaR 尾部百分位 (默认 0.05, 从 config.CVAR_PPO 读取)")
    ap.add_argument("--cvar_coef", type=float, default=None,
                    help="CVaR 约束项权重 (默认 0.1, 从 config.CVAR_PPO 读取)")
    ap.add_argument("--ensemble", action="store_true",
                    help="使用多模态集成 (FactorIC + PriceAction + NeSyTA)")
    args = ap.parse_args()
    d = args.day or (dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d")

    if args.ensemble:
        from multimodal_ensemble import run_multimodal_train
        print(json.dumps(run_multimodal_train(d, total_timesteps=args.timesteps),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run_drl_train(d, total_timesteps=args.timesteps,
                                        cvar_alpha=args.cvar_alpha,
                                        cvar_coef=args.cvar_coef),
                         ensure_ascii=False, indent=2))