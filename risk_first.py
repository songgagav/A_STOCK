# -*- coding: utf-8 -*-
"""Risk-First 架构：LLM 风险信号结构化约束层.

参考: 2026 FinRL-DeepSeek 研究发现, 标准 DRL 代理会忽略 LLM 的风险信号,
优先追逐价格动量. 本模块提供三层约束:

1. 方差过滤器 (Variance Filter)
   - 检测 LLM 情绪信号的异常突变 (超出滚动窗口的 N 倍标准差视为幻觉)
   - 输出 "LLM 信号置信度" 乘子, 方差过大时自动降权

2. 奖励惩罚 (Risk Penalty)
   - 当组合暴露超过阈值时, 在奖励函数中叠加惩罚项
   - 惩罚强度与暴露超限程度成正比

3. 确定性熔断 (Circuit Breaker)
   - 当尾部风险指标超过阈值时, 强制限制最大仓位比例
   - 与现有 PAPER.stop_loss / portfolio_drawdown 正交协作
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

_LOG = logging.getLogger("risk_first")


# ===================================================================
# 1. 方差过滤器 (Variance Filter)
# ===================================================================
class LLMVarianceFilter:
    """检测 LLM 情绪信号的异常突变, 输出信号置信度乘子.

    用滚动窗口 (默认 20 步) 的均值和标准差判断当前信号是否异常.
    异常判据: |signal - mean| > N * std (N 默认 2.5, 对应约 99% 置信区间).
    异常时置信度 = exp(-(|signal-mean|/std - N) / 2), 否则 = 1.0.
    """

    def __init__(
        self,
        window: int = 20,
        n_std: float = 2.5,
        min_confidence: float = 0.1,
    ):
        self.window = window
        self.n_std = n_std
        self.min_confidence = min_confidence
        self._history: list[float] = []

    def reset(self) -> None:
        self._history.clear()

    def update(self, signal_value: float) -> float:
        """输入最新 LLM 信号值, 返回置信度乘子 [min_confidence, 1.0].

        signal_value: 标量 (如 sentiment_score, 归一化到 0~1 或 -1~1).
        """
        self._history.append(signal_value)
        if len(self._history) < self.window:
            return 1.0

        arr = np.array(self._history[-self.window:], dtype=float)
        mean = float(arr.mean())
        std = float(arr.std()) + 1e-8
        deviation = abs(signal_value - mean) / std

        if deviation <= self.n_std:
            return 1.0

        # 指数衰减: 偏差越大置信度越低
        raw = float(np.exp(-(deviation - self.n_std) / 2.0))
        return max(self.min_confidence, raw)

    def state_dict(self) -> dict:
        return {"window": self.window, "n_std": self.n_std,
                "history": self._history[-self.window * 2:]}

    def load_state_dict(self, state: dict) -> None:
        self.window = state.get("window", self.window)
        self.n_std = state.get("n_std", self.n_std)
        self._history = list(state.get("history", []))


# ===================================================================
# 2. 风险暴露惩罚 (Risk Exposure Penalty)
# ===================================================================
class RiskExposurePenalty:
    """当组合在某些因子上的暴露超过阈值时, 计算奖励惩罚项.

    penalty = sum(max(0, |exposure_i| - threshold_i)^2 * coef_i)

    支持动态阈值: 可在运行时调整暴露上限.
    """

    def __init__(
        self,
        exposure_thresholds: dict[str, float] | None = None,
        penalty_coef: float = 0.5,
    ):
        """
        exposure_thresholds: {因子名: 暴露上限绝对值}.
            默认: signal=0.8, trend=0.8, liquidity=0.6, vol=0.6, mom_rev=0.5
        penalty_coef: 惩罚项系数 (乘以超出阈值的平方).
        """
        self.exposure_thresholds = exposure_thresholds or {
            "signal": 0.8, "trend": 0.8, "liquidity": 0.6,
            "vol": 0.6, "mom_rev": 0.5,
        }
        self.penalty_coef = penalty_coef

    def compute(self, weights: np.ndarray | list,
                factor_names: list[str] | None = None) -> float:
        """计算当前权重组合的风险暴露惩罚.

        Args:
            weights: 因子权重数组 (归一化到和为 1).
            factor_names: 因子名列表, 顺序与 weights 对应.

        Returns:
            float: 惩罚值 (非负). 纳入奖励时取负.
        """
        w = np.asarray(weights, dtype=float)
        if factor_names is None:
            factor_names = list(self.exposure_thresholds.keys())

        penalty = 0.0
        for i, name in enumerate(factor_names):
            if i >= len(w):
                break
            threshold = self.exposure_thresholds.get(name, 0.5)
            exposure = abs(w[i])
            if exposure > threshold:
                excess = (exposure - threshold) ** 2
                penalty += excess * self.penalty_coef

        return float(penalty)

    def compute_portfolio_penalty(
        self,
        portfolio_weights: list[float],
        concentration_limit: float = 0.15,
    ) -> float:
        """计算组合集中度惩罚 (单票权重超限).

        Args:
            portfolio_weights: 各标的权重列表.
            concentration_limit: 单票权重上限.

        Returns:
            float: 集中度惩罚值.
        """
        excess = sum(max(0.0, w - concentration_limit) ** 2
                     for w in portfolio_weights)
        return excess * self.penalty_coef * 2.0


# ===================================================================
# 3. 确定性熔断 (Circuit Breaker)
# ===================================================================
class CircuitBreaker:
    """当实时风险指标超过阈值时, 强制限制仓位.

    熔断级别:
        level=0: 正常
        level=1: 警告 (限制新开仓)
        level=2: 熔断 (强制减仓至安全比例)
        level=3: 紧急熔断 (强制清仓)

    熔断条件:
        - 组合回撤触发: portfolio_drawdown > drawdown_threshold
        - 波动率触发: 20日年化波动率 > vol_threshold
        - 尾部风险触发: CVaR(95%) > cvar_threshold
        - 最大回撤加速: 近5日回撤变化 > drawdown_acceleration
    """

    def __init__(
        self,
        drawdown_threshold: float = 0.08,
        vol_threshold: float = 0.35,
        cvar_threshold: float = 0.05,
        drawdown_acceleration: float = 0.03,
    ):
        self.drawdown_threshold = drawdown_threshold
        self.vol_threshold = vol_threshold
        self.cvar_threshold = cvar_threshold
        self.drawdown_acceleration = drawdown_acceleration
        self._level = 0
        self._triggered: list[str] = []

    @property
    def level(self) -> int:
        return self._level

    @property
    def triggered(self) -> list[str]:
        return self._triggered

    def reset(self) -> None:
        self._level = 0
        self._triggered.clear()

    def evaluate(
        self,
        portfolio_drawdown: float,
        annualized_vol: float | None = None,
        cvar_95: float | None = None,
        drawdown_5d_change: float | None = None,
    ) -> int:
        """评估当前风险状态, 返回熔断级别.

        Args:
            portfolio_drawdown: 当前组合回撤 (小数, 正数, 如 0.08 = 8%).
            annualized_vol: 20日年化波动率 (小数).
            cvar_95: 95% CVaR (小数, 正数).
            drawdown_5d_change: 近5日回撤变化 (小数, 正数 = 恶化).

        Returns:
            int: 熔断级别 (0, 1, 2, 3).
        """
        self._triggered.clear()

        if portfolio_drawdown >= self.drawdown_threshold * 1.5:
            self._level = 3
            self._triggered.append("drawdown_extreme")
            return 3

        if portfolio_drawdown >= self.drawdown_threshold:
            self._triggered.append("drawdown")

        if annualized_vol is not None and annualized_vol > self.vol_threshold:
            self._triggered.append("volatility")

        if cvar_95 is not None and cvar_95 > self.cvar_threshold:
            self._triggered.append("cvar")

        if (drawdown_5d_change is not None
                and drawdown_5d_change > self.drawdown_acceleration):
            self._triggered.append("drawdown_acceleration")

        n_triggers = len(self._triggered)
        if n_triggers >= 3:
            self._level = 3
        elif n_triggers >= 2:
            self._level = 2
        elif n_triggers >= 1:
            self._level = 1
        else:
            self._level = 0

        return self._level

    def get_position_limit(self) -> float:
        """根据熔断级别返回最大仓位比例限制.

        Returns:
            float: 最大仓位比例 (0.0 ~ 1.0).
        """
        limits = {0: 1.0, 1: 0.7, 2: 0.3, 3: 0.0}
        return limits.get(self._level, 0.5)


# ===================================================================
# 4. 综合 Risk-First 层
# ===================================================================
class RiskFirstLayer:
    """Risk-First 综合层: 方差过滤器 + 风险暴露惩罚 + 确定性熔断.

    在 DRL 训练和推理时, 作为 pre-step / post-step 回调注入.
    """

    def __init__(
        self,
        variance_filter: LLMVarianceFilter | None = None,
        exposure_penalty: RiskExposurePenalty | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        llm_signal_dim: int = 4,
    ):
        self.variance_filter = variance_filter or LLMVarianceFilter()
        self.exposure_penalty = exposure_penalty or RiskExposurePenalty()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.llm_signal_dim = llm_signal_dim
        self._filter_history: list[float] = []

    def reset(self) -> None:
        self.variance_filter.reset()
        self._filter_history.clear()

    def filter_llm_signals(
        self, sentiment_vec: np.ndarray | list
    ) -> tuple[np.ndarray, float]:
        """对 LLM 情绪信号做方差过滤, 返回 (过滤后信号, 平均置信度).

        Args:
            sentiment_vec: 情绪因子向量 (4 维).

        Returns:
            (np.ndarray, float): 过滤后的信号, 平均置信度乘子.
        """
        vec = np.asarray(sentiment_vec, dtype=float).flatten()
        if len(vec) == 0:
            return vec, 1.0

        confidences = []
        filtered = vec.copy()
        for i in range(len(vec)):
            conf = self.variance_filter.update(vec[i])
            confidences.append(conf)
            filtered[i] = vec[i] * conf

        self._filter_history.append(float(np.mean(confidences)))
        return filtered, float(np.mean(confidences))

    def step_reward_penalty(
        self,
        weights: np.ndarray | list,
        portfolio_weights: list[float] | None = None,
        factor_names: list[str] | None = None,
        llm_confidence: float = 1.0,
    ) -> float:
        """计算奖励惩罚项 (纳入奖励时取负).

        penalty = 暴露惩罚 + 集中度惩罚 + LLM 低置信惩罚

        Args:
            weights: 因子权重数组.
            portfolio_weights: 组合中各标的权重列表 (可选).
            factor_names: 因子名列表.
            llm_confidence: LLM 信号平均置信度 [0, 1].

        Returns:
            float: 总惩罚值 (非负).
        """
        total = self.exposure_penalty.compute(weights, factor_names)
        if portfolio_weights:
            total += self.exposure_penalty.compute_portfolio_penalty(
                portfolio_weights)

        # LLM 低置信惩罚: 置信度越低惩罚越大
        if llm_confidence < 0.5:
            total += (1.0 - llm_confidence) * 0.5

        return total

    def circuit_break_check(
        self,
        portfolio_drawdown: float,
        annualized_vol: float | None = None,
        cvar_95: float | None = None,
        drawdown_5d_change: float | None = None,
    ) -> int:
        """执行熔断检查, 返回熔断级别."""
        return self.circuit_breaker.evaluate(
            portfolio_drawdown, annualized_vol, cvar_95, drawdown_5d_change)

    def state_dict(self) -> dict:
        return {
            "variance_filter": self.variance_filter.state_dict(),
            "filter_history": self._filter_history[-50:],
            "circuit_level": self.circuit_breaker.level,
            "circuit_triggers": self.circuit_breaker.triggered,
        }

    def apply_position_limit(self, target_weights: np.ndarray) -> np.ndarray:
        """根据熔断级别调整目标权重.

        Args:
            target_weights: 原始目标权重 (和为 1).

        Returns:
            np.ndarray: 调整后的权重.
        """
        limit = self.circuit_breaker.get_position_limit()
        if limit >= 1.0:
            return target_weights

        # 限制最大仓位, 剩余部分分配为现金
        clipped = np.clip(target_weights, 0.0, limit)
        s = clipped.sum()
        if s > 0:
            return clipped / s
        return clipped