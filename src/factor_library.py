# -*- coding: utf-8 -*-
# ============================================================
# factor_library.py -- 基础因子库 (日频截面计算引擎)
#
# 从 h5i market.db 读取 daily_bars / financials / valuation，
# 逐截面计算 4 大类因子原始值，并支持 Winsorize -> OLS残差(ln_size+行业) -> z 打分。
#
# 因子分类:
#   价格动量类: ret_N, mom_fast_slow, rsi_14, kdj
#   波动率类:   hist_vol_N, atr_14, max_drawdown_N
#   量价结合类: volume_change_N, volume_ratio, turnover
#   基本面类:   ep, bp, roe, roe_yy_chg, rev_yoy, np_yoy
#
# CLI:
#   python factor_library.py compute 2026-09-04          # 单日原始因子值
#   python factor_library.py score 2026-09-04             # 单日截面打分
#   python factor_library.py list                          # 列出所有因子
#   python factor_library.py verify 2026-09-04            # 数据管道验证
# ============================================================

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Optional

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H5I_DB_PATH = os.path.join(_BASE, "data", "h5i", "market.db")
SYMBOLS_PQ = os.path.join(_BASE, "data", "h5i", "static", "symbols.parquet")
INDUSTRY_JSON = os.path.join(_BASE, "data", "industry_map.json")

_LOG = logging.getLogger("factor_library")

# ---------------------------------------------------------------------------
# P1 复权缺口修复: build_adj_close
# ---------------------------------------------------------------------------
def build_adj_close(df: pd.DataFrame) -> pd.Series:
    """用 change_pct 链重建"无除权缺口"的前复权 close (锚定最新收盘).

    语义: 原始 close 在除权日会骤变(缺口), 破坏均线/ATR/突破等依赖连续价格的
    信号. 交易所 change_pct(涨跌幅%) 本身已按前收盘复权口径计算, 因此令
    adj[t] / adj[t-1] = (1 + change_pct[t]/100), 并以最新一根 K 线为锚点
    (adj[-1] = raw_close[-1]) 反推整段, 得到与现价同尺度的复权序列.

    - df 需含列: close; 若有 change_pct 则用于缺口修复, 缺失行按 1.0 处理.
    - 无 change_pct 列或全 NaN: 返回原始 close (缩放比=1, 调用方自动回退).
    - 返回 pd.Series, 索引与 df 对齐 (行序须按时间升序).

    Examples
    --------
    df(close=[10, 11, 10.45], change_pct=[NaN, 10, -5]):
      a=[1, 1.1, 0.95]; adj=[10.0, 11.0, 10.45]  (10->11 +10%, 11->10.45 -5%)
    若某日 change_pct 与 close 不成比例(除权缺口), 链式重建可消除该缺口.
    """
    raw = pd.to_numeric(df.get("close"), errors="coerce").astype(float)
    if raw is None or len(raw) == 0:
        return pd.Series(dtype=float, index=df.index)
    has_chg = "change_pct" in df.columns
    chg = pd.to_numeric(df["change_pct"], errors="coerce") if has_chg else pd.Series(
        np.nan, index=df.index)
    # 无 change_pct 或全部缺失: 返回原始 close (缩放比=1)
    if not has_chg or not bool(chg.notna().any()):
        return raw.copy()
    out = raw.copy()
    a = (1.0 + chg.fillna(0.0) / 100.0).replace(0.0, np.nan).fillna(1.0)
    pref = a.cumprod()
    anchor_pref = float(pref.iloc[-1]) if len(pref) else 1.0
    last_close = float(raw.iloc[-1]) if np.isfinite(raw.iloc[-1]) else np.nan
    if np.isfinite(anchor_pref) and anchor_pref > 1e-12 and np.isfinite(last_close):
        out = last_close * pref / anchor_pref
    return out

# ---------------------------------------------------------------------------
# 旧 selector API 兼容层 (2026-09-07): selector.py 仍按旧签名引用
#   selector_weights() / score_factor(name, bars) / _pb_rev_score / _roe_score /
#   _mf_net_score. 语义与旧版一致 (线性裁切映射到 0..1), 使 realtime_engine 可导入.
# ---------------------------------------------------------------------------
def selector_weights() -> dict:
    """旧打分权重: 返回 config.SCORE_WEIGHTS 副本, 并经因子健康处置 (失效因子隔离)."""
    try:
        from config import SCORE_WEIGHTS
        W = dict(SCORE_WEIGHTS)
    except Exception:
        W = {"signal": 0.34, "trend": 0.14, "govern": 0.16, "liquidity": 0.08,
             "vol": 0.10, "mom_rev": 0.0, "pb_rev": 0.06, "roe": 0.06, "mf_net": 0.06}
    _apply_factor_health(W)
    return W


