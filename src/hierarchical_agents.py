# -*- coding: utf-8 -*-
"""Hi-DARTS: 层次化多智能体交易框架.

参考: 2025 年 Hi-DARTS 框架, 用元智能体分析市场波动,
动态激活高频或低频交易子智能体.

核心设计:
  1. 元智能体 (MetaAgent): 分析市场状态, 选择子策略
  2. 子策略 1 — 日频 (DailyAgent): 基于因子打分, 日频调仓
  3. 子策略 2 — 周频 (WeeklyAgent): 基于趋势跟踪, 周频调仓
  4. 子策略 3 — 事件驱动 (EventAgent): 基于公告/新闻, 事件触发调仓
  5. 集成决策: 元智能体输出子策略权重, 加权融合最终决策
"""

from __future__ import annotations

import numpy as np
from typing import Any

# 子策略类型枚举
AGENT_DAILY = "daily"
AGENT_WEEKLY = "weekly"
AGENT_EVENT = "event"
ALL_AGENTS = [AGENT_DAILY, AGENT_WEEKLY, AGENT_EVENT]


# ===================================================================
# 子策略 1: 日频因子打分
# ===================================================================
class DailyAgent:
    """日频因子打分策略.

    基于因子融合打分, 每日输出 Top-K 标的权重.
    模拟现有 factor_fusion 的逻辑.
    """

    def __init__(self, top_k: int = 10):
        self.top_k = top_k
        self.name = AGENT_DAILY

    def act(self, scores: np.ndarray, symbols: list[str]) -> dict[str, float]:
        """根据因子打分输出权重.

        Args:
            scores: 各标的的因子融合分 (N,).
            symbols: 标的代码列表.

        Returns:
            {symbol: weight}, 归一化权重.
        """
        if len(scores) == 0:
            return {}
        # 取 Top-K
        idx = np.argsort(scores)[::-1][:self.top_k]
        selected = np.array(symbols)[idx]
        top_scores = scores[idx]
        weights = top_scores / (top_scores.sum() + 1e-8)
        return dict(zip(selected, weights.tolist()))


# ===================================================================
# 子策略 2: 周频趋势跟踪
# ===================================================================
class WeeklyAgent:
    """周频趋势跟踪策略.

    基于 20 日均线趋势, 周频输出偏多头/偏空头权重.
    """

    def __init__(self, top_k: int = 10, ma_short: int = 5, ma_long: int = 20):
        self.top_k = top_k
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.name = AGENT_WEEKLY
        self._momentum: dict[str, float] = {}

    def update_momentum(self, symbol: str, price_series: np.ndarray) -> float:
        """更新单标的动量信号.

        Returns:
            float: 动量得分 [-1, 1].
        """
        if len(price_series) < self.ma_long:
            return 0.0
        ma_s = price_series[-self.ma_short:].mean()
        ma_l = price_series[-self.ma_long:].mean()
        mom = (ma_s - ma_l) / (ma_l + 1e-8)
        mom = np.clip(mom, -1.0, 1.0)
        self._momentum[symbol] = float(mom)
        return float(mom)

    def act(self) -> dict[str, float]:
        """根据动量信号输出权重."""
        if not self._momentum:
            return {}
        scores = np.array(list(self._momentum.values()))
        syms = list(self._momentum.keys())
        # 只取正动量
        pos_mask = scores > 0
        if not pos_mask.any():
            return {}
        pos_scores = scores[pos_mask]
        pos_syms = np.array(syms)[pos_mask]
        idx = np.argsort(pos_scores)[::-1][:self.top_k]
        selected = pos_syms[idx]
        weights = np.ones(len(selected)) / len(selected)
        return dict(zip(selected, weights.tolist()))


