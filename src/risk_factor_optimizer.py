# -*- coding: utf-8 -*-
"""风险因子 PPO 动态优化 (Risk Factor PPO).

参考: 2025 年量化研究报告用 PPO 动态优化风险因子生成, 解释度提升至 35.3%,
因子时序更稳定.

落地方式:
  1. 从全 A 平均收益序列提取滚动风险因子 (波动率 / CVaR95 / 最大回撤 / 下行波动)
  2. 用 RiskFactorAugmentedEnv 把风险因子并入 FactorValueEnv 观测与状态,
     让 CVaR_PPO 在决策时能看到并利用当前市场风险状态
  3. 提供风险因子的扩张标准化 (无未来函数) 与稳定性/解释度评估指标:
     - weight_stability_metrics : 动态权重的时序稳定性 (换手/自相关)
     - risk_explained_ratio    : 风险因子对因子收益的解释度 (R²)
"""

from __future__ import annotations

import os
import sys

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from gymnasium import spaces  # noqa: E402
from drl_train import FactorValueEnv  # noqa: E402  (CVaR_PPO 由调用方按需引入)

# 风险因子清单: 波动率 / CVaR95 / 最大回撤 / 下行波动
RISK_FACTOR_NAMES = ["vol", "cvar95", "mdd", "downside_dev"]


# ===================================================================
# 风险因子计算
# ===================================================================
def compute_risk_factors(
    returns: np.ndarray,
    window: int = 20,
    cvar_alpha: float = 0.05,
) -> np.ndarray:
    """从收益序列计算逐日滚动风险因子.

    Args:
        returns: 全 A 平均收益序列 (T,).
        window: 滚动窗口长度.
        cvar_alpha: CVaR 尾部分位 (默认 0.05 = 95% CVaR, 取最差 5%).

    Returns:
        ndarray (T, 4): 列序见 RISK_FACTOR_NAMES.
    """
    returns = np.asarray(returns, dtype=np.float64)
    n = len(returns)
    out = np.zeros((n, len(RISK_FACTOR_NAMES)), dtype=np.float64)
    for i in range(n):
        lo = max(0, i - window + 1)
        seg = returns[lo:i + 1]
        seg = seg[np.isfinite(seg)]
        if len(seg) < 3:
            continue
        # 1) 滚动波动率 (日频)
        out[i, 0] = float(seg.std(ddof=1))
        # 2) CVaR95 = -最差 alpha 尾部均值
        k = max(1, int(np.ceil(cvar_alpha * len(seg))))
        out[i, 1] = -float(np.sort(seg)[:k].mean())
        # 3) 滚动最大回撤 (窗口内峰谷比)
        peak = np.maximum.accumulate(seg)
        dd = np.max((peak - seg) / np.maximum(peak, 1e-12))
        out[i, 2] = float(dd) if np.isfinite(dd) else 0.0
        # 4) 下行波动 (负收益平方均值开根)
        neg = seg[seg < 0]
        out[i, 3] = float(np.sqrt(np.mean(neg ** 2))) if len(neg) > 0 else 0.0
    return out