# 已打印的隔离原因 (避免同原因重复刷屏)
_health_logged: dict = {}


def _apply_factor_health(W: dict) -> None:
    """(2026-09-07) 因子健康处置: 短期方向翻转/强度收敛(反转义失效)的因子, 打分权重置 0.

    由 factor_gate.factor_health_flags 依据 data/ic 曲线判定; 环境变量
    FACTOR_HEALTH_ENABLED=0 可关闭. 任何异常静默降级(不影响选股主链路).
    """
    if os.environ.get("FACTOR_HEALTH_ENABLED", "1") == "0":
        return
    try:
        from factor_gate import factor_health_flags
        flags = factor_health_flags()
    except Exception:
        return
    for key, info in (flags.get("isolated") or {}).items():
        if key in W and float(W.get(key) or 0.0) > 0.0:
            W[key] = 0.0
            reason = str(info.get("reason", "方向失效"))
            if _health_logged.get(key) != reason:
                _health_logged[key] = reason
                print(f"[factor_health] 失效因子隔离: {key} 权重 -> 0 ({reason})",
                      flush=True)


def _clip_score(x: float, lo: float, hi: float, invert: bool = False) -> float:
    """线性裁切: x∈[lo,hi] -> [0,1]; invert=True 时低值高分."""
    if not np.isfinite(x):
        return 0.5
    t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return float(1.0 - t if invert else t)


def score_factor(name: str, bars: pd.DataFrame) -> float:
    """旧实证 alpha 打分 (0..1).

    - "vol":  低波动高分 (20日年化波动 10%~60% 线性裁切反向)
    - "mom":  反转动量高分 (近20日收益 -20%~+20% 线性裁切反向)
    - 其它:   0.5 (中性)
    """
    if bars is None or len(bars) < 21:
        return 0.5
    close = pd.to_numeric(bars["close"], errors="coerce").astype(float).dropna()
    if len(close) < 21:
        return 0.5
    rets = close.diff().dropna() / close.shift(1).dropna()
    rets = rets[np.isfinite(rets)]
    if name == "vol":
        if len(rets) < 20:
            return 0.5
        ann = float(rets.tail(20).std(ddof=1) * np.sqrt(252.0))
        return _clip_score(ann, 0.10, 0.60, invert=True)
    if name == "mom":
        if len(close) < 21:
            return 0.5
        ret20 = float(close.iloc[-1] / close.iloc[-21] - 1.0)
        return _clip_score(ret20, -0.20, 0.20, invert=True)
    return 0.5


def _pb_rev_score(pb) -> float:
    """低PB高分: pb∈[0.5, 3.0] 线性裁切反向; 缺失 0.5."""
    if pb is None:
        return 0.5
    try:
        return _clip_score(float(pb), 0.5, 3.0, invert=True)
    except (TypeError, ValueError):
        return 0.5


def _roe_score(roe) -> float:
    """高ROE高分: roe∈[0, 0.25] 线性裁切; 缺失 0.5."""
    if roe is None:
        return 0.5
    try:
        return _clip_score(float(roe), 0.0, 0.25, invert=False)
    except (TypeError, ValueError):
        return 0.5


def _mf_net_score(net_flow, amount) -> float:
    """资金净流入强度高分: net/amount ∈ [-5%, +5%] 线性裁切; 缺失 0.5."""
    if net_flow is None or amount is None:
        return 0.5
    try:
        n, a = float(net_flow), float(amount)
        if a <= 0:
            return 0.5
        return _clip_score(n / a, -0.05, 0.05, invert=False)
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------
# 因子注册表
# ---------------------------------------------------------------------------
FACTOR_REGISTRY = {
    # ========== 价格动量类 ==========
    "ret_5": {"category": "momentum", "desc": "过去5日收益率", "window": 5},
    "ret_10": {"category": "momentum", "desc": "过去10日收益率", "window": 10},
    "ret_20": {"category": "momentum", "desc": "过去20日收益率", "window": 20},
    "ret_60": {"category": "momentum", "desc": "过去60日收益率", "window": 60},
    "mom_5_20": {"category": "momentum", "desc": "短中期动量(ret_5 - ret_20)", "fast": 5, "slow": 20},
    "mom_10_60": {"category": "momentum", "desc": "中长期动量(ret_10 - ret_60)", "fast": 10, "slow": 60},
    "rsi_14": {"category": "momentum", "desc": "相对强弱指标 RSI(14)", "window": 14},
    "kdj_k": {"category": "momentum", "desc": "KDJ K值(9,3,3)", "window": 9},
    "kdj_d": {"category": "momentum", "desc": "KDJ D值(9,3,3)", "window": 9},
    "kdj_j": {"category": "momentum", "desc": "KDJ J值(9,3,3)", "window": 9},
    # ========== 波动率类 ==========
    "hist_vol_10": {"category": "volatility", "desc": "10日历史波动率(年化)", "window": 10},
    "hist_vol_20": {"category": "volatility", "desc": "20日历史波动率(年化)", "window": 20},
    "hist_vol_60": {"category": "volatility", "desc": "60日历史波动率(年化)", "window": 60},
    "atr_14": {"category": "volatility", "desc": "平均真实波幅 ATR(14)", "window": 14},
    "max_drawdown_20": {"category": "volatility", "desc": "20日最大回撤(%)", "window": 20},
    # ========== 量价结合类 ==========
    "volume_change_5": {"category": "volume", "desc": "5日成交量变化率", "window": 5},
    "turnover": {"category": "volume", "desc": "换手率(当日)", "window": 1},
    "volume_ratio": {"category": "volume", "desc": "量比(当日成交量/5日均量)", "window": 5},
    # ========== 基本面类 ==========
    "ep": {"category": "fundamental", "desc": "市盈率倒数 EP=1/pe_ttm"},
    "bp": {"category": "fundamental", "desc": "市净率倒数 BP=1/pb"},
    "roe": {"category": "fundamental", "desc": "净资产收益率 ROE"},
    "roe_yy_chg": {"category": "fundamental", "desc": "ROE同比变化"},
    "rev_yoy": {"category": "fundamental", "desc": "营收同比增速"},
    "np_yoy": {"category": "fundamental", "desc": "净利润同比增速"},
}

