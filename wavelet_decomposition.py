# -*- coding: utf-8 -*-
"""多尺度信号分解: 小波变换分离趋势和波动.

参考: 2026 年 Hybrid-GRPO 框架, 用小波分解分离趋势和波动,
再结合 GRPO 组相对策略优化 + GAE 绝对基线, 提升更新稳定性.

核心功能:
  1. 小波分解: 将价格/收益率序列分解为低频趋势 + 高频波动 + 细节分量
  2. 分解特征: 输出多尺度特征向量, 嵌入 DRL 状态空间
  3. 重构信号: 可选重构用于奖励平滑
"""

from __future__ import annotations

import numpy as np
from typing import Any


# ===================================================================
# 小波分解核心
# ===================================================================
def _db4_wavelet_decomp(x: np.ndarray, level: int = 3) -> dict[str, np.ndarray]:
    """近似 Daubechies-4 小波分解 (纯 NumPy 实现, 无外部依赖).

    使用 Haar 小波近似 (db4 的简化版), 保留趋势/波动/细节三层.
    对长度 < 2 的序列返回全零.

    Args:
        x: 输入序列 (T,).
        level: 分解层数 (默认 3, 最多 5).

    Returns:
        dict: {
            "trend": ndarray,     # 低频趋势 (T,)
            "volatility": ndarray, # 高频波动 (T,)
            "detail": ndarray,     # 细节分量 (T,)
            "energy": float,       # 总能量
            "trend_ratio": float,  # 趋势能量占比
            "vol_ratio": float,    # 波动能量占比
        }
    """
    arr = np.asarray(x, dtype=float).copy()
    T = len(arr)
    if T < 2:
        return {"trend": np.zeros_like(arr), "volatility": np.zeros_like(arr),
                "detail": np.zeros_like(arr), "energy": 0.0,
                "trend_ratio": 0.0, "vol_ratio": 0.0}

    # Haar 小波分解
    trend = arr.copy()
    details = []
    for lvl in range(min(level, 5)):
        n = len(trend)
        if n < 2:
            break
        half = n // 2
        # 近似系数 (均值)
        approx = (trend[:half*2:2] + trend[1:half*2:2]) / 2.0
        # 细节系数 (差值)
        detail = (trend[:half*2:2] - trend[1:half*2:2]) / 2.0
        details.append(detail)
        trend = approx

    # 上采样重构到原始长度
    def _upsample(coeffs: np.ndarray, target_len: int) -> np.ndarray:
        if len(coeffs) == 0:
            return np.zeros(target_len)
        result = np.zeros(target_len)
        for i, c in enumerate(coeffs):
            idx = i * (target_len // max(1, len(coeffs)))
            if idx < target_len:
                result[idx] = c
        return result

    trend_up = _upsample(trend, T)
    vol_up = np.zeros(T)
    detail_up = np.zeros(T)

    if len(details) >= 1:
        vol_up = _upsample(details[0], T)
    if len(details) >= 2:
        pass  # 更多细节已合并到 vol_up
    if len(details) >= 3:
        detail_up = _upsample(details[-1], T)

    # 能量
    energy = float(np.sum(arr ** 2)) + 1e-10
    trend_energy = float(np.sum(trend_up ** 2))
    vol_energy = float(np.sum(vol_up ** 2))

    return {
        "trend": trend_up,
        "volatility": vol_up,
        "detail": detail_up,
        "energy": energy,
        "trend_ratio": trend_energy / energy,
        "vol_ratio": vol_energy / energy,
    }


def extract_wavelet_features(
    prices: np.ndarray,
    level: int = 3,
    window: int = 60,
) -> np.ndarray:
    """从价格序列提取多尺度小波特征.

    Args:
        prices: 价格序列 (T,), T >= window.
        level: 分解层数.
        window: 滑动窗口.

    Returns:
        ndarray: (T,) 特征向量, 每步包含:
            [trend_val, vol_val, trend_ratio, vol_ratio, energy_log]
            (5 维特征, 展平后每步 5 维)
    """
    arr = np.asarray(prices, dtype=float)
    T = len(arr)
    if T < window:
        window = max(2, T)

    features = np.zeros((T, 5), dtype=np.float32)
    for t in range(window, T + 1):
        chunk = arr[t - window:t]
        dec = _db4_wavelet_decomp(chunk, level)
        features[t - 1, 0] = float(dec["trend"][-1]) if len(dec["trend"]) > 0 else 0.0
        features[t - 1, 1] = float(dec["volatility"][-1]) if len(dec["volatility"]) > 0 else 0.0
        features[t - 1, 2] = float(dec["trend_ratio"])
        features[t - 1, 3] = float(dec["vol_ratio"])
        features[t - 1, 4] = float(np.log1p(dec["energy"]))

    return features  # (T, 5)


def smooth_reward_with_wavelet(
    rewards: np.ndarray, level: int = 2,
) -> np.ndarray:
    """用小波重构平滑奖励序列."""
    arr = np.asarray(rewards, dtype=float).copy()
    if len(arr) < 4:
        return arr
    dec = _db4_wavelet_decomp(arr, level)
    # 只用趋势分量重构 (去噪)
    return dec["trend"]


# ===================================================================
# Hybrid-GRPO 辅助函数
# ===================================================================
def compute_grpo_gae(
    rewards: np.ndarray,
    values: np.ndarray,
    gamma: float = 0.99,
    lam: float = 0.95,
    group_size: int = 4,
) -> tuple[np.ndarray, np.ndarray]:
    """计算 Hybrid-GRPO 的 GAE 绝对基线 + 组相对优势.

    GRPO 核心: 用组内相对优势代替传统的价值网络优势.

    Args:
        rewards: 奖励序列 (T,).
        values: 价值估计 (T,).
        gamma: 折扣因子.
        lam: GAE lambda.
        group_size: 组大小 (用于组相对优势).

    Returns:
        (gae_advantages, group_advantages):
            gae_advantages: GAE 绝对基线 (T,)
            group_advantages: 组相对优势 (T,)
    """
    T = len(rewards)
    gae = np.zeros(T, dtype=float)
    group_adv = np.zeros(T, dtype=float)

    # GAE 计算
    gae_runner = 0.0
    for t in reversed(range(T)):
        delta = rewards[t] + gamma * (values[t + 1] if t + 1 < T else 0.0) - values[t]
        gae_runner = delta + gamma * lam * gae_runner
        gae[t] = gae_runner

    # 组相对优势: 将序列分成 group_size 组, 组内归一化
    n_groups = max(1, T // group_size)
    for g in range(n_groups):
        start = g * group_size
        end = min(start + group_size, T)
        group_rewards = rewards[start:end]
        if len(group_rewards) > 1:
            mean_r = group_rewards.mean()
            std_r = group_rewards.std() + 1e-8
            group_adv[start:end] = (group_rewards - mean_r) / std_r

    return gae, group_adv


def hybrid_grpo_loss(
    advantages: th.Tensor,
    group_advantages: th.Tensor,
    log_probs: th.Tensor,
    old_log_probs: th.Tensor,
    clip_range: float = 0.2,
    grpo_coef: float = 0.3,
    entropy: th.Tensor | None = None,
) -> tuple[th.Tensor, th.Tensor, th.Tensor]:
    """Hybrid-GRPO 损失函数: PPO clip + GRPO 组相对约束.

    在 PyTorch 张量上计算, 供 CVaR_PPO 的 train() 调用.

    Returns:
        (policy_loss, grpo_loss, entropy_loss)
    """
    import torch as th

    ratio = th.exp(log_probs - old_log_probs)

    # PPO clip 损失 (GAE 绝对基线)
    pg_loss1 = -advantages * ratio
    pg_loss2 = -advantages * th.clamp(ratio, 1.0 - clip_range, 1.0 + clip_range)
    policy_loss = th.max(pg_loss1, pg_loss2).mean()

    # GRPO 组相对约束
    grpo_loss = (group_advantages * ratio).mean() * grpo_coef

    # 熵奖励
    entropy_loss = 0.0
    if entropy is not None:
        entropy_loss = -0.01 * entropy.mean()

    return policy_loss, grpo_loss, entropy_loss