# ============================================================
# p08_signal.py -- P08 治理信号 纯pandas重算
# 与回测/模拟盘版 _calc_signal 逻辑一致 (RSI/MACD/SMA/ATR/breakout)
# 用于: 对任意 A 股个股的历史日线计算 P08 治理信号值(全A轮动打分输入)
# ============================================================

import numpy as np
import pandas as pd
from config import SIGNAL_PARAMS
from factor_library import build_adj_close   # P1: 复权缺口修复


# P1: 复权缺口下, 原始 high/low 与 close 同属不复权尺度. 用复发 close 重建比例
# 相同的 high/low (每日 ratio = adj_close / raw_close 缩放), 保证突破/ATR不受
# 除权日价格骤变影响. 无 change_pct 时 build_adj_close 返回原始 close, 缩放比为1.
def _adj_hl(df: pd.DataFrame):
    adj_close = build_adj_close(df)
    raw_close = pd.to_numeric(df["close"], errors="coerce").astype(float)
    ratio = adj_close / raw_close.replace(0, np.nan)
    ratio = ratio.fillna(1.0).replace([np.inf, -np.inf], 1.0)
    high = pd.to_numeric(df.get("high"), errors="coerce").astype(float) * ratio
    low = pd.to_numeric(df.get("low"), errors="coerce").astype(float) * ratio
    return adj_close, high, low


# ---- 指标实现 (与 vnpy ArrayManager 口径对齐) ----
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # vnpy 用 Wilder 平滑 (SMA of gains), 这里用 EWM alpha=1/period 近似
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(50.0)


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / period, adjust=False).mean()


def ma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


# ---- P08 原始信号 (对应 _calc_signal) ----
def calc_raw_signal(df: pd.DataFrame, params: dict = None) -> pd.Series:
    """对单只股票日线 DataFrame(含 date/open/high/low/close/volume)
    返回逐日均值化后的 raw_signal (未截断/未平滑, 供打分)
    """
    p = {**SIGNAL_PARAMS, **(params or {})}
    close, high, low = _adj_hl(df)   # P1: 复权close/high/low, 消除除权缺口

    rsi_v = rsi(close, p["rsi_period"])
    dif, dea, hist = macd(close)
    atr_v = atr(df, p["atr_period"])
    sma20 = ma(close, p["sma_short"])
    sma60 = ma(close, p["sma_mid"])
    sma120 = ma(close, p["sma_long"])

    # RSI 分量
    rsi_sig = np.where(rsi_v > 70, -0.5,
               np.where(rsi_v < 30, 0.5,
               (rsi_v - 50) / 20.0))

    # MACD 分量
    macd_sig = np.where(
        (hist > 0) & (dif > dea), 0.3,
        np.where((hist < 0) & (dif < dea), -0.3, 0.0))

    # 趋势分量
    trend_sig = np.where(
        (close > sma20) & (sma20 > sma60), 0.3,
        np.where((close < sma20) & (sma20 < sma60), -0.3, 0.0))

    # 突破分量
    high20 = high.rolling(20).max()
    low20 = low.rolling(20).min()
    break_sig = np.where(
        close > high20 * 0.98, 0.2,
        np.where(close < low20 * 1.02, -0.2, 0.0))

    # 波动缩放
    vol_scale = np.where(atr_v > 0, np.minimum(1.0, 0.02 / atr_v.replace(0, np.nan)), 1.0)
    vol_scale = np.nan_to_num(vol_scale, nan=1.0)

    raw = (0.3 * rsi_sig + 0.25 * macd_sig +
           0.25 * trend_sig + 0.2 * break_sig) * vol_scale

    return pd.Series(raw, index=df.index)


def last_signal(df: pd.DataFrame, smooth: int = 5) -> float:
    """返回最后一天经过 smooth 期平滑并截断的 P08 信号"""
    raw = calc_raw_signal(df)
    sig = raw.dropna()
    if sig.empty:
        return 0.0
    smooth_vals = sig.iloc[-smooth:].mean()
    return float(smooth_vals)


# ---- 辅助: 趋势强度 / 治理质量分 (轮动打分用) ----
def trend_score(df: pd.DataFrame) -> float:
    """趋势分: 20/60/120 多头排列得高分, 空头排列得负分"""
    close = build_adj_close(df)   # P1: 复权close, 除权日不破坏均线排列
    if len(close) < 120:
        return 0.0
    s20, s60, s120 = ma(close, 20), ma(close, 60), ma(close, 120)
    c = close.iloc[-1]
    s20v, s60v, s120v = s20.iloc[-1], s60.iloc[-1], s120.iloc[-1]
    score = 0.0
    if c > s20v and s20v > s60v > s120v:
        score = 1.0
    elif c > s20v and s20v > s60v:
        score = 0.5
    elif c > s20v:
        score = 0.3
    elif c < s20v and s20v < s60v < s120v:
        score = -0.8
    elif c < s20v and s20v < s60v:
        score = -0.4
    else:
        score = 0.0
    return score


