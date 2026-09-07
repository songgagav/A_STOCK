# -*- coding: utf-8 -*-
"""Almgren-Chriss 滑点与市场冲击模型.

将滑点分解为市场冲击（Market Impact）和执行风险（Execution Risk）两个独立分量,
替代原有固定万分之五滑点 + 万分之二冲击的静态费率.

参考: Almgren & Chriss (2001), "Optimal Execution of Portfolio Transactions"
      2025 RL-based execution optimization literature (slippage decomposition)

使用方式:
  from slippage_model import almglen_chriss_slippage

  # 在撮合前计算动态滑点率
  slippage_rate = almglen_chriss_slippage(
      order_size=10000,          # 订单金额 (元)
      avg_daily_volume=5e7,      # 日均成交额 (元)
      volatility=0.02,           # 20日年化波动率
  )
  # 替换原有固定 PAPER["slippage"] + PAPER["impact_cost"]
"""
from __future__ import annotations

import numpy as np


def almglen_chriss_slippage(
    order_size: float,
    avg_daily_volume: float,
    volatility: float,
    permanent_coef: float = 0.1,
    temporary_coef: float = 0.5,
    min_slippage: float = 0.0001,
    max_slippage: float = 0.02,
) -> float:
    """Almgren-Chriss 模型: 分解为永久冲击 + 临时冲击.

    Parameters
    ----------
    order_size : float
        订单金额 (元), 即 qty × price.
    avg_daily_volume : float
        日均成交额 (元), 建议用最近 20 日均值.
    volatility : float
        日频波动率 (小数, 如 0.02 = 2%), 建议用最近 20 日年化 / sqrt(252).
    permanent_coef : float
        永久冲击系数 (默认 0.1).
    temporary_coef : float
        临时冲击系数 (默认 0.5).
    min_slippage : float
        最小滑点率 (默认 万1, 防止极端小单计算为 0).
    max_slippage : float
        最大滑点率 (默认 2%, 防止极端大单/低流动性标的).

    Returns
    -------
    float
        单边滑点率 (小数), 买入时加到成交价, 卖出时从成交价扣除.
    """
    if avg_daily_volume <= 0 or order_size <= 0:
        return min_slippage

    participation = order_size / avg_daily_volume
    participation = np.clip(participation, 0.0, 1.0)  # 参与率不超过 100%

    # 永久冲击: 线性于参与率, 不随波动率变化
    permanent_impact = permanent_coef * participation

    # 临时冲击: 与参与率和波动率正相关
    temporary_impact = temporary_coef * participation * volatility

    total = permanent_impact + temporary_impact
    return float(np.clip(total, min_slippage, max_slippage))


def estimate_daily_volume(
    turnover_data: list[float] | None,
    fallback: float = 5e7,
) -> float:
    """从最近 N 日成交额序列估算日均成交额.

    Parameters
    ----------
    turnover_data : list[float] | None
        最近 N 日成交额序列 (元), 如 None 则回退 fallback.
    fallback : float
        无数据时的默认值 (默认 5000 万).

    Returns
    -------
    float
        日均成交额 (元), 至少 1 万.
    """
    if not turnover_data or len(turnover_data) < 1:
        return max(fallback, 1e4)
    arr = np.array(turnover_data, dtype=np.float64)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if len(arr) < 1:
        return max(fallback, 1e4)
    return float(max(arr.mean(), 1e4))


def estimate_volatility(
    close_prices: list[float] | None,
    fallback: float = 0.025,
) -> float:
    """从最近 N 日收盘价序列估算日频波动率.

    Parameters
    ----------
    close_prices : list[float] | None
        最近 N 日收盘价序列, 如 None 则回退 fallback.
    fallback : float
        无数据时的默认值 (默认 2.5%).

    Returns
    -------
    float
        日频波动率 (小数), 至少 0.001.
    """
    if not close_prices or len(close_prices) < 5:
        return fallback
    arr = np.array(close_prices, dtype=np.float64)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if len(arr) < 5:
        return fallback
    log_rets = np.diff(np.log(arr))
    log_rets = log_rets[np.isfinite(log_rets)]
    if len(log_rets) < 4:
        return fallback
    return float(max(np.std(log_rets, ddof=1), 0.001))


