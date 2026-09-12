# -*- coding: utf-8 -*-
"""CSCV-PBO: 用组合对称交叉验证估计"回测过拟合概率" (2026-09-13).

参考: Bailey, Borwein, López de Prado & Zhu (2014),
     "The Probability of Backtest Overfitting", Journal of Computational Finance.

为何替换原实现
--------------
原 overfitting_test.test_pbo() 的做法是:
    shuffled = choice(sharpes, n, replace=False) + N(0, 0.5*std(sharpes))
    rank = P(max(shuffled) < max(sharpes));  pbo = 1 - rank
而 `choice(..., replace=False)` 只是置换, `max` 恒等于 `max(sharpes)`, 于是判据
退化为"噪声是否把其他配置抬过最大值", 数学上 rank <= 1/2 -> **pbo >= 0.5 恒成立**,
`pbo < 50%` 这项检查因此永不可能通过; 且噪声尺度 0.5*std 是写死的常数, 与策略
优劣无关。实测 12 窗口给出 0.7308(7 窗口时 0.5366), 两值均在 0.5 以上, 可佐证。

本模块实现标准 CSCV
-------------------
输入: 时间被切成 S 个不相交的等长块(S 为偶数), 每块上 N 个候选配置的日收益。
对每一个"取 S/2 块作 IS、其余 S/2 块作 OOS"的组合 c (共 C(S, S/2) 种):
    n*  = IS 上表现最好的配置
    w_c = n* 在 OOS 上按表现升序的归一化排名 = rank / (N + 1) ∈ (0, 1)
    λ_c = logit(w_c) = ln(w_c / (1 - w_c))
PBO = P(λ_c < 0), 即"IS 最优的配置在 OOS 落到后一半"的频率。PBO 越低越好,
> 0.5 表示该搜索过程大概率只是在拟合噪声。

同时输出论文中的辅助量:
    - prob_oos_loss: IS 最优配置在 OOS 上亏损的频率
    - is_oos_slope / is_oos_intercept: OOS 表现对 IS 表现的回归(过拟合时趋平)
    - λ 的经验分布分位数

本模块为纯计算(不依赖网络/数据库), 便于单元测试。
"""
from __future__ import annotations

import itertools
import math
from typing import Sequence

import numpy as np

TRADING_DAYS = 252


def sharpe(x: np.ndarray) -> float:
    """年化 Sharpe(无风险利率取 0). 样本不足或零波动时返回 NaN."""
    a = np.asarray(x, dtype=np.float64).ravel()
    a = a[np.isfinite(a)]
    if a.size < 3:
        return float("nan")
    sd = float(np.std(a, ddof=1))
    if not np.isfinite(sd) or sd <= 1e-12:
        return float("nan")
    return float(np.mean(a) / sd * math.sqrt(TRADING_DAYS))


def _avg_rank(values: np.ndarray) -> np.ndarray:
    """升序平均排名(1..N, 并列取平均), 与 scipy.stats.rankdata 的 'average' 等价.

    不引入 scipy 依赖; NaN 排在最后并共享末位平均名次。
    """
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(n, dtype=np.float64)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def cscv_pbo(blocks: Sequence[np.ndarray],
             metric=sharpe) -> dict:
    """组合对称交叉验证估计 PBO.

    参数
    ----
    blocks : 长度 S(偶数, S>=4) 的序列; blocks[i] 是第 i 个时间块上 N 个配置的
             日收益矩阵, shape = (该块的天数, N)。各块的 N 必须一致, 行数可不同。
    metric : 表现度量, 默认年化 Sharpe。

    返回 dict: pbo / n_blocks / n_configs / n_combos / lambda 统计 / 辅助量。
    """
    if not blocks:
        raise ValueError("blocks 不能为空")
    arrs = [np.asarray(b, dtype=np.float64) for b in blocks]
    if any(a.ndim != 2 for a in arrs):
        raise ValueError("每个 block 必须是二维 (天数, 配置数)")
    S = len(arrs)
    N = arrs[0].shape[1]
    if N < 2:
        raise ValueError(f"至少需要 2 个配置, 实际 {N}")
    if S < 4 or S % 2 != 0:
        raise ValueError(f"块数 S 必须为 >=4 的偶数, 实际 {S}")
    if any(a.shape[1] != N for a in arrs):
        raise ValueError("各 block 的配置数 N 不一致")

    half = S // 2
    combos = list(itertools.combinations(range(S), half))
    lambdas, omegas, is_best_oos, is_best_is = [], [], [], []
    oos_loss = 0
    is_all, oos_all = [], []

    for is_idx in combos:
        is_set = set(is_idx)
        oos_idx = [i for i in range(S) if i not in is_set]
        m_is = np.concatenate([arrs[i] for i in is_idx], axis=0)
        m_oos = np.concatenate([arrs[i] for i in oos_idx], axis=0)

        p_is = np.array([metric(m_is[:, n]) for n in range(N)], dtype=np.float64)
        p_oos = np.array([metric(m_oos[:, n]) for n in range(N)], dtype=np.float64)
        if not np.isfinite(p_is).any():
            continue
        n_star = int(np.nanargmax(p_is))

        ranks = _avg_rank(p_oos)
        w = float(ranks[n_star]) / (N + 1)          # 归一化到 (0,1)
        lam = math.log(w / (1.0 - w))               # logit
        lambdas.append(lam)
        omegas.append(w)
        is_best_is.append(float(p_is[n_star]))
        is_best_oos.append(float(p_oos[n_star]))
        if np.isfinite(p_oos[n_star]) and p_oos[n_star] < 0:
            oos_loss += 1
        is_all.append(p_is)
        oos_all.append(p_oos)

    if not lambdas:
        raise ValueError("所有组合的 IS 表现均不可计算(检查收益矩阵是否全为 NaN)")

    lam = np.array(lambdas, dtype=np.float64)
    pbo = float(np.mean(lam < 0.0))

    # IS -> OOS 回归(过拟合时斜率趋 0 或为负)
    slope = intercept = float("nan")
    X = np.concatenate(is_all) if is_all else np.array([])
    Y = np.concatenate(oos_all) if oos_all else np.array([])
    m = np.isfinite(X) & np.isfinite(Y)
    if m.sum() >= 3 and np.std(X[m]) > 1e-12:
        slope, intercept = np.polyfit(X[m], Y[m], 1)

    return {
        "pbo": pbo,
        "n_blocks": S,
        "n_configs": N,
        "n_combos": len(lambdas),
        "lambda_mean": float(np.mean(lam)),
        "lambda_std": float(np.std(lam, ddof=1)) if lam.size > 1 else 0.0,
        "lambda_p05": float(np.percentile(lam, 5)),
        "lambda_p50": float(np.percentile(lam, 50)),
        "lambda_p95": float(np.percentile(lam, 95)),
        "omega_mean": float(np.mean(omegas)),
        "prob_oos_loss": float(oos_loss / len(lambdas)),
        "is_best_mean_is": float(np.mean(is_best_is)),
        "is_best_mean_oos": float(np.mean(is_best_oos)),
        "is_oos_slope": float(slope),
        "is_oos_intercept": float(intercept),
    }