# 时序列因子（需要历史 bars，按类别分组）
TIME_SERIES_FACTORS = {
    "momentum": ["ret_5", "ret_10", "ret_20", "ret_60", "mom_5_20", "mom_10_60",
                 "rsi_14", "kdj_k", "kdj_d", "kdj_j"],
    "volatility": ["hist_vol_10", "hist_vol_20", "hist_vol_60", "atr_14", "max_drawdown_20"],
    "volume": ["volume_change_5", "turnover", "volume_ratio"],
}

# 基本面因子（不需要历史 bars，从 financials/valuation 快照获取）
FUNDAMENTAL_FACTORS = ["ep", "bp", "roe", "roe_yy_chg", "rev_yoy", "np_yoy"]

# 最大需要的历史窗口长度（用于确定回拉数据量）
MAX_HISTORY_NEEDED = 60  # ret_60 + buffer

# ---------------------------------------------------------------------------
# 惰性数据库连接
# ---------------------------------------------------------------------------
_DB = None
def _db():
    global _DB
    if _DB is None:
        import h5i_db
        _DB = h5i_db.Database(H5I_DB_PATH)
    return _DB


def _sql(q: str) -> pd.DataFrame:
    return _db().sql(q).to_pandas()


# ---------------------------------------------------------------------------
# 日历和静态数据
# ---------------------------------------------------------------------------
_CAL = None
def _calendar() -> list[str]:
    global _CAL
    if _CAL is None:
        df = _sql("SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars ORDER BY d")
        _CAL = [str(x) for x in df["d"].tolist()]
    return _CAL


def _snap_date(as_of) -> Optional[str]:
    """规整 as_of 到最近交易日."""
    try:
        D = str(pd.Timestamp(as_of).normalize())[:10]
    except (ValueError, TypeError):
        return None
    cal = _calendar()
    if not cal:
        return None
    if D >= cal[-1]:
        return cal[-1]
    if D < cal[0]:
        return None
    for d in reversed(cal):
        if d <= D:
            return d
    return None


_ACTIVE = None
def _active_szsh() -> set[str]:
    global _ACTIVE
    if _ACTIVE is None:
        df = pd.read_parquet(SYMBOLS_PQ)
        df["code"] = df["symbol"].astype(str).str.zfill(6)
        _ACTIVE = set(
            df.loc[df["is_active"] & df["market"].isin(["sz", "sh"]), "code"])
    return _ACTIVE


_INDUSTRY = None
def _load_industry() -> dict[str, str]:
    global _INDUSTRY
    if _INDUSTRY is None:
        im = json.load(open(INDUSTRY_JSON, encoding="utf-8")).get("map") or {}
        out: dict[str, str] = {}
        for k, v in im.items():
            code = str(k).split(".")[0]
            tags = v.get("tags") or []
            lv1 = tags[0].split("-")[0] if tags and tags[0] else "未知"
            out[code] = lv1
        _INDUSTRY = out
    return _INDUSTRY


