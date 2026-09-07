# -*- coding: utf-8 -*-
"""StockMARL: 多智能体模拟学习.

参考: 2025 年 StockMARL, 让 RL 智能体通过观察多种模拟投资者行为
(日内交易、动量追逐、风险厌恶) 来学习交易策略.

核心设计:
  1. 异质智能体模拟器 (HeterogeneousAgentSim): 管理多种模拟投资者
  2. 智能体类型:
     - IntradayTrader: 日内交易者 (高频, 均值回归)
     - MomentumChaser: 动量追逐者 (追涨杀跌)
     - RiskAverse: 风险厌恶者 (保守, 低仓位)
     - ValueInvestor: 价值投资者 (低PB高ROE)
  3. DRL 策略通过观察这些智能体的行为 + 市场状态, 学习适应策略
"""

from __future__ import annotations

import numpy as np
from typing import Any


# ===================================================================
# 异质智能体基类
# ===================================================================
class HeterogeneousAgent:
    """模拟投资者基类."""

    def __init__(self, name: str, cash: float = 100000.0):
        self.name = name
        self.cash = cash
        self.positions: dict[str, float] = {}
        self.trade_history: list[dict] = []

    def act(self, *args, **kwargs) -> dict[str, float]:
        """返回 {symbol: target_weight}."""
        raise NotImplementedError

    def record_trade(self, symbol: str, qty: int, price: float) -> None:
        self.trade_history.append({
            "symbol": symbol, "qty": qty, "price": price,
            "agent": self.name,
        })

    def get_state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cash": self.cash,
            "n_positions": len(self.positions),
            "n_trades": len(self.trade_history),
        }


# ===================================================================
# 日内交易者
# ===================================================================
class IntradayTrader(HeterogeneousAgent):
    """日内交易者: 高频均值回归, 偏好高换手率."""

    def __init__(self, cash: float = 100000.0, mean_reversion_window: int = 5):
        super().__init__("intraday_trader", cash)
        self.mean_reversion_window = mean_reversion_window

    def act(self, price_returns: dict[str, np.ndarray]) -> dict[str, float]:
        """基于均值回归策略.

        Args:
            price_returns: {symbol: 收益率序列}.

        Returns:
            {symbol: target_weight}.
        """
        weights = {}
        for symbol, rets in price_returns.items():
            if len(rets) < self.mean_reversion_window:
                continue
            recent = rets[-self.mean_reversion_window:]
            mean_ret = float(recent.mean())
            std_ret = float(recent.std()) + 1e-8
            # 均值回归信号: 最近收益偏离均值越远, 反向交易力度越大
            latest = float(rets[-1])
            z_score = (latest - mean_ret) / std_ret
            signal = -z_score * 0.1  # 反向
            weights[symbol] = max(0.0, signal)

        total = sum(weights.values()) or 1.0
        return {k: v / total for k, v in weights.items()}


# ===================================================================
# 动量追逐者
# ===================================================================
class MomentumChaser(HeterogeneousAgent):
    """动量追逐者: 追涨杀跌, 偏好短期动量."""

    def __init__(self, cash: float = 100000.0, momentum_window: int = 10):
        super().__init__("momentum_chaser", cash)
        self.momentum_window = momentum_window

    def act(self, price_returns: dict[str, np.ndarray]) -> dict[str, float]:
        """基于动量策略.

        Returns:
            {symbol: target_weight}.
        """
        weights = {}
        for symbol, rets in price_returns.items():
            if len(rets) < self.momentum_window:
                continue
            mom = float(rets[-self.momentum_window:].mean())
            if mom > 0:
                weights[symbol] = mom * 2.0  # 追涨

        if not weights:
            return {}
        total = sum(weights.values()) or 1.0
        return {k: v / total for k, v in weights.items()}


# ===================================================================
# 风险厌恶者
# ===================================================================
class RiskAverse(HeterogeneousAgent):
    """风险厌恶者: 保守, 低仓位, 偏好低波动股票."""

    def __init__(self, cash: float = 100000.0, max_positions: int = 5):
        super().__init__("risk_averse", cash)
        self.max_positions = max_positions

    def act(self, price_returns: dict[str, np.ndarray],
            volatilities: dict[str, float] | None = None) -> dict[str, float]:
        """基于低波动策略.

        Returns:
            {symbol: target_weight}.
        """
        vol_dict = volatilities or {}
        candidates = []
        for symbol, rets in price_returns.items():
            if len(rets) < 20:
                continue
            vol = vol_dict.get(symbol, float(rets[-20:].std()))
            # 低波动 + 正收益
            ret = float(rets[-20:].mean())
            if ret > 0 and vol < 0.03:
                candidates.append((symbol, 1.0 / (vol + 1e-8)))

        candidates.sort(key=lambda x: x[1], reverse=True)
        selected = candidates[:self.max_positions]
        weights = {s: w for s, w in selected}
        total = sum(weights.values()) or 1.0
        return {k: v / total for k, v in weights.items()}