def governance_score(row: dict, fin: dict = None) -> float:
    """治理/质量分 (真实财务数据, 0..1)

    优先使用 financials 表真实指标打分 (roe / 负债率 / 盈利增速 / 现金流质量):
      * roe:             高ROE高分 (0..15% 线性, 15%+ 满分)
      * liability_ratio: 低负债高分 (0..80% 反向线性, 80%+ 接近0)
      * profit_yoy:      盈利同比正增长加分 (>=0 线性 0.5..1, 亏损负分)
      * operate_cf:      经营现金流为正加分 (每股经营现金流 > 0)
    四者加权为质量分 0..1; 再用估值快照(PB/PE)做负向过滤(剔除负净资产/极高PB/巨亏)。

    fin 为 None 或空 dict 时(无财务数据)回退到旧的估值快照占位逻辑, 保证兜底可用。
    """
    if not fin:
        return _governance_fallback(row)
    score = 0.0
    # --- ROE: 0..15% 线性, 满分封顶 ---
    roe = fin.get("roe")
    if isinstance(roe, (int, float)) and np.isfinite(roe):
        score += 0.45 * np.clip(roe / 15.0, 0.0, 1.0)
    # --- 负债率: 0..80% 反向线性 ---
    liab = fin.get("liability_ratio")
    if isinstance(liab, (int, float)) and np.isfinite(liab) and liab >= 0:
        score += 0.25 * np.clip((0.80 - liab) / 0.80, 0.0, 1.0)
    # --- 盈利同比: >=0 线性到 0.5..1, 负增长降分 ---
    yoy = fin.get("profit_yoy")
    if isinstance(yoy, (int, float)) and np.isfinite(yoy):
        if yoy >= 0:
            score += 0.2 * (0.5 + 0.5 * np.clip(yoy / 50.0, 0.0, 1.0))
        else:
            score += 0.2 * (0.5 * np.clip(1.0 + yoy / 50.0, 0.0, 1.0))
    else:
        score += 0.2 * 0.5  # 缺失给中性
    # --- 经营现金流为正 ---
    ocf = fin.get("operate_cf")
    if isinstance(ocf, (int, float)) and np.isfinite(ocf):
        score += 0.1 * (1.0 if ocf > 0 else 0.0)
    else:
        score += 0.1 * 0.5
    # --- 估值快照负向过滤 (与 fallback 一致的硬约束) ---
    pb = row.get("pb")
    pe = row.get("pe_ttm")
    if isinstance(pb, (int, float)) and np.isfinite(pb) and (pb < 0 or pb > 30):
        score *= 0.7      # 负净资产/极高PB 打7折
    if isinstance(pe, (int, float)) and np.isfinite(pe) and pe < 0:
        score *= 0.8      # 亏损(PE为负) 打8折
    return float(np.clip(score, 0.0, 1.0))


def _governance_fallback(row: dict) -> float:
    """无财务数据时的兜底: 基于估值快照 + 占位规则 (旧逻辑保留)。"""
    score = 0.5
    pb = row.get("pb")
    pe = row.get("pe_ttm")
    if isinstance(pb, (int, float)) and np.isfinite(pb):
        if pb < 0 or pb > 30:      # 负净资产或极高PB
            score -= 0.3
        elif pb <= 8:             # 低估值偏好
            score += 0.15
    if isinstance(pe, (int, float)) and np.isfinite(pe):
        if 0 < pe < 50:           # 盈利且估值合理
            score += 0.1
        elif pe <= 0:             # 亏损
            score -= 0.2
    return float(np.clip(score, 0.0, 1.0))


def liquidity_score(row: dict, max_amount: float) -> float:
    """流动性分: 按成交额占比归一化到 0..1"""
    amt = row.get("amount") or 0.0
    if not isinstance(amt, (int, float)) or max_amount <= 0:
        return 0.0
    return float(np.clip(amt / max_amount, 0.0, 1.0))


# ---- 实证因子: 低波动 alpha + 反转动量 alpha (IC 回算验证后引入) ----
def volatility_score(df: pd.DataFrame, window: int = 20) -> float:
    """低波动 alpha 分: 近 window 日日收益标准差越低分越高 (反向使用 vol)。

    IC回算: 波动因子 H20 IC=-0.09, ICIR=-0.47, 是三者中最稳的负向 alpha,
    即"低波动 -> 未来表现更好"。因此这里对低波动给高分。
    返回 0..1 (越低波越接近1)。
    """
    close = build_adj_close(df)   # P1: 复权close 的 pct_change 才是真实日收益
    if len(close) < window + 2:
        return 0.5  # 数据不足给中性
    ret = close.pct_change().dropna()
    recent = ret.iloc[-window:]
    std = recent.std()
    if not np.isfinite(std) or std <= 0:
        return 0.5
    # 用样本内 std 分位数将 std 映射到 0..1: 低波动 -> 高分
    # 简化映射: std 极小(0.5%)得高分, std 很大(8%)得低分
    lo, hi = 0.005, 0.08
    v = np.clip((hi - std) / (hi - lo), 0.0, 1.0)
    return float(v)


def momentum_reversal_score(df: pd.DataFrame, window: int = 20) -> float:
    """反转动量 alpha: 过去 window 日累计涨幅越低(近期跌得多)分越高.

    IC: 20日动量 H20=-0.07, 方向为反转——"过去涨得多的未来跌",
    所以此项对"近期超跌/低动量"给高分。
    返回 0..1 (动量越低越接近1)。
    """
    close = build_adj_close(df)   # P1: 复权close, 除权缺口不造成虚假超跌
    if len(close) < window + 2:
        return 0.5
    p0 = close.iloc[-window - 1]
    p1 = close.iloc[-1]
    if p0 <= 0 or p1 <= 0:
        return 0.5
    mom = p1 / p0 - 1.0  # 过去20日累计收益
    if not np.isfinite(mom):
        return 0.5
    # 动量 -> 反转分: mom 越负分越高. 以 ±20% 为宽幅归一化
    lo, hi = -0.25, 0.25
    v = np.clip((hi - mom) / (hi - lo), 0.0, 1.0)
    return float(v)