# ---------------------------------------------------------------------------
# 批量获取历史 bars
# ---------------------------------------------------------------------------
def _fetch_bars(start_date: str, end_date: str) -> pd.DataFrame:
    """获取指定日期范围内所有活跃股票的 daily_bars 子集."""
    df = _sql(
        f"SELECT CAST(ts AS DATE) d, symbol, open, high, low, close, "
        f"volume, amount, change_pct, turnover "
        f"FROM daily_bars WHERE CAST(ts AS DATE) >= DATE '{start_date}' "
        f"AND CAST(ts AS DATE) <= DATE '{end_date}'")
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    active = _active_szsh()
    df = df[df["symbol"].isin(active)].copy()
    df = df.drop_duplicates(["symbol", "d"], keep="last").sort_values(["symbol", "d"])
    df = df.reset_index(drop=True)
    return df


# ===================================================================
# 因子计算函数 (私有)
# 每个函数接收 groupby 后的单股票 DataFrame, 返回最后一行的因子值
# ===================================================================

def _ret_N(g: pd.DataFrame, N: int) -> float:
    """N日收益率: close_{t} / close_{t-N} - 1"""
    if len(g) < N + 1:
        return np.nan
    p0 = g["close"].iloc[-N - 1]
    p1 = g["close"].iloc[-1]
    if p0 <= 0 or p1 <= 0:
        return np.nan
    return float(p1 / p0 - 1.0)


def _mom_fast_slow(g: pd.DataFrame, fast: int, slow: int) -> float:
    """动量差: ret_fast - ret_slow"""
    rf = _ret_N(g, fast)
    rs = _ret_N(g, slow)
    if not np.isfinite(rf) or not np.isfinite(rs):
        return np.nan
    return rf - rs


