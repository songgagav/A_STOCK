# -*- coding: utf-8 -*-
"""可解释强化学习 + 自适应特征选择.

参考: 2025 年 A 股实证 (000063) 实现 88.80% 累计收益, 超越 DQN 基线 20.76%.

核心功能:
  1. 特征重要性追踪 (FeatureImportanceTracker): 记录每个特征对决策的贡献
  2. 自适应特征选择 (AdaptiveFeatureSelector): 根据 IC 表现动态选择特征
  3. 决策可追溯 (DecisionTrace): 记录每次决策的关键因素
  4. 可解释性约束: 在状态空间中增加约束, 让策略决策可追溯
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

import numpy as np


# ===================================================================
# 特征重要性追踪
# ===================================================================
class FeatureImportanceTracker:
    """追踪每个特征对决策的贡献.

    通过记录特征值与动作的相关性, 近似计算特征重要性.
    """

    def __init__(self, feature_names: list[str]):
        self.feature_names = feature_names
        self.n_features = len(feature_names)
        # {feature_name: [importance_scores]}
        self._history: dict[str, list[float]] = defaultdict(list)
        self._total_steps = 0

    def record(
        self,
        obs: np.ndarray,
        action: np.ndarray,
        reward: float,
    ) -> None:
        """记录一步观测-动作-奖励, 更新特征重要性.

        Args:
            obs: 观测向量 (n_features,).
            action: 动作向量.
            reward: 奖励标量.
        """
        self._total_steps += 1
        obs = np.asarray(obs, dtype=float).flatten()
        action = np.asarray(action, dtype=float).flatten()

        # 特征重要性近似: |obs_i * action_mean| * |reward|
        mean_action = float(np.mean(action)) if len(action) > 0 else 0.0
        action_scale = abs(mean_action)

        for i, name in enumerate(self.feature_names):
            if i < len(obs):
                importance = abs(obs[i]) * action_scale * (1.0 + abs(reward))
                self._history[name].append(float(importance))

    def get_importance(self, top_k: int | None = None) -> dict[str, float]:
        """获取特征重要性得分.

        Args:
            top_k: 只返回前 K 个特征.

        Returns:
            {feature_name: importance_score}.
        """
        # 所有已注册特征默认重要性 0.0 (尚未记录时也可查询/排序)
        scores = {name: 0.0 for name in self.feature_names}
        for name, vals in self._history.items():
            scores[name] = float(np.mean(vals)) if vals else 0.0

        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        if top_k:
            sorted_scores = sorted_scores[:top_k]
        return dict(sorted_scores)

    def get_top_features(self, k: int = 5) -> list[str]:
        """返回最重要 K 个特征名."""
        imp = self.get_importance()
        sorted_items = sorted(imp.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in sorted_items[:k]]

    def reset(self) -> None:
        self._history.clear()
        self._total_steps = 0


# ===================================================================
# 自适应特征选择
# ===================================================================
class AdaptiveFeatureSelector:
    """根据 IC 表现动态选择特征子集.

    维护特征池, 定期评估每个特征的 IC, 剔除低效特征.
    """

    def __init__(
        self,
        all_features: list[str],
        ic_threshold: float = 0.02,
        min_features: int = 3,
        eval_interval: int = 20,
    ):
        self.all_features = all_features
        self.ic_threshold = ic_threshold
        self.min_features = min(min_features, len(all_features))
        self.eval_interval = eval_interval
        self._active_features = list(all_features)
        self._ic_history: dict[str, list[float]] = defaultdict(list)
        self._step = 0

    @property
    def active_features(self) -> list[str]:
        return self._active_features

    def update(self, feature_values: dict[str, float], forward_return: float) -> dict[str, float]:
        """更新特征 IC 估计.

        Args:
            feature_values: {feature_name: value}.
            forward_return: 未来收益.

        Returns:
            {feature_name: ic_estimate}.
        """
        self._step += 1
        ic_estimates = {}

        for name, value in feature_values.items():
            if np.isfinite(value) and np.isfinite(forward_return):
                self._ic_history[name].append(value * forward_return)

        # 每 eval_interval 步重新评估特征
        if self._step % self.eval_interval == 0:
            self._evaluate()

        for name in self._active_features:
            hist = self._ic_history.get(name, [])
            if len(hist) >= 5:
                ic_estimates[name] = float(np.mean(hist[-self.eval_interval:]))
            else:
                ic_estimates[name] = 0.0

        return ic_estimates

    def _evaluate(self) -> None:
        """评估并更新活跃特征集."""
        scores = {}
        for name in self._active_features:
            hist = self._ic_history.get(name, [])
            if len(hist) >= self.eval_interval:
                recent_ic = float(np.mean(hist[-self.eval_interval:]))
                scores[name] = recent_ic

        if not scores:
            return

        # 保留 IC 大于阈值的特征
        kept = [name for name, ic in scores.items() if abs(ic) >= self.ic_threshold]
        if len(kept) < self.min_features:
            # 补充 IC 最高的
            sorted_items = sorted(scores.items(), key=lambda x: abs(x[1]), reverse=True)
            kept = [name for name, _ in sorted_items[:self.min_features]]

        self._active_features = kept

    def reset(self) -> None:
        self._active_features = list(self.all_features)
        self._ic_history.clear()
        self._step = 0


# ===================================================================
# 决策可追溯
# ===================================================================
class DecisionTrace:
    """记录每次决策的关键因素, 实现可追溯性.

    每条记录包含:
      - step: 步数
      - obs_summary: 观测关键维度
      - action: 动作
      - reward: 奖励
      - top_features: 最重要的 K 个特征
      - explanation: 自然语言解释 (结构化)
    """

    def __init__(self, feature_names: list[str], max_records: int = 1000):
        self.feature_names = feature_names
        self.max_records = max_records
        self._records: list[dict[str, Any]] = []
        self._step = 0

    def record(
        self,
        obs: np.ndarray,
        action: np.ndarray,
        reward: float,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """记录一步决策."""
        self._step += 1
        obs = np.asarray(obs, dtype=float).flatten()

        # 找出最重要的 3 个特征
        if len(self.feature_names) > 0:
            n = min(len(self.feature_names), len(obs))
            feature_contrib = [
                (self.feature_names[i], abs(obs[i]))
                for i in range(n)
            ]
            feature_contrib.sort(key=lambda x: x[1], reverse=True)
            top_features = feature_contrib[:3]
        else:
            top_features = []

        record = {
            "step": self._step,
            "obs_norm": float(np.linalg.norm(obs)),
            "obs_mean": float(np.mean(obs)),
            "action_mean": float(np.mean(action)),
            "action_std": float(np.std(action)),
            "reward": float(reward),
            "top_features": [{"name": n, "contribution": round(c, 4)} for n, c in top_features],
            "is_extreme": bool(abs(reward) > 3.0),
        }
        if extra:
            record.update(extra)

        self._records.append(record)
        if len(self._records) > self.max_records:
            self._records = self._records[-self.max_records:]

    def get_recent(self, n: int = 10) -> list[dict[str, Any]]:
        return self._records[-n:]

    def get_extreme_decisions(self, threshold: float = 3.0) -> list[dict[str, Any]]:
        """返回奖励极端 (|reward| > threshold) 的决策."""
        return [r for r in self._records if abs(r["reward"]) > threshold]

    def get_explanation(self, step: int | None = None) -> str:
        """生成决策可读解释.

        Args:
            step: 指定步数, 默认最近一步.

        Returns:
            str: 解释文本.
        """
        if not self._records:
            return "无决策记录"
        rec = self._records[-1] if step is None else self._records[step]
        lines = [
            f"第 {rec['step']} 步决策",
            f"奖励: {rec['reward']:.4f}",
            f"动作均值: {rec['action_mean']:.4f} (标准差: {rec['action_std']:.4f})",
            f"观测范数: {rec['obs_norm']:.2f}",
        ]
        if rec.get("top_features"):
            lines.append("关键特征:")
            for ft in rec["top_features"]:
                lines.append(f"  - {ft['name']}: 贡献 {ft['contribution']}")
        if rec.get("is_extreme"):
            lines.append("⚠ 极端决策")
        return "\n".join(lines)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "n_records": len(self._records),
                "records": self._records[-100:],
            }, f, ensure_ascii=False, indent=2)

    def reset(self) -> None:
        self._records.clear()
        self._step = 0


# ===================================================================
# 可解释性约束层 (集成到 FactorValueEnv)
# ===================================================================
def compute_explainability_penalty(
    weights: np.ndarray,
    feature_names: list[str],
    concentration_limit: float = 0.5,
) -> float:
    """计算可解释性惩罚: 权重集中度惩罚.

    鼓励分散化权重, 使决策更易解释.

    Args:
        weights: 因子权重.
        feature_names: 因子名.
        concentration_limit: 集中度上限.

    Returns:
        float: 惩罚值.
    """
    w = np.asarray(weights, dtype=float)
    if len(w) == 0:
        return 0.0
    # 最大权重占比惩罚
    max_w = float(w.max())
    if max_w > concentration_limit:
        return (max_w - concentration_limit) ** 2 * 0.5
    return 0.0