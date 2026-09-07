# -*- coding: utf-8 -*-
"""Logic-Q 神经符号化趋势分析框架.

参考: 2026 年提出的 Logic-Q 框架, 通过神经符号化趋势分析 (NeSy-TA),
动态判断市场趋势并调整模型参数.

核心思想:
  1. 符号化趋势规则 (MA 交叉, 支撑阻力, 量价关系) 编码为可调参数
  2. 规则参数根据历史数据自适应校准
  3. 动态调整策略网络输出分布 (temperature / 动作偏置)

与 neural_ta.py 互补:
  - neural_ta.py: 用 MLP 学习特征到调优参数的映射 (神经层)
  - logic_q.py:  用符号化规则 + 参数校准 (符号层)
  - 两者可独立输出, 也可融合输出 (Logic-Q 论文的核心贡献)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import numpy as np

_LOG = logging.getLogger("logic_q")

# ===================================================================
# 符号化趋势规则定义
# ===================================================================
MA_CROSS_LOOKBACKS = [5, 10, 20, 60]  # 均线周期对
MA_CROSS_PAIRS = [(5, 10), (5, 20), (10, 20), (20, 60)]


def _calc_ma(arr: np.ndarray, period: int) -> float:
    if len(arr) < period or period <= 0:
        return 0.0
    return float(arr[-period:].mean())


def _calc_support_resistance(
    highs: np.ndarray, lows: np.ndarray, lookback: int = 60
) -> tuple[float, float]:
    """计算支撑位和阻力位 (最近 lookback 日的极值)."""
    if len(highs) < lookback or len(lows) < lookback:
        return float(highs[-1]) if len(highs) else 0.0, float(lows[-1]) if len(lows) else 0.0
    resistance = float(highs[-lookback:].max())
    support = float(lows[-lookback:].min())
    return support, resistance


# ===================================================================
# 趋势状态枚举
# ===================================================================
class TrendState:
    """A 股市场趋势状态.

    适用于短周期 (日频), 编码为 4 类:
      0 = 震荡 (无明确方向)
      1 = 上升趋势 (短均线 > 长均线, 且价在支撑位上方)
      2 = 下降趋势 (短均线 < 长均线, 且价在阻力位下方)
      3 = 反转预警 (趋势即将改变, 如均线粘合或量价背离)
    """
    OSCILLATE = 0
    UPTREND = 1
    DOWNTREND = 2
    REVERSION_WARNING = 3


# ===================================================================
# 符号化趋势规则引擎
# ===================================================================
class SymbolicTrendEngine:
    """符号化趋势规则引擎.

    输入: 价格序列 (close, high, low)
    输出: 趋势状态 + 规则参数 + 置信度

    可调参数:
      - ma_cross_threshold: 均线交叉阈值 (短-长均线差 / 长均线, 小数)
      - sr_breakout_threshold: 支撑阻力突破阈值 (小数)
      - volume_confirmation: 成交量确认信号权重 [0, 1]
    """

    def __init__(
        self,
        ma_cross_threshold: float = 0.02,
        sr_breakout_threshold: float = 0.015,
        volume_confirmation: float = 0.5,
    ):
        self.ma_cross_threshold = ma_cross_threshold
        self.sr_breakout_threshold = sr_breakout_threshold
        self.volume_confirmation = volume_confirmation

    def get_params(self) -> dict[str, float]:
        return {
            "ma_cross_threshold": self.ma_cross_threshold,
            "sr_breakout_threshold": self.sr_breakout_threshold,
            "volume_confirmation": self.volume_confirmation,
        }

    def set_params(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, float(v))

    def analyze(
        self,
        closes: np.ndarray,
        highs: np.ndarray | None = None,
        lows: np.ndarray | None = None,
        volumes: np.ndarray | None = None,
    ) -> dict[str, Any]:
        """对价格序列执行符号化趋势分析.

        Args:
            closes: 收盘价序列 (至少 60 个数据点).
            highs: 最高价序列 (可选).
            lows: 最低价序列 (可选).
            volumes: 成交量序列 (可选).

        Returns:
            dict: {
                "trend_state": int (0-3),
                "trend_state_name": str,
                "ma_cross_score": float,     # [-1, 1], 正=多头排列
                "ma_bull_ratio": float,       # [0, 1], 多头排列占比
                "ma_bear_ratio": float,       # [0, 1], 空头排列占比
                "sr_position": float,         # [0, 1], 价格在支撑/阻力间的相对位置
                "volume_ratio": float,        # 量比 (当前/均值)
                "confidence": float,          # 趋势判断置信度 [0, 1]
                "params_used": dict,          # 当前使用的参数
            }
        """
        if len(closes) < 60:
            return {"trend_state": TrendState.OSCILLATE,
                    "trend_state_name": "OSCILLATE",
                    "ma_cross_score": 0.0, "ma_bull_ratio": 0.0,
                    "ma_bear_ratio": 0.0, "sr_position": 0.5,
                    "volume_ratio": 1.0, "confidence": 0.0,
                    "params_used": self.get_params()}

        closes = np.asarray(closes, dtype=float)
        ma_cache = {p: _calc_ma(closes, p) for p in MA_CROSS_LOOKBACKS}

        # ---- 均线交叉得分 ----
        cross_scores = []
        bull_count = 0
        bear_count = 0
        for short_p, long_p in MA_CROSS_PAIRS:
            if short_p not in ma_cache or long_p not in ma_cache:
                continue
            ma_s = ma_cache[short_p]
            ma_l = ma_cache[long_p]
            ref = ma_l if abs(ma_l) > 1e-8 else 1.0
            diff = (ma_s - ma_l) / ref
            cross_scores.append(diff)
            if diff > self.ma_cross_threshold:
                bull_count += 1
            elif diff < -self.ma_cross_threshold:
                bear_count += 1

        ma_cross_score = float(np.mean(cross_scores)) if cross_scores else 0.0
        n_pairs = len(cross_scores) if cross_scores else 1
        ma_bull_ratio = bull_count / n_pairs
        ma_bear_ratio = bear_count / n_pairs

        # ---- 支撑阻力位置 ----
        cur_price = closes[-1]
        h = highs if highs is not None else closes
        lo = lows if lows is not None else closes
        support, resistance = _calc_support_resistance(h, lo)
        sr_range = resistance - support if resistance > support else 1.0
        sr_position = float(np.clip(
            (cur_price - support) / sr_range, 0.0, 1.0))

        # ---- 成交量确认 ----
        volume_ratio = 1.0
        if volumes is not None and len(volumes) >= 20:
            v_arr = np.asarray(volumes, dtype=float)
            cur_vol = float(v_arr[-1])
            avg_vol = float(v_arr[-20:].mean())
            if avg_vol > 1e-8:
                volume_ratio = cur_vol / avg_vol

        # ---- 趋势状态判定 ----
        trend_state = TrendState.OSCILLATE
        signals = []

        if ma_cross_score > self.ma_cross_threshold and sr_position > 0.3:
            trend_state = TrendState.UPTREND
            signals.append("uptrend")
        elif ma_cross_score < -self.ma_cross_threshold and sr_position < 0.7:
            trend_state = TrendState.DOWNTREND
            signals.append("downtrend")
        else:
            signals.append("oscillate")

        # 反转预警: 均线粘合 (差绝对值小) + 量价背离
        ma5_20 = abs(ma_cache.get(5, 0) - ma_cache.get(20, 0))
        ma20_60 = abs(ma_cache.get(20, 0) - ma_cache.get(60, 0))
        if ma5_20 < self.ma_cross_threshold * 0.5 and ma20_60 < self.ma_cross_threshold * 0.5:
            trend_state = TrendState.REVERSION_WARNING
            signals.append("reversion_warning")

        # 置信度: 基于信号一致性
        n_signal = len(signals)
        confidence = 0.5 + abs(ma_cross_score) * 2.0
        if n_signal >= 2:
            confidence *= 0.8
        confidence = float(np.clip(confidence, 0.0, 1.0))

        return {
            "trend_state": int(trend_state),
            "trend_state_name": ["OSCILLATE", "UPTREND", "DOWNTREND",
                                 "REVERSION_WARNING"][trend_state],
            "ma_cross_score": round(float(ma_cross_score), 4),
            "ma_bull_ratio": round(float(ma_bull_ratio), 4),
            "ma_bear_ratio": round(float(ma_bear_ratio), 4),
            "sr_position": round(float(sr_position), 4),
            "volume_ratio": round(float(volume_ratio), 4),
            "confidence": round(float(confidence), 4),
            "signals": signals,
            "params_used": self.get_params(),
        }


# ===================================================================
# 可调参数校准器
# ===================================================================
class ParameterCalibrator:
    """根据历史 IC 表现校准符号化规则参数.

    校准目标: 让规则参数在历史 IC 数据上达到最优表现.
    使用网格搜索 + 简单 IC 评估.

    可调参数空间:
      ma_cross_threshold: [0.005, 0.05], 步长 0.005
      sr_breakout_threshold: [0.005, 0.04], 步长 0.005
    """

    def __init__(self, engine: SymbolicTrendEngine):
        self.engine = engine

    def calibrate(
        self,
        price_series: dict[str, np.ndarray],
        forward_returns: np.ndarray,
    ) -> dict[str, float]:
        """网格搜索校准参数.

        Args:
            price_series: {"close": ndarray, "high": ndarray, "low": ndarray}.
            forward_returns: 未来收益序列 (与 price_series 长度对齐).

        Returns:
            dict: 最优参数 {参数名: 值}.
        """
        best_ic = -1.0
        best_params = self.engine.get_params()

        for ma_cross in np.arange(0.005, 0.055, 0.005):
            for sr_break in np.arange(0.005, 0.045, 0.005):
                self.engine.set_params(
                    ma_cross_threshold=ma_cross,
                    sr_breakout_threshold=sr_break,
                )
                result = self.engine.analyze(
                    price_series.get("close", np.array([])),
                    price_series.get("high"),
                    price_series.get("low"),
                )
                # 趋势信号 -> 方向预测 (1=多头, -1=空头, 0=震荡)
                ts = result["trend_state"]
                signal = 1.0 if ts == TrendState.UPTREND else (
                    -1.0 if ts == TrendState.DOWNTREND else 0.0
                )
                if len(forward_returns) > 0:
                    ic = float(np.corrcoef(
                        [signal] * len(forward_returns), forward_returns)[0, 1])
                    ic = abs(ic) if np.isfinite(ic) else -1.0
                    if ic > best_ic:
                        best_ic = ic
                        best_params = {
                            "ma_cross_threshold": round(ma_cross, 3),
                            "sr_breakout_threshold": round(sr_break, 3),
                        }

        self.engine.set_params(**best_params)
        return best_params


# ===================================================================
# Logic-Q 综合接口
# ===================================================================
class LogicQ:
    """Logic-Q 神经符号化趋势分析主类.

    集成符号化规则引擎 + 可调参数校准 + 策略输出调整.

    用法:
        lq = LogicQ()
        result = lq.analyze(closes, highs, lows, volumes)
        tuning = lq.get_policy_tuning(result)  # 给 PPO 策略网络的调优参数
    """

    def __init__(
        self,
        engine: SymbolicTrendEngine | None = None,
        calibrator: ParameterCalibrator | None = None,
    ):
        self.engine = engine or SymbolicTrendEngine()
        self.calibrator = calibrator or ParameterCalibrator(self.engine)
        self._calibrated = False

    def calibrate(
        self,
        closes: np.ndarray,
        forward_returns: np.ndarray,
        highs: np.ndarray | None = None,
        lows: np.ndarray | None = None,
    ) -> dict[str, float]:
        """校准规则参数."""
        params = self.calibrator.calibrate(
            {"close": closes, "high": highs, "low": lows},
            forward_returns,
        )
        self._calibrated = True
        return params

    def analyze(
        self,
        closes: np.ndarray,
        highs: np.ndarray | None = None,
        lows: np.ndarray | None = None,
        volumes: np.ndarray | None = None,
    ) -> dict[str, Any]:
        """执行符号化趋势分析."""
        return self.engine.analyze(closes, highs, lows, volumes)

    def get_policy_tuning(
        self, analysis_result: dict[str, Any] | None = None,
        closes: np.ndarray | None = None,
        highs: np.ndarray | None = None,
        lows: np.ndarray | None = None,
        volumes: np.ndarray | None = None,
    ) -> dict[str, float]:
        """根据趋势分析结果, 输出 PPO 策略网络的调优参数.

        与 neural_ta.py 的 compute_tuning 输出格式兼容:
            delta_scale: 动作幅度缩放 [0.2, 1.5]
            temperature: 动作噪声温度 [0.5, 2.5]
            weight_clip: 权重裁剪上限 [0.3, 0.7]

        Logic-Q 特有:
            trend_bias: 趋势偏置 (-1~1, 负=偏空, 正=偏多)
            action_bias: 动作偏置向量 (shape 与 PPO 动作空间匹配)
        """
        if analysis_result is None:
            if closes is not None:
                analysis_result = self.analyze(closes, highs, lows, volumes)
            else:
                return {"delta_scale": 0.5, "temperature": 1.0,
                        "weight_clip": 0.5, "trend_bias": 0.0,
                        "mode": "fallback"}

        ts = analysis_result.get("trend_state", TrendState.OSCILLATE)
        conf = analysis_result.get("confidence", 0.5)
        ma_score = analysis_result.get("ma_cross_score", 0.0)
        sr_pos = analysis_result.get("sr_position", 0.5)
        vol_ratio = analysis_result.get("volume_ratio", 1.0)

        # ---- delta_scale: 趋势越强, scale 越大 ----
        if ts == TrendState.UPTREND:
            delta = 0.8 + abs(ma_score) * 0.7
        elif ts == TrendState.DOWNTREND:
            delta = 0.2 + abs(ma_score) * 0.3
        elif ts == TrendState.REVERSION_WARNING:
            delta = 0.3 + (1.0 - conf) * 0.5
        else:
            delta = 0.35 + abs(ma_score) * 0.5
        delta = max(0.2, min(1.5, delta))

        # ---- temperature: 反转预警时高温度 (多探索) ----
        if ts == TrendState.REVERSION_WARNING:
            temp = 1.8 + (1.0 - conf) * 0.7
        elif ts == TrendState.OSCILLATE:
            temp = 1.2 + (1.0 - conf) * 0.8
        else:
            temp = 0.8 + (1.0 - conf) * 0.5
        temp = max(0.5, min(2.5, temp))

        # ---- weight_clip: 趋势强->集中, 震荡->分散 ----
        if ts == TrendState.UPTREND:
            wc = 0.35 - abs(ma_score) * 0.1
        elif ts == TrendState.DOWNTREND:
            wc = 0.45 - abs(ma_score) * 0.15
        else:
            wc = 0.55 + (1.0 - conf) * 0.15
        wc = max(0.3, min(0.7, wc))

        # ---- trend_bias: 趋势偏置 ----
        if ts == TrendState.UPTREND:
            trend_bias = 0.3 * conf
        elif ts == TrendState.DOWNTREND:
            trend_bias = -0.3 * conf
        elif ts == TrendState.REVERSION_WARNING:
            trend_bias = -0.1 * conf
        else:
            trend_bias = 0.0

        # ---- 量价确认 ----
        if vol_ratio > 1.5 and ts == TrendState.UPTREND:
            delta *= 1.2
            delta = min(1.5, delta)
        elif vol_ratio < 0.5 and ts == TrendState.UPTREND:
            delta *= 0.8
            delta = max(0.2, delta)

        return {
            "delta_scale": round(delta, 4),
            "temperature": round(temp, 4),
            "weight_clip": round(wc, 4),
            "trend_bias": round(trend_bias, 4),
            "trend_state": ts,
            "trend_state_name": analysis_result.get("trend_state_name", "OSCILLATE"),
            "confidence": round(conf, 4),
            "mode": "logic_q",
            "params_used": analysis_result.get("params_used", {}),
        }


# ===================================================================
# 快捷接口: 从 realtime_engine 获取价格序列, 输出调优参数
# ===================================================================
def compute_logic_q_tuning(
    day_dir: str,
    calibrate: bool = False,
) -> dict[str, Any]:
    """从 h5i-db 读取 CSI300 价格序列, 执行 Logic-Q 分析.

    Args:
        day_dir: 交易日 YYYYMMDD.
        calibrate: 是否执行参数校准 (计算量大, 默认关闭).

    Returns:
        dict: Logic-Q 分析结果 + 调优参数.
    """
    try:
        from h5i_bar_store import H5iBarStore
        store = H5iBarStore()
        bars = store.bars("000300", end=f"{day_dir[:4]}-{day_dir[4:6]}-{day_dir[6:8]}")
        store.close()
    except Exception:
        return {"ok": False, "error": "h5i-db 不可用",
                "mode": "fallback"}

    if bars is None or bars.empty or len(bars) < 60:
        return {"ok": False, "error": f"数据不足 (need >= 60, got {len(bars) if bars is not None else 0})",
                "mode": "fallback"}

    closes = bars["close"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float) if "high" in bars.columns else closes
    lows = bars["low"].to_numpy(dtype=float) if "low" in bars.columns else closes
    volumes = (bars["volume"].to_numpy(dtype=float)
               if "volume" in bars.columns and bars["volume"].notna().any()
               else None)

    lq = LogicQ()
    analysis = lq.analyze(closes, highs, lows, volumes)
    tuning = lq.get_policy_tuning(analysis)

    result = {
        "ok": True,
        "day": day_dir,
        "analysis": analysis,
        "tuning": tuning,
        "mode": "logic_q",
    }

    if calibrate:
        # 用未来 5 日收益做校准 (仅用于评估, 不参与训练)
        if len(closes) > 65:
            fwd_ret = (closes[5:] - closes[:-5]) / closes[:-5]
            params = lq.calibrate(closes, fwd_ret, highs, lows)
            result["calibrated_params"] = params

    return result