def _rsi(g: pd.DataFrame, N: int = 14) -> float:
    """RSI(14)."""
    if len(g) < N + 1:
        return np.nan
    closes = g["close"].values
    deltas = np.diff(closes[-(N + 1):])
    gains = deltas.clip(min=0)
    losses = (-deltas).clip(min=0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _kdj(g: pd.DataFrame, N: int = 9) -> tuple[float, float, float]:
    """KDJ(K, D, J). 标准三值平滑: K=2/3*prevK+1/3*RSV, D=2/3*prevD+1/3*K."""
    if len(g) < N * 2 - 1:  # 需要足够的窗口 recalculate
        return np.nan, np.nan, np.nan
    # 逐日计算 RSV 序列 (从最早到最晚), 然后递归平滑
    highs = g["high"].values
    lows = g["low"].values
    closes = g["close"].values
    L = len(highs)
    rsvs = []
    for i in range(L - N + 1, L + 1):
        hi = float(highs[i - N:i].max())
        lo = float(lows[i - N:i].min())
        c = float(closes[i - 1])
        if hi == lo:
            rsvs.append(50.0)
        else:
            rsvs.append((c - lo) / (hi - lo) * 100.0)
    # 递归平滑: 初始 K=D=50
    k, d = 50.0, 50.0
    for rsv in rsvs:
        k = 2.0 / 3.0 * k + 1.0 / 3.0 * rsv
        d = 2.0 / 3.0 * d + 1.0 / 3.0 * k
    j = 3.0 * k - 2.0 * d
    return float(k), float(d), float(j)


def _hist_vol(g: pd.DataFrame, N: int) -> float:
    """N日历史波动率(年化): std(change_pct) * sqrt(242)."""
    if len(g) < N + 1:
        return np.nan
    cp = g["change_pct"].values[-(N + 1):-1]  # 取 N 个日收益率
    if len(cp) < 5:
        return np.nan
    sd = float(np.std(cp, ddof=1))
    if not np.isfinite(sd):
        return np.nan
    return sd * np.sqrt(242.0)


def _atr(g: pd.DataFrame, N: int = 14) -> float:
    """ATR(14): N 日真实波幅均值."""
    if len(g) < N + 1:
        return np.nan
    closes = g["close"].values
    highs = g["high"].values
    lows = g["low"].values
    trs = []
    for i in range(-N, 0):
        hi = highs[i]
        lo = lows[i]
        pc = closes[i - 1]
        tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
        trs.append(tr)
    return float(np.mean(trs)) if trs else np.nan


def _max_drawdown(g: pd.DataFrame, N: int) -> float:
    """N日最大回撤(百分比): min(close / rolling_max - 1)."""
    if len(g) < N + 1:
        return np.nan
    closes = g["close"].values[-(N + 1):]
    rolling_max = np.maximum.accumulate(closes)
    dd = closes / rolling_max - 1.0
    return float(dd.min() * 100.0)


def _volume_change(g: pd.DataFrame, N: int) -> float:
    """N日成交量变化率: volume_t / volume_{t-N} - 1."""
    if len(g) < N + 1:
        return np.nan
    v0 = g["volume"].iloc[-N - 1]
    v1 = g["volume"].iloc[-1]
    if v0 <= 0 or v1 <= 0:
        return np.nan
    return float(v1 / v0 - 1.0)


def _volume_ratio(g: pd.DataFrame, N: int = 5) -> float:
    """量比: 当日成交量 / 过去 N 日均量(不含当日)."""
    if len(g) < N + 1:
        return np.nan
    vol_hist = g["volume"].values[-(N + 1):-1]
    vol_today = g["volume"].values[-1]
    avg_vol = float(vol_hist.mean())
    if avg_vol <= 0 or vol_today <= 0:
        return np.nan
    return float(vol_today / avg_vol)


# ===================================================================
# 主计算入口: 单日因子原始值
# ===================================================================

def compute_factors(as_of: str, factors: list[str] | None = None,
                    return_raw: bool = True) -> dict:
    """as_of 日收盘后计算全市场因子原始值.

    Args:
        as_of: 交易日 YYYY-MM-DD.
        factors: 因子名列表, None=全部.
        return_raw: True 返回 {symbol: {factor: value}};
                    False 返回 pd.DataFrame.

    Returns:
        dict with keys: {"as_of", "n_pool", "n_symbols", "factors": {...}}
    """
    t0 = time.time()
    D = _snap_date(as_of)
    if D is None:
        return {"as_of": str(as_of), "n_pool": 0, "n_symbols": 0,
                "factors": {}, "error": "no trading day"}

    if factors is None:
        factors = list(FACTOR_REGISTRY.keys())

    active = _active_szsh()
    industry = _load_industry()

    # 分解: 时序列因子 vs 基本面因子
    ts_factors = [f for f in factors if f not in FUNDAMENTAL_FACTORS]
    fd_factors = [f for f in factors if f in FUNDAMENTAL_FACTORS]

    # ---------- 1. 时序列因子: 需要历史 bars ----------
    ts_raw: dict[str, dict] = {}
    if ts_factors:
        cal = _calendar()
        d_idx = cal.index(D) if D in cal else -1
        start = cal[max(0, d_idx - MAX_HISTORY_NEEDED - 5)]
        bars = _fetch_bars(start, D)
        if bars.empty:
            return {"as_of": D, "n_pool": 0, "n_symbols": 0,
                    "factors": {}, "error": "no bars data"}

        for sym in bars["symbol"].unique():
            ts_raw.setdefault(sym, {})

        # 逐股票计算
        for sym, g in bars.groupby("symbol", sort=False):
            g = g.reset_index(drop=True)
            if len(g) < 5:
                continue
            out = ts_raw.setdefault(sym, {})
            for fname in ts_factors:
                meta = FACTOR_REGISTRY.get(fname, {})
                cat = meta.get("category", "")
                val = np.nan
                try:
                    if cat == "momentum":
                        if fname.startswith("ret_"):
                            val = _ret_N(g, meta["window"])
                        elif fname.startswith("mom_"):
                            val = _mom_fast_slow(g, meta["fast"], meta["slow"])
                        elif fname == "rsi_14":
                            val = _rsi(g, 14)
                        elif fname.startswith("kdj_"):
                            k, d, j = _kdj(g, 9)
                            if fname == "kdj_k":
                                val = k
                            elif fname == "kdj_d":
                                val = d
                            elif fname == "kdj_j":
                                val = j
                    elif cat == "volatility":
                        if fname.startswith("hist_vol_"):
                            val = _hist_vol(g, meta["window"])
                        elif fname == "atr_14":
                            val = _atr(g, 14)
                        elif fname == "max_drawdown_20":
                            val = _max_drawdown(g, 20)
                    elif cat == "volume":
                        if fname == "volume_change_5":
                            val = _volume_change(g, 5)
                        elif fname == "turnover":
                            val = float(g["turnover"].iloc[-1]) if pd.notna(g["turnover"].iloc[-1]) else np.nan
                        elif fname == "volume_ratio":
                            val = _volume_ratio(g, 5)
                except Exception:
                    val = np.nan
                out[fname] = val

    # ---------- 2. 基本面因子: 从 financials/valuation 快照 ----------
    if fd_factors:
        # 获取当日 bar (用于筛选 active 池)
        day_bars = _sql(f"SELECT symbol, close FROM daily_bars "
                        f"WHERE CAST(ts AS DATE)=DATE '{D}'")
        day_bars["symbol"] = day_bars["symbol"].astype(str).str.zfill(6)
        day_bars = day_bars[day_bars["symbol"].isin(active)].copy()
        day_bars = day_bars.dropna(subset=["close"])

        # 获取财务/估值数据 (含 roe_yy_chg: 当前 roe - 上年同期 roe)
        # 方法: 加载所有季度, 按 symbol 错位 4 季度对齐
        fin = _sql("SELECT CAST(ts AS DATE) ts, symbol, roe, rev_yoy, np_yoy "
                   "FROM financials")
        fin["symbol"] = fin["symbol"].astype(str).str.zfill(6)
        fin["ts"] = pd.to_datetime(fin["ts"])
        fin["avail"] = fin["ts"].map(_avail_date)
        # 只保留 PIT 可用的报告期
        fin = fin[fin["avail"] <= pd.Timestamp(D)].copy()
        # 按 (symbol, ts) 去重, 保留最新一条
        fin = fin.sort_values("ts").drop_duplicates(["symbol", "ts"], keep="last")
        # 计算 roe_yy_chg: 错位 4 季度对齐
        fin["kk"] = fin["ts"].dt.year * 4 + (fin["ts"].dt.month // 3 - 1)
        fin["kk_prev"] = fin["kk"] - 4
        lag = fin[["symbol", "kk", "roe"]].rename(columns={"kk": "kk_prev", "roe": "roe_yy_ago"})
        fin = fin.merge(lag, on=["symbol", "kk_prev"], how="left", suffixes=("", "_lag"))
        fin["roe_yy_chg"] = fin["roe"] - fin["roe_yy_ago"]
        # 取每个 symbol 最新一条 (PIT 可用日期最晚、且报告期最晚)
        fin = fin.sort_values(["symbol", "avail", "ts"]).drop_duplicates("symbol", keep="last")

        val = _sql(f"SELECT CAST(ts AS DATE) d, symbol, pe_ttm, pb "
                   f"FROM valuation WHERE CAST(ts AS DATE) <= DATE '{D}'")
        val["symbol"] = val["symbol"].astype(str).str.zfill(6)
        val["d"] = pd.to_datetime(val["d"])
        val = val.sort_values("d").drop_duplicates("symbol", keep="last")

        fd_map = fin.set_index("symbol")[["roe", "roe_yy_chg", "rev_yoy", "np_yoy"]].to_dict("index")
        val_map = val.set_index("symbol")[["pe_ttm", "pb"]].to_dict("index")

        for _, r in day_bars.iterrows():
            sym = r["symbol"]
            ts_raw.setdefault(sym, {})
            fv = fd_map.get(sym, {})
            vv = val_map.get(sym, {})
            if "ep" in fd_factors:
                pe = vv.get("pe_ttm")
                ts_raw[sym]["ep"] = 1.0 / pe if pe and pe > 0 else np.nan
            if "bp" in fd_factors:
                pb = vv.get("pb")
                ts_raw[sym]["bp"] = 1.0 / pb if pb and pb > 0 else np.nan
            if "roe" in fd_factors:
                ts_raw[sym]["roe"] = fv.get("roe", np.nan)
            if "roe_yy_chg" in fd_factors:
                ts_raw[sym]["roe_yy_chg"] = fv.get("roe_yy_chg", np.nan)
            if "rev_yoy" in fd_factors:
                ts_raw[sym]["rev_yoy"] = fv.get("rev_yoy", np.nan)
            if "np_yoy" in fd_factors:
                ts_raw[sym]["np_yoy"] = fv.get("np_yoy", np.nan)

    # ---------- 3. 组装结果 ----------
    n_pool = len(active)
    if return_raw:
        return {
            "as_of": D,
            "n_pool": n_pool,
            "n_symbols": len(ts_raw),
            "factors": ts_raw,
            "elapsed_s": round(time.time() - t0, 2),
        }
    # 返回 DataFrame
    rows = []
    for sym, fvals in ts_raw.items():
        row = {"symbol": sym}
        row.update(fvals)
        rows.append(row)
    df = pd.DataFrame(rows)
    return {
        "as_of": D,
        "n_pool": n_pool,
        "n_symbols": len(df),
        "df": df,
        "elapsed_s": round(time.time() - t0, 2),
    }


def _avail_date(ts: pd.Timestamp) -> pd.Timestamp:
    """财务报告可用日期映射."""
    y, m = ts.year, ts.month
    if m == 12:
        return pd.Timestamp(f"{y + 1}-04-30")
    if m == 3:
        return pd.Timestamp(f"{y}-04-30")
    if m == 6:
        return pd.Timestamp(f"{y}-08-31")
    if m == 9:
        return pd.Timestamp(f"{y}-10-31")
    return pd.Timestamp(f"{y}-12-31")


# ===================================================================
# 截面打分管道 (Winsorize -> OLS残差(ln_size+行业) -> z-score)
# 复用 factor_fusion 的完整中性化流程
# ===================================================================

MIN_CS_N = 30
MIN_POOL_N = 30


def _winsor(s: pd.Series, lo_q=0.01, hi_q=0.99) -> pd.Series:
    lo, hi = s.quantile(lo_q), s.quantile(hi_q)
    if pd.isna(lo):
        return s
    return s.clip(lo, hi)


def cross_section_scores(as_of: str, factors: list[str] | None = None,
                         neutralization: bool = True) -> dict:
    """单日截面因子打分.

    Args:
        as_of: 交易日 YYYY-MM-DD.
        factors: 因子列表, None=全部.
        neutralization: 是否中性化(ln_size+行业).

    Returns:
        dict with keys: {as_of, n_pool, n_scored, coverage,
                         factor_scores: {factor: {symbol: z}},
                         composite: {symbol: z} (等权合成)}
    """
    t0 = time.time()
    D = _snap_date(as_of)
    if D is None:
        return {"as_of": str(as_of), "error": "no trading day"}

    res = compute_factors(D, factors=factors, return_raw=False)
    df = res.get("df")
    if df is None or len(df) == 0:
        return {"as_of": D, "n_pool": res.get("n_pool", 0), "n_scored": 0,
                "coverage": 0.0, "error": "no factors computed"}

    if factors is None:
        factors = list(FACTOR_REGISTRY.keys())

    # 只保留有数据的因子列
    available = [f for f in factors if f in df.columns and df[f].notna().sum() >= MIN_CS_N]

    if not available:
        return {"as_of": D, "n_pool": len(df), "n_scored": 0,
                "coverage": 0.0, "error": "no factor with enough coverage"}

    # 添加 ln_size 和 industry 用于中性化
    industry = _load_industry()
    df["industry"] = df["symbol"].map(industry).fillna("未知")

    # 获取当日估值数据
    day_bars = _sql(f"SELECT symbol, close FROM daily_bars "
                    f"WHERE CAST(ts AS DATE)=DATE '{D}'")
    day_bars["symbol"] = day_bars["symbol"].astype(str).str.zfill(6)
    val = _sql(f"SELECT CAST(ts AS DATE) d, symbol, float_shares "
               f"FROM valuation WHERE CAST(ts AS DATE) <= DATE '{D}'")
    val["symbol"] = val["symbol"].astype(str).str.zfill(6)
    val["d"] = pd.to_datetime(val["d"])
    val = val.sort_values("d").drop_duplicates("symbol", keep="last")
    val_map = val.set_index("symbol")["float_shares"].to_dict()
    close_map = day_bars.set_index("symbol")["close"].to_dict()

    df["ln_size"] = np.nan
    for i, r in df.iterrows():
        sym = r["symbol"]
        close = close_map.get(sym)
        fs = val_map.get(sym)
        if close and close > 0 and fs and fs > 0:
            df.at[i, "ln_size"] = float(np.log(close * fs))

    # 逐因子打分
    factor_scores: dict[str, dict] = {}
    factor_meta: dict[str, dict] = {}
    for fname in available:
        if neutralization:
            zmap, meta = _residualize(df, fname)
        else:
            raw = df[fname].dropna()
            if len(raw) < MIN_CS_N:
                continue
            zmap = {s: float(v) for s, v in zip(raw.index, raw.values)}
            meta = {"n": len(raw), "ok": True}
        factor_scores[fname] = zmap
        factor_meta[fname] = meta

    # 等权合成 composite
    syms = set()
    for zmap in factor_scores.values():
        syms.update(zmap.keys())
    comp = {}
    for s in syms:
        vals = [zmap.get(s) for zmap in factor_scores.values()
                if s in zmap and np.isfinite(zmap[s])]
        if vals:
            comp[s] = float(np.mean(vals))
    # 合成后 z-score
    if comp:
        cv = np.array(list(comp.values()))
        mu, sd = float(cv.mean()), float(cv.std())
        if sd > 1e-12:
            comp = {s: (v - mu) / sd for s, v in comp.items()}

    return {
        "as_of": D,
        "n_pool": len(df),
        "n_scored": len(comp),
        "coverage": round(len(comp) / len(df), 4) if len(df) else 0.0,
        "factor_scores": factor_scores,
        "factor_meta": factor_meta,
        "composite": comp,
        "elapsed_s": round(time.time() - t0, 2),
    }


def _residualize(df: pd.DataFrame, factor: str) -> tuple[dict, dict]:
    """逐因子中性化: Winsorize -> OLS(ln_size + 行业) -> z."""
    ok = (df[factor].notna() & df["ln_size"].notna()
          & np.isfinite(df[factor].to_numpy()))
    n = int(ok.sum())
    if n < MIN_CS_N:
        return {}, {"n": n, "ok": False}
    sub = df.loc[ok, ["symbol", factor, "ln_size", "industry"]].copy()
    y = _winsor(sub[factor]).to_numpy(dtype=float)
    size = sub["ln_size"].to_numpy(dtype=float)
    dummies = pd.get_dummies(sub["industry"].fillna("未知"), prefix="", prefix_sep="")
    X = np.hstack([size.reshape(-1, 1), dummies.to_numpy(dtype=float)])
    try:
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
    except Exception:
        return {}, {"n": n, "ok": False, "error": "lstsq failed"}
    sd = float(resid.std())
    if not np.isfinite(sd) or sd <= 1e-12:
        return {}, {"n": n, "ok": False, "degen": True}
    z = (resid - resid.mean()) / sd
    out = {s: float(v) for s, v in zip(sub["symbol"], z)}
    return out, {"n": n, "ok": True}


# ===================================================================
# 数据管道验证
# ===================================================================

def verify_pipeline(as_of: str | None = None) -> dict:
    """验证数据管道通畅性: 检查各表数据可用性、因子覆盖率."""
    t0 = time.time()
    D = _snap_date(as_of) if as_of else _calendar()[-1]
    if D is None:
        return {"ok": False, "error": "no trading day"}

    results = {}
    # 1. daily_bars
    bars = _sql(f"SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms "
                f"FROM daily_bars WHERE CAST(ts AS DATE)=DATE '{D}'")
    results["daily_bars"] = {
        "n_rows": int(bars["n"].iloc[0]),
        "n_symbols": int(bars["syms"].iloc[0]),
    }

    # 2. financials (最近一期)
    fin = _sql("SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms FROM financials")
    results["financials"] = {
        "n_rows": int(fin["n"].iloc[0]),
        "n_symbols": int(fin["syms"].iloc[0]),
    }

    # 3. valuation
    val = _sql("SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms FROM valuation")
    results["valuation"] = {
        "n_rows": int(val["n"].iloc[0]),
        "n_symbols": int(val["syms"].iloc[0]),
    }

    # 4. 因子计算
    fac = compute_factors(D, return_raw=False)
    df = fac.get("df")
    results["factor_compute"] = {
        "n_symbols": len(df) if df is not None else 0,
        "n_pool": fac.get("n_pool", 0),
        "elapsed_s": fac.get("elapsed_s", 0),
    }

    # 5. 各因子覆盖率
    if df is not None and len(df):
        coverage = {}
        for fname in FACTOR_REGISTRY:
            if fname in df.columns:
                cov = float(df[fname].notna().sum() / len(df))
                coverage[fname] = round(cov, 4)
        results["factor_coverage"] = coverage

    results["as_of"] = D
    results["ok"] = True
    results["elapsed_s"] = round(time.time() - t0, 2)
    return results


# ===================================================================
# CLI
# ===================================================================

def _list_factors():
    """打印因子注册表."""
    print(f"{'因子名':<20} {'分类':<15} {'描述':<40}")
    print("-" * 75)
    for name, meta in FACTOR_REGISTRY.items():
        print(f"{name:<20} {meta['category']:<15} {meta['desc']:<40}")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("用法: python factor_library.py <command> [args]")
        print("命令: compute, score, list, verify")
        return 1

    cmd = sys.argv[1]
    if cmd == "list":
        _list_factors()
        return 0

    day = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "verify":
        r = verify_pipeline(day)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0

    if cmd == "compute":
        factors = sys.argv[3:] if len(sys.argv) > 3 else None
        r = compute_factors(day, factors=factors, return_raw=False)
        df = r.get("df")
        if df is not None and len(df):
            print(f"as_of={r['as_of']}  symbols={r['n_symbols']}  pool={r['n_pool']}  "
                  f"elapsed={r['elapsed_s']}s")
            print(f"\n前5行因子样本:")
            print(df.head(5).to_string())
            # 输出各因子统计
            avail = [c for c in FACTOR_REGISTRY if c in df.columns]
            print(f"\n因子覆盖率 ({len(avail)}个):")
            for f in avail:
                n = int(df[f].notna().sum())
                print(f"  {f:<20} n={n:>6d}  cov={n/len(df):.2%}  "
                      f"mean={df[f].mean():>10.4f}  std={df[f].std():>10.4f}")
        else:
            print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0

    if cmd == "score":
        factors = sys.argv[3:] if len(sys.argv) > 3 else None
        r = cross_section_scores(day, factors=factors)
        n_scored = len(r.get("composite", {}))
        print(f"as_of={r.get('as_of')}  pool={r.get('n_pool')}  "
              f"scored={n_scored}  cov={r.get('coverage'):.2%}  "
              f"elapsed={r.get('elapsed_s')}s")
        print(f"因子打分详情:")
        for fname, zmap in r.get("factor_scores", {}).items():
            meta = r.get("factor_meta", {}).get(fname, {})
            print(f"  {fname:<20} n={meta.get('n', 0):>6d}  ok={meta.get('ok')}")
        if n_scored:
            comp = r["composite"]
            top5 = sorted(comp.items(), key=lambda x: -x[1])[:5]
            bot5 = sorted(comp.items(), key=lambda x: x[1])[:5]
            print(f"\n合成分 Top5: {top5}")
            print(f"合成分 Bottom5: {bot5}")
        return 0

    print(f"未知命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())