def expanding_zscore(x: np.ndarray, warmup: int = 10) -> np.ndarray:
    """扩张标准化: 第 t 日均值/标准差只用 0..t 的数据, 避免未来函数.

    样本太少或方差为 0 时输出 0, 结果裁剪到 [-5, 5].
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    out = np.zeros(n, dtype=np.float64)
    for i in range(n):
        seg = x[:i + 1]
        seg = seg[np.isfinite(seg)]
        if len(seg) < max(3, warmup):
            continue
        m = float(seg.mean())
        s = float(seg.std(ddof=1))
        out[i] = 0.0 if s < 1e-12 else (x[i] - m) / s
    return np.clip(out, -5.0, 5.0)


# ===================================================================
# 风险因子提取器
# ===================================================================
class RiskFactorExtractor:
    """维护风险因子原始值 + 扩张标准化值, 提供任意时点查询."""

    def __init__(
        self,
        returns: np.ndarray,
        window: int = 20,
        cvar_alpha: float = 0.05,
    ):
        self.returns = np.asarray(returns, dtype=np.float64)
        self.window = max(3, int(window))
        self.cvar_alpha = cvar_alpha
        self.raw = compute_risk_factors(self.returns, self.window, cvar_alpha)
        self.normalized = np.column_stack([
            expanding_zscore(self.raw[:, j]) for j in range(self.raw.shape[1])
        ]).astype(np.float32)
        self.names = list(RISK_FACTOR_NAMES)

    def raw_at(self, idx: int) -> np.ndarray:
        idx = int(np.clip(idx, 0, len(self.raw) - 1))
        return self.raw[idx].astype(np.float32)

    def norm_at(self, idx: int) -> np.ndarray:
        idx = int(np.clip(idx, 0, len(self.normalized) - 1))
        return self.normalized[idx].astype(np.float32)


# ===================================================================
# RiskFactorAugmentedEnv: 风险因子并入 FactorValueEnv
# ===================================================================
class RiskFactorAugmentedEnv(FactorValueEnv):
    """在 FactorValueEnv 基础上把市场风险因子并入观测.

    观测 = 原始 FactorValueEnv 观测 (因子值 + 情绪 + stance + regime)
           + 当前时点风险因子 (vol/cvar95/mdd/downside_dev, 扩张标准化).

    用于让 CVaR_PPO 在动态权重分配时感知市场风险状态 (波动率/CVaR/回撤),
    风险因子序列更稳定地参与决策.
    """

    def __init__(
        self,
        factor_history: np.ndarray,
        future_returns: np.ndarray,
        returns: np.ndarray,
        brief: dict | None = None,
        lookback: int = 5,
        regime_features: np.ndarray | None = None,
        risk_first_layer=None,
        factor_names: list[str] | None = None,
        risk_factor_window: int = 20,
        cvar_alpha: float = 0.05,
    ):
        super().__init__(
            factor_history, future_returns, returns,
            brief=brief, lookback=lookback,
            regime_features=regime_features,
            risk_first_layer=risk_first_layer,
            factor_names=factor_names,
        )
        self._rf_extractor = RiskFactorExtractor(
            returns, window=risk_factor_window, cvar_alpha=cvar_alpha)
        self.risk_factor_window = risk_factor_window
        self.risk_factor_names = list(self._rf_extractor.names)

        base_dim = int(self.observation_space.shape[0])
        k = self.risk_factor_names.__len__()
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf,
            shape=(base_dim + k,), dtype=np.float32,
        )

    def _state(self) -> np.ndarray:
        base = super()._state()
        # 观测最后一行对应市场日 self.t-1; 风险因子取同一市场日, 无未来函数
        idx = int(np.clip(self.t - 1, 0, len(self._rf_extractor.raw) - 1))
        risk_part = self._rf_extractor.norm_at(idx)
        return np.concatenate([base, risk_part]).astype(np.float32)

    def step(self, action):
        """父类 step 基础上, 把当前风险因子写入 info 供上层消费."""
        obs, reward, done, truncated, info = super().step(action)
        idx = int(np.clip(self.t - 1, 0, len(self._rf_extractor.raw) - 1))
        info["risk_factors"] = self._rf_extractor.norm_at(idx).tolist()
        return obs, reward, bool(done), bool(truncated), info

    @property
    def risk_obs_dim(self) -> int:
        return int(self._rf_extractor.normalized.shape[1])


# ===================================================================
# 稳定性 / 解释度评估
# ===================================================================
def weight_stability_metrics(weight_matrix: np.ndarray) -> dict[str, float]:
    """动态因子权重时序稳定性.

    Args:
        weight_matrix: (T, n_factors) 逐日权重 (行和=1).

    Returns:
        dict: {
            "mean_autocorr": 权重序列一阶自相关 (越高越稳),
            "mean_turnover": 平均单日换手 (0.5*Σ|Δw|, 越低越稳),
            "stability_score": 综合稳定分 = mean_autocorr / (1 + turnover).
        }
    """
    W = np.asarray(weight_matrix, dtype=np.float64)
    if W.ndim != 2 or len(W) < 3:
        return {"mean_autocorr": 1.0, "mean_turnover": 0.0, "stability_score": 1.0}

    diffs = np.abs(np.diff(W, axis=0)).sum(axis=1)
    turnover = float(np.mean(diffs) * 0.5)

    autocorrs = []
    for j in range(W.shape[1]):
        col = W[:, j]
        s = col.std()
        if s < 1e-12:
            autocorrs.append(1.0)   # 恒定权重视为完全稳定
            continue
        c = np.corrcoef(col[:-1], col[1:])[0, 1]
        autocorrs.append(float(c) if np.isfinite(c) else 0.0)
    mean_autocorr = float(np.mean(autocorrs)) if autocorrs else 1.0

    return {
        "mean_autocorr": round(mean_autocorr, 4),
        "mean_turnover": round(turnover, 6),
        "stability_score": round(mean_autocorr / (1.0 + turnover), 4),
    }


def risk_explained_ratio(
    factor_returns: np.ndarray,
    risk_factors: np.ndarray,
) -> dict:
    """风险因子对因子收益的解释度 (R², 多元线性回归).

    对应 2025 报告"PPO 动态优化风险因子后解释度提升"的可复现口径:
    对每个候选因子收益序列, 用风险因子向量做线性回归, 返回 R² 分布.

    Args:
        factor_returns: (T, n_factors) 候选因子收益.
        risk_factors:   (T, K) 风险因子 (建议用扩张标准化矩阵).

    Returns:
        dict: {"mean_r2": float, "per_factor_r2": list[float]}.
    """
    Y = np.asarray(factor_returns, dtype=np.float64)
    X = np.asarray(risk_factors, dtype=np.float64)
    if Y.ndim != 2 or X.ndim != 2 or len(Y) != len(X):
        return {"mean_r2": 0.0, "per_factor_r2": []}
    T = len(Y)
    X1 = np.column_stack([np.ones(T), X])
    r2s = []
    try:
        beta, *_ = np.linalg.lstsq(X1, Y, rcond=None)
        pred = X1 @ beta
        for j in range(Y.shape[1]):
            y = Y[:, j]
            ss_res = float(np.sum((y - pred[:, j]) ** 2))
            ss_tot = float(np.sum((y - y.mean()) ** 2))
            r2s.append(1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0)
    except np.linalg.LinAlgError:
        r2s = []
    return {
        "mean_r2": round(float(np.mean(r2s)), 4) if r2s else 0.0,
        "per_factor_r2": [round(v, 4) for v in r2s],
    }


# ===================================================================
# 环境工厂 (供 CVaR_PPO 训练入口复用)
# ===================================================================
def make_risk_factor_env(
    factor_history: np.ndarray,
    future_returns: np.ndarray,
    returns: np.ndarray,
    brief: dict | None = None,
    lookback: int = 5,
    regime_features: np.ndarray | None = None,
    risk_first_layer=None,
    factor_names: list[str] | None = None,
    risk_factor_window: int = 20,
    cvar_alpha: float = 0.05,
) -> RiskFactorAugmentedEnv:
    """构造带风险因子观测的 FactorValueEnv 子类."""
    return RiskFactorAugmentedEnv(
        factor_history, future_returns, returns,
        brief=brief, lookback=lookback,
        regime_features=regime_features,
        risk_first_layer=risk_first_layer,
        factor_names=factor_names,
        risk_factor_window=risk_factor_window,
        cvar_alpha=cvar_alpha,
    )