# ===================================================================
# 价值投资者
# ===================================================================
class ValueInvestor(HeterogeneousAgent):
    """价值投资者: 偏好低PB高ROE的股票."""

    def __init__(self, cash: float = 100000.0, top_k: int = 10):
        super().__init__("value_investor", cash)
        self.top_k = top_k

    def act(self, fundamentals: dict[str, dict[str, float]]) -> dict[str, float]:
        """基于基本面策略.

        Args:
            fundamentals: {symbol: {"pb": float, "roe": float, ...}}.

        Returns:
            {symbol: target_weight}.
        """
        scores = {}
        for symbol, f in fundamentals.items():
            pb = f.get("pb", 1.0)
            roe = f.get("roe", 0.0)
            if pb <= 0:
                continue
            # 价值得分 = ROE / PB
            score = roe / pb
            scores[symbol] = max(0.0, score)

        if not scores:
            return {}
        sorted_syms = sorted(scores, key=scores.get, reverse=True)[:self.top_k]
        weights = {s: scores[s] for s in sorted_syms}
        total = sum(weights.values()) or 1.0
        return {k: v / total for k, v in weights.items()}


# ===================================================================
# 异质智能体模拟器
# ===================================================================
class HeterogeneousAgentSim:
    """异质智能体模拟器.

    管理多种模拟投资者, 计算市场参与者行为的综合影响.
    """

    def __init__(self):
        self.agents: list[HeterogeneousAgent] = [
            IntradayTrader(),
            MomentumChaser(),
            RiskAverse(),
            ValueInvestor(),
        ]
        self._action_history: list[dict] = []

    @property
    def agent_names(self) -> list[str]:
        return [a.name for a in self.agents]

    def step(
        self,
        price_returns: dict[str, np.ndarray],
        fundamentals: dict[str, dict[str, float]] | None = None,
        volatilities: dict[str, float] | None = None,
    ) -> dict[str, dict[str, float]]:
        """执行一步模拟, 所有智能体输出动作.

        Args:
            price_returns: {symbol: 收益率序列}.
            fundamentals: {symbol: {pb, roe, ...}}.
            volatilities: {symbol: 波动率}.

        Returns:
            {agent_name: {symbol: weight}}.
        """
        actions = {}
        for agent in self.agents:
            try:
                if isinstance(agent, (IntradayTrader, MomentumChaser)):
                    a = agent.act(price_returns)
                elif isinstance(agent, RiskAverse):
                    a = agent.act(price_returns, volatilities)
                elif isinstance(agent, ValueInvestor):
                    a = agent.act(fundamentals or {})
                else:
                    a = {}
                actions[agent.name] = a
            except Exception:
                actions[agent.name] = {}

        self._action_history.append(actions)
        return actions

    def get_consensus_signal(self, actions: dict[str, dict[str, float]]) -> dict[str, float]:
        """计算各智能体的共识信号.

        对每个股票, 计算有多少比例的智能体选择了它.
        """
        if not actions:
            return {}
        n_agents = len(self.agents)
        signal = {}
        for agent_name, action in actions.items():
            for sym, w in action.items():
                if w > 0:
                    signal[sym] = signal.get(sym, 0.0) + 1.0 / n_agents
        return signal

    def get_herding_index(self, actions: dict[str, dict[str, float]]) -> float:
        """计算羊群效应指数: 智能体之间的行动一致性."""
        if not actions:
            return 0.0
        all_syms = set()
        for a in actions.values():
            all_syms.update(a.keys())
        if not all_syms:
            return 0.0

        # 每只股票被多少智能体选中
        counts = {s: 0 for s in all_syms}
        for a in actions.values():
            for s in a:
                counts[s] = counts.get(s, 0) + 1

        n_agents = len(self.agents)
        if n_agents <= 1:
            return 0.0
        # 平均一致率
        mean_count = np.mean(list(counts.values())) if counts else 0
        return float(mean_count / n_agents)

    def get_state_vector(self, n_symbols: int = 10) -> np.ndarray:
        """输出模拟器状态向量 (嵌入 DRL 观测).

        Returns:
            ndarray: [herding_index, n_agent_types, avg_position_ratio, ...]
        """
        if not self._action_history:
            return np.zeros(4, dtype=np.float32)

        last = self._action_history[-1]
        herding = self.get_herding_index(last)
        n_active = sum(1 for a in last.values() if a)
        avg_positions = np.mean([len(a) for a in last.values()]) if last else 0

        return np.array([
            float(herding),
            float(n_active) / len(self.agents),
            float(avg_positions) / 10.0,
            float(len(self._action_history)),
        ], dtype=np.float32)