# ===================================================================
# 滑点双分量分解: 市场冲击 (Market Impact) vs 执行风险 (Execution Risk)
# ===================================================================
def market_impact_rate(
    order_size: float,
    avg_daily_volume: float,
    volatility: float,
    permanent_coef: float = 0.1,
    temporary_coef: float = 0.5,
    min_rate: float = 0.0001,
    max_rate: float = 0.02,
) -> float:
    """市场冲击分量 (Almgren-Chriss 永久+临时冲击).

    由订单参与率 (order_size / ADV) 和波动率决定, 是滑点中随订单规模
    变化的"可预测"部分. 返回值与 almglen_chriss_slippage 相同 (当执行
    时长为 0 时即等于总滑点).
    """
    if avg_daily_volume <= 0 or order_size <= 0:
        return min_rate
    participation = float(np.clip(order_size / avg_daily_volume, 0.0, 1.0))
    permanent_impact = permanent_coef * participation
    temporary_impact = temporary_coef * participation * volatility
    total = permanent_impact + temporary_impact
    return float(np.clip(total, min_rate, max_rate))


def execution_risk_rate(
    volatility: float,
    execution_horizon_days: float,
    urgency_kappa: float = 1.0,
    min_rate: float = 0.0,
    max_rate: float = 0.02,
) -> float:
    """执行风险分量 (滑点中随执行时长/波动率变化的"不确定性"部分).

    含义: 订单需要更长时间完成执行时, 承受价格逆向波动的风险更高,
    近似 σ × sqrt(horizon_days / 252). urgency_kappa 反映执行紧迫度
    (越大 = 越急于成交, 风险越高, 如被动跟随盘口的拆单可取 0.3~1.0).

    当执行时长为 0 (瞬时成交) 时该分量为 0, 完全回落到纯市场冲击.
    """
    if execution_horizon_days <= 0 or volatility <= 0:
        return 0.0
    horizon = float(np.clip(execution_horizon_days, 0.0, 252.0))
    risk = volatility * np.sqrt(horizon / 252.0) * urgency_kappa
    return float(np.clip(risk, min_rate, max_rate))


def decompose_slippage(
    order_size: float,
    avg_daily_volume: float,
    volatility: float,
    execution_horizon_days: float = 0.0,
    permanent_coef: float = 0.1,
    temporary_coef: float = 0.5,
    urgency_kappa: float = 1.0,
    min_slippage: float = 0.0001,
    max_slippage: float = 0.02,
) -> dict:
    """把单边滑点分解为两个独立分量.

    Parameters
    ----------
    order_size : float            订单金额 (元)
    avg_daily_volume : float      日均成交额 (元)
    volatility : float            日频波动率 (小数)
    execution_horizon_days : float 预计执行时长 (交易日, 0=瞬时成交)
    permanent_coef / temporary_coef : Almgren-Chriss 冲击系数
    urgency_kappa : float         执行紧迫度系数

    Returns
    -------
    dict: {
        "market_impact_rate": 市场冲击率 (小数),
        "execution_risk_rate": 执行风险率 (小数),
        "total_rate": 合计滑点率 (小数, 封顶 max_slippage),
        "market_impact_bps": 市场冲击 (基点),
        "execution_risk_bps": 执行风险 (基点),
        "total_bps": 合计 (基点),
    }
    """
    impact = market_impact_rate(
        order_size, avg_daily_volume, volatility,
        permanent_coef=permanent_coef, temporary_coef=temporary_coef,
        min_rate=min_slippage, max_rate=max_slippage,
    )
    risk = execution_risk_rate(
        volatility, execution_horizon_days, urgency_kappa=urgency_kappa,
        min_rate=0.0, max_rate=max_slippage,
    )
    total = float(np.clip(impact + risk, min_slippage, max_slippage))
    return {
        "market_impact_rate": round(impact, 8),
        "execution_risk_rate": round(risk, 8),
        "total_rate": round(total, 8),
        "market_impact_bps": round(impact * 1e4, 2),
        "execution_risk_bps": round(risk * 1e4, 2),
        "total_bps": round(total * 1e4, 2),
    }


def apply_slippage(
    ref_price: float,
    side: str,
    order_size: float,
    avg_daily_volume: float,
    volatility: float,
    execution_horizon_days: float = 0.0,
) -> tuple[float, dict]:
    """对参考价施加双分量滑点, 返回 (成交价, 分解明细).

    Parameters
    ----------
    ref_price : float         参考价 (现价/昨收)
    side : str                "buy" 或 "sell"
    """
    dec = decompose_slippage(
        order_size, avg_daily_volume, volatility, execution_horizon_days)
    direction = 1.0 if str(side).lower() == "buy" else -1.0
    exec_price = ref_price * (1.0 + direction * dec["total_rate"])
    return float(exec_price), dec