# ===================================================================
# 子策略 3: 事件驱动
# ===================================================================
class EventAgent:
    """事件驱动策略.

    基于事件类型 (业绩预增/政策利好/股东增持) 调整权重.
    """

    def __init__(self, top_k: int = 10):
        self.top_k = top_k
        self.name = AGENT_EVENT
        # 事件缓存: {symbol: event_score}
        self._events: dict[str, float] = {}

    def register_event(self, symbol: str, event_type: str, intensity: float = 1.0) -> None:
        """注册事件.

        event_type: "earnings_surprise", "policy_catalyst", "buyback", "insider_buy"
        """
        base_scores = {
            "earnings_surprise": 0.8,
            "policy_catalyst": 0.6,
            "buyback": 0.4,
            "insider_buy": 0.3,
        }
        score = base_scores.get(event_type, 0.2) * intensity
        self._events[symbol] = self._events.get(symbol, 0.0) + score

    def clear_events(self) -> None:
        self._events.clear()

    def act(self) -> dict[str, float]:
        """根据事件得分输出权重."""
        if not self._events:
            return {}
        scores = np.array(list(self._events.values()))
        syms = list(self._events.keys())
        idx = np.argsort(scores)[::-1][:self.top_k]
        selected = np.array(syms)[idx]
        top_scores = scores[idx]
        weights = top_scores / (top_scores.sum() + 1e-8)
        return dict(zip(selected, weights.tolist()))


# ===================================================================
# 元智能体 (MetaAgent)
# ===================================================================
class MetaAgent:
    """元智能体: 分析市场状态, 动态选择子策略.

    输入: 市场状态特征 (波动率/趋势/成交量)
    输出: 各子策略的激活权重 [daily, weekly, event], 和为 1
    """

    def __init__(
        self,
        vol_threshold_low: float = 0.15,
        vol_threshold_high: float = 0.30,
        trend_threshold: float = 0.02,
    ):
        self.vol_threshold_low = vol_threshold_low
        self.vol_threshold_high = vol_threshold_high
        self.trend_threshold = trend_threshold
        # 默认权重: 偏日频
        self.weights = np.array([0.6, 0.3, 0.1], dtype=float)

    def analyze(
        self,
        annualized_vol: float = 0.2,
        trend_strength: float = 0.0,
        event_intensity: float = 0.0,
        regime: int = 0,
    ) -> dict[str, Any]:
        """分析市场状态, 更新子策略权重.

        Args:
            annualized_vol: 年化波动率.
            trend_strength: 趋势强度 [-1, 1].
            event_intensity: 事件密集度 [0, 1].
            regime: 市场状态 (0=震荡, 1=上升, 2=下降).

        Returns:
            dict: {agent_name: weight, ...}
        """
        daily_w = 0.4
        weekly_w = 0.3
        event_w = 0.3

        # 波动率调整: 高波动 → 降日频升周频
        if annualized_vol > self.vol_threshold_high:
            daily_w -= 0.2
            weekly_w += 0.2
        elif annualized_vol < self.vol_threshold_low:
            daily_w += 0.1
            weekly_w -= 0.1

        # 趋势调整: 强趋势 → 升周频
        if abs(trend_strength) > self.trend_threshold:
            if trend_strength > 0:
                weekly_w += 0.1
                daily_w -= 0.1
            else:
                daily_w += 0.1
                weekly_w -= 0.1

        # 事件密集度调整: 事件多 → 升事件驱动
        if event_intensity > 0.5:
            event_w += 0.2
            daily_w -= 0.1
            weekly_w -= 0.1

        # 市场状态调整
        if regime == 2:  # 下降趋势
            daily_w -= 0.1
            event_w += 0.1

        # 归一化
        weights = np.array([daily_w, weekly_w, event_w])
        weights = np.clip(weights, 0.0, 1.0)
        weights = weights / (weights.sum() + 1e-8)
        self.weights = weights

        return {
            AGENT_DAILY: float(weights[0]),
            AGENT_WEEKLY: float(weights[1]),
            AGENT_EVENT: float(weights[2]),
        }

    def fuse_actions(
        self,
        daily_action: dict[str, float],
        weekly_action: dict[str, float],
        event_action: dict[str, float],
    ) -> dict[str, float]:
        """根据元智能体权重融合子策略动作.

        Args:
            daily_action: 日频策略输出 {symbol: weight}.
            weekly_action: 周频策略输出.
            event_action: 事件驱动策略输出.

        Returns:
            {symbol: fused_weight}, 归一化权重.
        """
        # 收集所有股票
        all_symbols = set(daily_action) | set(weekly_action) | set(event_action)
        if not all_symbols:
            return {}

        fused = {}
        for sym in all_symbols:
            w = 0.0
            w += self.weights[0] * daily_action.get(sym, 0.0)
            w += self.weights[1] * weekly_action.get(sym, 0.0)
            w += self.weights[2] * event_action.get(sym, 0.0)
            fused[sym] = w

        # 归一化
        total = sum(fused.values()) or 1.0
        return {k: v / total for k, v in fused.items()}