# -*- coding: utf-8 -*-
# ============================================================
# factor_fusion.py -- 四因子 ICIR 加权融合打分 (方案A: 替换 f_ml)
#
# 背景
# ----
# 旧 f_ml 由 ml_fusion_bridge.compute_fml 调外部 ml_fusion venv 现算
# (XGB+LightGBM+CatBoost Stacking)。本模块提供与 f_ml 同角色的
# "横截面中性化四因子融合分数 fused score", 数据 100% 来自
#   data/h5i/market.db  (daily_bars / financials / valuation)
#   data/h5i/static/symbols.parquet
#   data/industry_map.json
# 不触碰 h5i 库文件、不引用 DuckDB、不启动常驻进程。
#
# 因子与融合参数 (由月度中性化面板 m6_neutralize post-ICIR 核定):
#   pb_inv     权重 0.7300  方向 +1 (高值做多)
#   ep         权重 0.7456  方向 +1 (高值做多)
#   ocf_ps     权重 0.5460  方向 +1 (高值做多)
#   roe_yy_chg 权重 1.0618  方向 -1 (负 IC, 低值做多, 取值反号)
#
# 截面处理 (每个 as_of 日独立执行):
#   候选池 -> 逐股组装原始因子 -> 逐因子 1%/99% Winsorize
#            -> OLS 残差 (ln_size + 一级行业哑变量, 缺失行业归'未知')
#            -> z-score -> 按方向*权重线性合成 -> 合成后再 z-score.
#
# 替换点
# ----
# target_weighting.ensure_target_weights 的 fml 分支 / selector._blend_fml
# 统一经 fusion_or_fml(items, as_of) -> (scores_dict, used), used ∈
#   'fusion' | 'fml_fallback' | 'equal'
# 环境变量开关:
#   FUSION_SCORE=1 (默认) 启用融合; =0 走旧 f_ml 路径
#   FORCE_FML=1           强制走旧 ml_fusion_bridge.compute_fml
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

H5I_DB_PATH = os.path.join(_BASE, "data", "h5i", "market.db")
SYMBOLS_PQ = os.path.join(_BASE, "data", "h5i", "static", "symbols.parquet")
INDUSTRY_JSON = os.path.join(_BASE, "data", "industry_map.json")

_LOG = logging.getLogger("factor_fusion")

# ---------------------------------------------------------------------------
# 融合参数
# ---------------------------------------------------------------------------
FACTOR_WEIGHTS: dict[str, float] = {
    "pb_inv": 0.7300,
    "ep": 0.7456,
    "ocf_ps": 0.5460,
    "roe_yy_chg": 1.0618,
}
DIRECTIONS: dict[str, int] = {  # 合成符号: +1 直接用 z, -1 取反
    "pb_inv": 1,
    "ep": 1,
    "ocf_ps": 1,
    "roe_yy_chg": -1,
}
MIN_CS_N = 30          # 单因子截面最小样本(不足则该日该因子置空)
MIN_POOL_N = 30        # 每日可出分的最小池
MIN_COVERAGE = 0.80    # fusion_or_fml 覆盖门槛 (请求标的中有分比例)

# 报告期可用性(PIT)映射: 与 m5_rebuild / 交易所披露规则一致
def _avail_date(ts: pd.Timestamp) -> pd.Timestamp:
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


def _is_on() -> bool:
    """FUSION_SCORE 默认开; FORCE_FML=1 强制走旧路径."""
    if os.environ.get("FORCE_FML", "0") == "1":
        return False
    return os.environ.get("FUSION_SCORE", "1") != "0"


# ---------------------------------------------------------------------------
# 惰性数据库/静态数据
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


_SYMBOL_DF = None
def load_symbols() -> pd.DataFrame:
    """symbols.parquet (raw). 列: symbol/market/list_date/is_active/float_shares."""
    global _SYMBOL_DF
    if _SYMBOL_DF is None:
        df = pd.read_parquet(SYMBOLS_PQ)
        df = df.copy()
        df["code"] = df["symbol"].astype(str).str.zfill(6)
        _SYMBOL_DF = df
    return _SYMBOL_DF


_ACTIVE_SZSH = None
def _active_szsh() -> set[str]:
    global _ACTIVE_SZSH
    if _ACTIVE_SZSH is None:
        d = load_symbols()
        _ACTIVE_SZSH = set(
            d.loc[d["is_active"] & d["market"].isin(["sz", "sh"]), "code"])
    return _ACTIVE_SZSH


_FLOAT_MAP = None
def load_float_shares() -> dict[str, float]:
    """symbols.parquet 的 float_shares (接口层; 目前 static 文件该列全空,
    实际流通股数取 valuation.float_shares 见 _snapshot_at, 此为兜底映射)."""
    global _FLOAT_MAP
    if _FLOAT_MAP is None:
        d = load_symbols()
        _FLOAT_MAP = {r.code: (float(r.float_shares)
                               if pd.notna(r.float_shares) else float("nan"))
                      for r in d.itertuples()}
    return _FLOAT_MAP


_INDUSTRY = None
def load_industry() -> dict[str, str]:
    """industry_map.json -> {裸6位symbol: 一级行业(tags[0]第一段)}."""
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
# financials 全表缓存 (PIT 派生: avail / roe_yy_chg)
# ---------------------------------------------------------------------------
_FIN = None
_CAL = None
def _calendar() -> list[pd.Timestamp]:
    global _CAL
    if _CAL is None:
        days = _sql("SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars ORDER BY 1")
        _CAL = pd.to_datetime(days["d"]).dt.normalize().tolist()
    return _CAL


def _fin_frame() -> pd.DataFrame:
    global _FIN
    if _FIN is not None:
        return _FIN
    t0 = time.time()
    df = _sql("SELECT CAST(ts AS DATE) ts, symbol, revenue, rev_yoy, net_profit, "
              "np_yoy, ded_np_yoy, eps, bvps, ocf_ps, net_margin, roe, "
              "debt_ratio, gross_margin FROM financials")
    df["ts"] = pd.to_datetime(df["ts"])
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df = df[df["ts"].dt.month.isin([3, 6, 9, 12])].copy()
    df["avail"] = df["ts"].map(_avail_date)
    # roe_yy_chg = 本报告期 roe - 上年同期报告期 roe (错位 4 个季度, 无前视)
    df["kk"] = df["ts"].dt.year * 4 + (df["ts"].dt.month // 3 - 1)
    lag = df[["symbol", "kk", "roe"]].rename(columns={"roe": "roe_yy_ago"})
    df = df.assign(kk_prev=df["kk"] - 4)
    df = df.merge(lag, left_on=["symbol", "kk_prev"], right_on=["symbol", "kk"],
                  how="left", suffixes=("", "_y"))
    df["roe_yy_chg"] = df["roe"] - df["roe_yy_ago"]
    df = df.drop(columns=["kk_y", "kk_prev", "roe_yy_ago"])
    # 同 (symbol, 报告期) 重复只留一条; 同 (symbol, 可用日) 保留报告期最新一条
    df = (df.sort_values(["symbol", "ts"])
            .drop_duplicates(["symbol", "ts"], keep="last")
            .sort_values(["symbol", "avail", "ts"])
            .drop_duplicates(["symbol", "avail"], keep="last"))
    _FIN = df[["symbol", "ts", "avail", "roe", "roe_yy_chg", "eps",
               "ocf_ps", "np_yoy", "rev_yoy", "ded_np_yoy",
               "bvps"]].reset_index(drop=True)
    _LOG.debug("fin_frame loaded rows=%d %.0fs", len(_FIN), time.time() - t0)
    return _FIN


# valuation 范围缓存 (按需从 lo 加载)
_VAL = None
_VAL_LO = None
def _val_frame(lo: pd.Timestamp) -> pd.DataFrame:
    global _VAL, _VAL_LO
    if _VAL is not None and (lo is None or (_VAL_LO is not None and lo >= _VAL_LO)):
        return _VAL
    t0 = time.time()
    lo_s = (lo or pd.Timestamp("1990-01-01")).strftime("%Y-%m-%d")
    df = _sql("SELECT CAST(ts AS DATE) d, symbol, pe_ttm, pb, float_shares, is_st "
              f"FROM valuation WHERE CAST(ts AS DATE) >= DATE '{lo_s}'")
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["d"] = pd.to_datetime(df["d"])
    df = df.drop_duplicates(["symbol", "d"], keep="last")
    df = df.sort_values("d").reset_index(drop=True)
    _VAL, _VAL_LO = df, lo
    _LOG.debug("val_frame loaded since=%s rows=%d %.0fs", lo_s, len(df),
               time.time() - t0)
    return _VAL


# ---------------------------------------------------------------------------
# 公开 PIT 查询接口
# ---------------------------------------------------------------------------
def _canon_code(x) -> str:
    return str(x).split(".")[0]


def pit_financials(symbol, as_of: str) -> Optional[dict]:
    """symbol 在 as_of 时最新可见一期报告(无前视).

    返回 {roe, roe_yy_chg, eps, ocf_ps, np_yoy, rev_yoy, ded_np_yoy,
          period:'YYYY-MM-DD', avail:'YYYY-MM-DD'}; 无则 None.
    """
    code = _canon_code(symbol)
    D = pd.Timestamp(str(as_of)[:10])
    fin = _fin_frame()
    sub = fin[(fin["symbol"] == code) & (fin["avail"] <= D)]
    if sub.empty:
        return None
    r = sub.sort_values("ts").iloc[-1]
    return {
        "roe": float(r["roe"]) if pd.notna(r["roe"]) else None,
        "roe_yy_chg": float(r["roe_yy_chg"]) if pd.notna(r["roe_yy_chg"]) else None,
        "eps": float(r["eps"]) if pd.notna(r["eps"]) else None,
        "ocf_ps": float(r["ocf_ps"]) if pd.notna(r["ocf_ps"]) else None,
        "np_yoy": float(r["np_yoy"]) if pd.notna(r["np_yoy"]) else None,
        "rev_yoy": float(r["rev_yoy"]) if pd.notna(r["rev_yoy"]) else None,
        "ded_np_yoy": float(r["ded_np_yoy"]) if pd.notna(r["ded_np_yoy"]) else None,
        "period": str(r["ts"].date()),
        "avail": str(r["avail"].date()),
    }


def valuation_latest(symbol, as_of: str) -> Optional[dict]:
    """valuation 表 ts<=as_of 最近一期 (pe_ttm/pb/float_shares/is_st)."""
    code = _canon_code(symbol)
    D = pd.Timestamp(str(as_of)[:10])
    v = _val_frame(D - pd.Timedelta(days=730))
    sub = v[(v["symbol"] == code) & (v["d"] <= D)]
    if sub.empty:
        return None
    r = sub.sort_values("d").iloc[-1]
    return {
        "pe_ttm": float(r["pe_ttm"]) if pd.notna(r["pe_ttm"]) else None,
        "pb": float(r["pb"]) if pd.notna(r["pb"]) else None,
        "float_shares": float(r["float_shares"]) if pd.notna(r["float_shares"]) else None,
        "is_st": bool(r["is_st"]) if pd.notna(r["is_st"]) else None,
        "val_date": str(r["d"].date()),
    }


# ---------------------------------------------------------------------------
# 单截面组装: 某日候选池 + 财务/估值快照 (dict 式快照, 支持跨日滚动)
# ---------------------------------------------------------------------------
def _snap_date(as_of) -> Optional[pd.Timestamp]:
    """把 as_of 规整到 <= as_of 的最近交易日."""
    try:
        D = pd.Timestamp(str(as_of)[:10]).normalize()
    except (ValueError, TypeError):
        return None
    cal = _calendar()
    if not cal:
        return None
    if D >= cal[-1]:
        return cal[-1]
    lo, hi = 0, len(cal) - 1
    if D < cal[0]:
        return None
    # binary search last cal <= D
    while lo <= hi:
        mid = (lo + hi) // 2
        if cal[mid] <= D:
            lo = mid + 1
        else:
            hi = mid - 1
    return cal[hi]


def _bar_day(D: pd.Timestamp) -> pd.DataFrame:
    dstr = D.strftime("%Y-%m-%d")
    df = _sql(f"SELECT symbol, close, change_pct FROM daily_bars "
              f"WHERE CAST(ts AS DATE)=DATE '{dstr}'")
    df["symbol"] = df["symbol"].astype(str)
    df = df[df["symbol"].str.fullmatch(r"\d{6}")].copy()
    df["symbol"] = df["symbol"].str.zfill(6)
    return df


class _Snap:
    """按日期推进维护 {symbol: 最新可见行} 快照 (财务用 avail, 估值用 ts 日期).

    advance 用 np.searchsorted 定位新增区间(块), 仅在块内按 symbol 取 tail,
    避免逐行 Python iloc 扫描全表.
    """

    def __init__(self, fin: pd.DataFrame, val: pd.DataFrame):
        self.fin = fin.sort_values("avail").reset_index(drop=True)
        self.val = val.sort_values("d").reset_index(drop=True)
        self._f_key = self.fin["avail"].to_numpy()          # datetime64
        self._v_key = self.val["d"].to_numpy()
        self.fp = 0
        self.vp = 0
        self.fmap: dict[str, dict] = {}
        self.vmap: dict[str, dict] = {}
        self._cur: Optional[pd.Timestamp] = None

    def advance(self, upto: pd.Timestamp):
        if self._cur is not None and upto <= self._cur:
            return
        u = np.datetime64(upto.normalize())
        # financials: avail <= upto
        pos = int(np.searchsorted(self._f_key, u, side="right"))
        if pos > self.fp:
            blk = self.fin.iloc[self.fp:pos]
            self.fp = pos
            if len(blk):
                tail = blk.drop_duplicates("symbol", keep="last")
                for r in tail.itertuples(index=False):
                    self.fmap[r.symbol] = {
                        "roe": r.roe, "roe_yy_chg": r.roe_yy_chg,
                        "eps": r.eps, "ocf_ps": r.ocf_ps, "np_yoy": r.np_yoy,
                        "rev_yoy": r.rev_yoy, "bvps": r.bvps,
                    }
        # valuation: d <= upto
        pos = int(np.searchsorted(self._v_key, u, side="right"))
        if pos > self.vp:
            blk = self.val.iloc[self.vp:pos]
            self.vp = pos
            if len(blk):
                tail = blk.drop_duplicates("symbol", keep="last")
                for r in tail.itertuples(index=False):
                    self.vmap[r.symbol] = {
                        "pe_ttm": r.pe_ttm, "pb": r.pb,
                        "float_shares": r.float_shares, "is_st": r.is_st,
                    }
        self._cur = upto.normalize()


def _assemble_snapshot(snap: _Snap, bar: pd.DataFrame,
                       active: set[str]) -> pd.DataFrame:
    """bar: 当日 bar (symbol/close/change_pct) -> 截面行:
    symbol/close/chg/ln_size/industry/pe_ttm/pb/roe/roe_yy_chg/eps/ocf_ps."""
    bar = bar[bar["symbol"].isin(active)].copy()
    bar = bar.dropna(subset=["close", "change_pct"])
    bar = bar[bar["change_pct"].abs() <= 9.5]
    ind = load_industry()
    rows = []
    for r in bar.itertuples(index=False):
        sym = r.symbol
        v = snap.vmap.get(sym)
        f = snap.fmap.get(sym)
        fs = float(v["float_shares"]) if v and pd.notna(v["float_shares"]) else float("nan")
        if not np.isfinite(fs) or fs <= 0 or not np.isfinite(r.close) or r.close <= 0:
            ln_size = float("nan")
        else:
            ln_size = float(np.log(r.close * fs))
        rows.append({
            "symbol": sym,
            "close": float(r.close),
            "chg": float(r.change_pct),
            "ln_size": ln_size,
            "industry": ind.get(sym, "未知"),
            "pe_ttm": (float(v["pe_ttm"]) if v and pd.notna(v["pe_ttm"]) else np.nan),
            "pb": (float(v["pb"]) if v and pd.notna(v["pb"]) else np.nan),
            "roe": (float(f["roe"]) if f and pd.notna(f["roe"]) else np.nan),
            "roe_yy_chg": (float(f["roe_yy_chg"]) if f and pd.notna(f["roe_yy_chg"]) else np.nan),
            "eps": (float(f["eps"]) if f and pd.notna(f["eps"]) else np.nan),
            "ocf_ps": (float(f["ocf_ps"]) if f and pd.notna(f["ocf_ps"]) else np.nan),
            "np_yoy": (float(f["np_yoy"]) if f and pd.notna(f["np_yoy"]) else np.nan),
            "rev_yoy": (float(f["rev_yoy"]) if f and pd.notna(f["rev_yoy"]) else np.nan),
            "bvps": (float(f["bvps"]) if f and pd.notna(f["bvps"]) else np.nan),
        })
    df = pd.DataFrame(rows)
    if len(df):
        for c in ["pe_ttm", "pb", "roe", "roe_yy_chg", "eps", "ocf_ps", "np_yoy",
                  "rev_yoy", "bvps"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # pb_inv / ep 原值
        df["pb_inv"] = np.where(df["pb"].notna() & (df["pb"] > 0), 1.0 / df["pb"], np.nan)
        df["ep"] = np.where(df["pe_ttm"].notna() & (df["pe_ttm"] > 0), 1.0 / df["pe_ttm"], np.nan)
    return df


# ---------------------------------------------------------------------------
# 截面中性化打分核心: Winsorize -> OLS(ln_size+行业) -> z
# ---------------------------------------------------------------------------
def _winsor(s: pd.Series, lo_q=0.01, hi_q=0.99) -> pd.Series:
    lo, hi = s.quantile(lo_q), s.quantile(hi_q)
    if pd.isna(lo):
        return s
    return s.clip(lo, hi)


def _residualize(df: pd.DataFrame, factor: str) -> tuple[dict, dict]:
    """逐日单因子中性化. 返回 ({symbol: z}, meta)."""
    ok = df[factor].notna() & df["ln_size"].notna() & np.isfinite(df[factor].to_numpy())
    n = int(ok.sum())
    if n < MIN_CS_N:
        return {}, {"n": n, "ok": False}
    sub = df.loc[ok, ["symbol", factor, "ln_size", "industry"]].copy()
    y = _winsor(sub[factor]).to_numpy(dtype=float)
    size = sub["ln_size"].to_numpy(dtype=float)
    dummies = pd.get_dummies(sub["industry"].fillna("未知"), prefix="", prefix_sep="")
    ind_cols = list(dummies.columns)
    X = np.hstack([size.reshape(-1, 1), dummies.to_numpy(dtype=float)])
    try:
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
    except Exception as e:  # noqa: BLE001
        return {}, {"n": n, "ok": False, "error": f"{type(e).__name__}: {e}"}
    sd = float(resid.std())
    y_sd = float(y.std())
    if not np.isfinite(sd) or sd <= 1e-12 or y_sd <= 1e-12 or sd / max(y_sd, 1e-12) < 1e-6:
        return {}, {"n": n, "ok": False, "degen": True}
    z = (resid - resid.mean()) / sd
    out = {s: float(v) for s, v in zip(sub["symbol"], z)}
    # 正交快检
    zs = pd.Series(z, index=sub["symbol"])
    r_size = float(np.corrcoef(zs, sub["ln_size"].to_numpy())[0, 1]) if n > 1 else np.nan
    r_ind = []
    for c in ind_cols:
        dd = dummies[c].to_numpy()
        if dd.std() > 0 and n > 1:
            r_ind.append(float(np.corrcoef(zs, dd)[0, 1]))
    meta = {
        "n": n, "ok": True, "winsor": [float(y.min()), float(y.max())],
        "r_size": (round(r_size, 4) if np.isfinite(r_size) else None),
        "mean_abs_r_ind": (round(float(np.mean(np.abs(r_ind))), 4) if r_ind else None),
        "worst_ind": (round(float(np.max(np.abs(r_ind))), 4) if r_ind else None),
    }
    return out, meta


def cross_section_scores(as_of: str, symbols=None) -> dict:
    """as_of 日收盘后全自动候选池截面出分 (供验证与生产).

    Returns {"as_of": str(交易日), "n_pool": int, "n_scored": int,
             "coverage": float, "scores": {裸6位symbol: float}, "meta": {...}}
    若当日无池/退化 -> n_pool=0, scores={} (绝不抛异常).
    """
    t0 = time.time()
    D = _snap_date(as_of)
    if D is None:
        return {"as_of": str(as_of), "n_pool": 0, "n_scored": 0,
                "coverage": 0.0, "scores": {}, "meta": {"error": "no trading day"}}
    dstr = D.strftime("%Y-%m-%d")
    active = _active_szsh()
    fin = _fin_frame()
    val = _val_frame(D - pd.Timedelta(days=730))
    snap = _Snap(fin, val)
    snap.advance(D)
    bar = _bar_day(D)
    df = _assemble_snapshot(snap, bar, active)
    if len(df) < MIN_POOL_N:
        return {"as_of": dstr, "n_pool": int(len(df)), "n_scored": 0,
                "coverage": 0.0, "scores": {}, "meta": {"pool_too_small": int(len(df))}}
    return _score_df(df, dstr, elapsed=time.time() - t0)


def _score_df(df: pd.DataFrame, dstr: str, elapsed: float = 0.0) -> dict:
    scores: dict[str, float] = {}
    z_by_factor: dict[str, dict] = {}
    factor_meta: dict[str, dict] = {}
    for f in FACTOR_WEIGHTS:
        if f not in df.columns:
            continue
        zm, meta = _residualize(df, f)
        z_by_factor[f] = zm
        factor_meta[f] = meta
    # 合成: 至少 1 个因子有残差 z
    syms = sorted(set().union(*[set(z.keys()) for z in z_by_factor.values()])
                  if z_by_factor else set())
    if not syms:
        return {"as_of": dstr, "n_pool": int(len(df)), "n_scored": 0,
                "coverage": 0.0, "scores": {}, "meta": {"no_factor": True,
                                                        "factor_meta": factor_meta}}
    comp = {}
    for s in syms:
        raw = 0.0
        for f, w in FACTOR_WEIGHTS.items():
            zm = z_by_factor.get(f)
            if not zm or s not in zm:
                continue
            raw += w * DIRECTIONS[f] * zm[s]
        comp[s] = raw
    cv = np.array(list(comp.values()), dtype=float)
    sd = float(cv.std())
    if len(comp) < MIN_POOL_N or not np.isfinite(sd) or sd <= 1e-12:
        return {"as_of": dstr, "n_pool": int(len(df)), "n_scored": 0,
                "coverage": 0.0, "scores": {}, "meta": {"composite_degen": True,
                                                        "n_comp": len(comp),
                                                        "factor_meta": factor_meta}}
    mean_c = float(cv.mean())
    for s, v in comp.items():
        scores[s] = float((v - mean_c) / sd)

    # 正交性快检 (合成后)
    scored_df = df[df["symbol"].isin(scores)].copy()
    size_arr = scored_df["ln_size"].to_numpy(dtype=float)
    score_arr = np.array([scores[s] for s in scored_df["symbol"]], dtype=float)
    r_size = (float(np.corrcoef(score_arr, size_arr)[0, 1])
              if len(score_arr) > 1 and size_arr.std() > 0 else None)
    inds = scored_df["industry"].fillna("未知")
    dum = pd.get_dummies(inds, prefix="", prefix_sep="")
    r_inds = []
    for c in dum.columns:
        dd = dum[c].to_numpy()
        if dd.std() > 0:
            r_inds.append(float(np.corrcoef(score_arr, dd)[0, 1]))
    meta = {
        "pool_n": int(len(df)),
        "scored_n": len(scores),
        "n_need_size_dropped": int(df["ln_size"].isna().sum()),
        "coverage": round(len(scores) / len(df), 4) if len(df) else 0.0,
        "n_factor_used": int(sum(1 for m in factor_meta.values() if m.get("ok"))),
        "factor_meta": factor_meta,
        "orth_r_size": (round(r_size, 5) if r_size is not None else None),
        "orth_mean_abs_r_ind": (round(float(np.mean(np.abs(r_inds))), 5)
                                if r_inds else None),
        "elapsed_s": round(elapsed, 2),
    }
    return {"as_of": dstr, "n_pool": int(len(df)), "n_scored": len(scores),
            "coverage": round(len(scores) / len(df), 4) if len(df) else 0.0,
            "scores": scores, "meta": meta}


def snapshot_day_df(as_of: str) -> pd.DataFrame:
    """as_of 收盘后当日候选池截面 df (规则同 cross_section_scores:
    active sz/sh + 当日有 bar + |change_pct|<=9.5), 含 ln_size/industry 及全部
    原始财务列 (pe_ttm/pb/roe/roe_yy_chg/eps/ocf_ps/np_yoy/rev_yoy/bvps).
    供 gp4 观察候选单日记录/监控; 无交易日返回空表."""
    D = _snap_date(as_of)
    if D is None:
        return pd.DataFrame()
    active = _active_szsh()
    fin = _fin_frame()
    val = _val_frame(D - pd.Timedelta(days=730))
    snap = _Snap(fin, val)
    snap.advance(D)
    bar = _bar_day(D)
    return _assemble_snapshot(snap, bar, active)


# ---------------------------------------------------------------------------
# gp4 监控因子 (QuantGplearn GP 挖掘候选, 见 factor_mine/gp_mine_report.json)
#
# 月频面板表达式 (X 映射 monthly_panel_neutral *_neu 特征列):
#   gp4 = div(sub(abs(X8), add(X6, X3)), add(abs(X4), sqrt(X12)))
#       = (|rev_yoy_neu| - (np_yoy_neu + roe_yy_chg_neu)) / (|roe_neu| + sqrt(bvps_neu))
#
# 日频等价: 每个 as_of 交易日把 5 个原始财务因子 (rev_yoy/np_yoy/roe_yy_chg/roe/bvps)
#   用与融合因子完全相同的截面处理 (Winsorize 1/99 -> OLS残差(ln_size+行业) -> z),
#   再代入公式. 公式域与月频评估一致: bvps_z<0 处 sqrt 无定义/除零/非有限 -> 该股
#   当日无 gp4 分 (观察口径, 明确计入覆盖率与分母说明).
# 纯监控, 不参与融合权重/生产打分 (调用方仅 score_series_hist 验证路径).
# ---------------------------------------------------------------------------
GP4_WATCH = {
    "name": "gp4",
    "expr": "div(sub(abs(X8), add(X6, X3)), add(abs(X4), sqrt(X12)))",
    "expr_zh": "(|rev_yoy_neu| - (np_yoy_neu + roe_yy_chg_neu)) / "
               "(|roe_neu| + sqrt(bvps_neu))",
    "components": ["rev_yoy", "np_yoy", "roe_yy_chg", "roe", "bvps"],
    "status": "watch",
}


def gp4_watch_scores(df: pd.DataFrame) -> tuple[dict, dict]:
    """对单日截面 df (须含 ln_size/industry + GP4_WATCH.components 列) 计算 gp4 分.

    Returns ({裸6位symbol: gp4原值}, meta). 任一分量截面样本不足则整日返回空.
    """
    comps = GP4_WATCH["components"]
    missing = [c for c in comps if c not in df.columns]
    if missing:
        return {}, {"ok": False, "error": f"缺列 {missing}"}
    z_by = {}
    meta_by = {}
    for c in comps:
        zc, mc = _residualize(df, c)
        z_by[c] = zc
        meta_by[c] = mc
        if not mc.get("ok"):
            return {}, {"ok": False, "pool_n": int(len(df)),
                        "factor_missing": c, "factor_meta": meta_by}
    common = set.intersection(*[set(z.keys()) for z in z_by.values()])
    scores: dict[str, float] = {}
    n_drop = {"bvps_neg": 0, "div_nonfinite": 0}
    for s in common:
        zr = z_by["rev_yoy"][s]
        zn = z_by["np_yoy"][s]
        zg = z_by["roe_yy_chg"][s]
        zroe = z_by["roe"][s]
        zb = z_by["bvps"][s]
        num = abs(zr) - (zn + zg)
        if zb < 0.0:
            n_drop["bvps_neg"] += 1
            continue
        den = abs(zroe) + float(np.sqrt(zb))
        if not np.isfinite(den) or abs(den) < 1e-9:
            n_drop["div_nonfinite"] += 1
            continue
        v = num / den
        if not np.isfinite(v):
            n_drop["div_nonfinite"] += 1
            continue
        scores[s] = float(v)
    meta = {
        "ok": True,
        "pool_n": int(len(df)),
        "defined_n": len(scores),
        "component_n": {c: int(m.get("n", 0)) for c, m in meta_by.items()},
        "n_drop": n_drop,
        "note": "bvps_z<0 -> sqrt 无定义, 与月频评估同口径 (观察候选 gp4 的固有域限制)",
    }
    return scores, meta


# ---------------------------------------------------------------------------
# fusion_or_fml 统一入口 (替换点共用)
# ---------------------------------------------------------------------------
def fusion_or_fml(items: list[dict], as_of: str):
    """集中打分入口: 先 fusion, 失败/低覆盖回退旧 f_ml, 再空则 equal.

    Returns (scores_dict: {canon: float}, used: 'fusion'|'fml_fallback'|'equal')
    canon 为 items 中的原始 canon (600519.SH 等). 绝不抛异常.
    """
    from datetime import date as _date
    raw_asof = str(as_of or "").strip()
    as_of = raw_asof[:10] if raw_asof else _date.today().strftime("%Y-%m-%d")
    canons = [str(t.get("canon") or "").strip() for t in items]
    canons = [c for c in canons if c]
    req_n = len(canons)
    st = {"as_of": as_of, "req": req_n}

    def _finish(used, sc):
        st["used"] = used
        st["n"] = len(sc)
        st["coverage"] = round(len(sc) / req_n, 4) if req_n else 0.0
        _LOG.info("[fusion_or_fml] used=%s as_of=%s req=%d got=%d cov=%.2f %s",
                  used, st["as_of"], req_n, len(sc), st["coverage"], st)
        return sc, used

    if req_n == 0:
        return _finish("equal", {})

    if _is_on():
        try:
            r = cross_section_scores(as_of, symbols=canons)
            sc_all = r.get("scores") or {}
            codes = [_canon_code(c) for c in canons]
            sc = {c: sc_all[code] for c, code in zip(canons, codes)
                  if code in sc_all}
            cov = len(sc) / req_n if req_n else 0.0
            if (r.get("n_scored") or 0) >= MIN_POOL_N and cov >= MIN_COVERAGE:
                _LOG.info("[fusion_or_fml] fusion OK as_of=%s pool=%d scored=%d cov=%.2f",
                          r.get("as_of"), r.get("n_pool", 0), len(sc_all), cov)
                return _finish("fusion", sc)
            _LOG.warning("[fusion_or_fml] fusion 覆盖率不足/样本少 -> 回退 f_ml "
                         "(as_of=%s n_sc=%d req=%d cov=%.2f)",
                         as_of, len(sc), req_n, cov)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("[fusion_or_fml] fusion 异常回退 f_ml: %s: %s",
                         type(e).__name__, e)
    else:
        _LOG.info("[fusion_or_fml] 融合被禁用(FORCE_FML/FUSION_SCORE) -> f_ml 路径")

    # 旧路径回退
    try:
        from ml_fusion_bridge import compute_fml
        res = compute_fml(canons, as_of)
        raw = res.get("scores") or {}     # 旧桥返回 {canon: score}
        sc_f = {c: float(raw[c]) for c in canons
                if c in raw and isinstance(raw.get(c), (int, float))}
        if sc_f:
            return _finish("fml_fallback", sc_f)
        if raw:
            _LOG.warning("[fusion_or_fml] f_ml 返回 %d 条但均不匹配请求 canon "
                         "(as_of=%s)", len(raw), as_of)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("[fusion_or_fml] f_ml 回退失败: %s: %s", type(e).__name__, e)
    return _finish("equal", {})


# ===================================================================
# 4.1 加权方案 (集成)
# ===================================================================

def compute_fusion_weights(
    factor_list: list[str] | None = None,
    scheme: str = "icir",
    window: int = 60,
    hold: str = "hold_5",
) -> dict:
    """ICIR加权方案: 从 ic_history.csv 读取 IC 序列, 滚动计算权重.

    当无 IC 历史时自动降级为等权兜底.

    Args:
        factor_list: 因子名列表, 默认 FACTOR_WEIGHTS.keys()
        scheme: "icir" | "ic_mean" | "equal"
        window: 滚动窗口 (交易日)
        hold: 持有期列名, 如 "hold_5"

    Returns:
        {factor: {weight, ic_mean, icir, ...}, meta: {...}}
    """
    if factor_list is None:
        factor_list = list(FACTOR_WEIGHTS.keys())
    try:
        from factor_mine.weighting_scheme import factor_weights, load_ic_history
        ic = load_ic_history()
        result = factor_weights(factor_list, scheme=scheme, ic_history=ic,
                                window=window, hold=hold)
        return result
    except Exception as e:
        # 等权兜底
        n = len(factor_list)
        ew = round(1.0 / n, 6) if n else 0.0
        weights = {f: {"weight": ew, "scheme": "equal_fallback",
                       "error": str(e)[:80]} for f in factor_list}
        return {"weights": weights, "meta": {"scheme": "equal_fallback",
                "note": f"加权计算异常, 等权兜底: {e}"}}


# ===================================================================
# 4.2 行业/市值中性化 (公开 API)
# ===================================================================

def neutralize_factor(
    factor_values: pd.Series,
    industry: pd.Series | None = None,
    log_market_cap: pd.Series | None = None,
    winsor_lo: float = 0.01,
    winsor_hi: float = 0.99,
) -> pd.Series:
    """截面中性化: OLS 回归残差.

    因子值 = industry_dummies + log_market_cap + residual
    残差即为中性化因子, 再标准化为 z-score.

    Args:
        factor_values: 因子原始值 (index=stock symbol)
        industry: 行业分类 (index=stock symbol, value=行业名)
        log_market_cap: 对数市值 (index=stock symbol)
        winsor_lo: 下限 Winsorize 分位
        winsor_hi: 上限 Winsorize 分位

    Returns:
        pd.Series: 中性化+标准化后的因子值 (index=stock symbol)
    """
    if factor_values.empty:
        return factor_values

    df = pd.DataFrame({"factor": factor_values})
    if industry is not None:
        df["industry"] = industry
    else:
        df["industry"] = "unknown"
    if log_market_cap is not None:
        df["ln_size"] = log_market_cap
    else:
        df["ln_size"] = 0.0

    ok = df["factor"].notna() & df["ln_size"].notna() & np.isfinite(df["factor"].to_numpy())
    n = int(ok.sum())
    if n < 30:
        return pd.Series(index=factor_values.index, dtype=float)

    sub = df.loc[ok].copy()
    y = sub["factor"].clip(sub["factor"].quantile(winsor_lo),
                           sub["factor"].quantile(winsor_hi)).to_numpy(dtype=float)
    size = sub["ln_size"].to_numpy(dtype=float)
    dummies = pd.get_dummies(sub["industry"].fillna("unknown"), prefix="", prefix_sep="")
    X = np.hstack([size.reshape(-1, 1), dummies.to_numpy(dtype=float)])
    try:
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
    except Exception:
        return pd.Series(index=factor_values.index, dtype=float)

    sd = float(resid.std())
    if not np.isfinite(sd) or sd <= 1e-12:
        return pd.Series(index=factor_values.index, dtype=float)

    z = (resid - resid.mean()) / sd
    return pd.Series(z, index=sub.index)


# ===================================================================
# 4.3 加权融合 (生产入口)
# ===================================================================

def weighted_fusion(
    scores_by_factor: dict[str, dict[str, float]],
    weight_config: dict | None = None,
    scheme: str = "icir",
) -> dict:
    """多因子加权融合, 支持三种方案.

    Args:
        scores_by_factor: {factor: {symbol: z_score}}
        weight_config: 若为 None 则自动计算
        scheme: 自动计算时使用的方案

    Returns:
        {symbol: fused_z_score}
    """
    if weight_config is None:
        result = compute_fusion_weights(list(scores_by_factor.keys()), scheme=scheme)
        weight_config = result.get("weights", {})

    from factor_mine.weighting_scheme import fuse_scores
    return fuse_scores(scores_by_factor, weight_config)
# ---------------------------------------------------------------------------
def score_series_hist(end: Optional[str] = None, days: int = 90) -> dict:
    """最近 days 个交易日逐日(收盘后视角)出分, 附未来 1/5 日真实收益.

    Returns {"as_of_start", "as_of_end", "n_days", "samples": [...],
             "history": [{date, pool_n, scored_n, coverage,
                          scores:{code:z}, fwd1:{code:pct}, fwd5:{code:pct},
                          ln_size:{code}, industry:{code}}]}
    只读, 不落任何生产文件.
    """
    cal = _calendar()
    if not cal:
        return {"error": "no calendar"}
    end_ts = pd.Timestamp(str(end)[:10]).normalize() if end else cal[-1]
    if end_ts > cal[-1]:
        end_ts = cal[-1]
    if end_ts < cal[0]:
        return {"error": f"end {end_ts} before first bar"}
    hist = [d for d in cal if d <= end_ts][-int(days):]
    if not hist:
        return {"error": "empty history window"}
    # bar 装载窗口向后延伸到 end_ts 之后最多 6 个交易日 (有数据则加载),
    # 保证每个评分日的 fwd1/fwd5 可在其自身 bar 序列内取到(尾部无未来数据则 NaN).
    hi_idx = min(cal.index(end_ts) + 6, len(cal) - 1)
    hi_bar = cal[hi_idx]
    bars = _sql(f"SELECT CAST(ts AS DATE) d, symbol, close, change_pct "
                f"FROM daily_bars WHERE CAST(ts AS DATE) >= DATE '{hist[0].strftime('%Y-%m-%d')}' "
                f"AND CAST(ts AS DATE) <= DATE '{hi_bar.strftime('%Y-%m-%d')}'")
    bars["d"] = pd.to_datetime(bars["d"])
    bars["symbol"] = bars["symbol"].astype(str)
    bars = bars[bars["symbol"].str.fullmatch(r"\d{6}")].copy()
    bars["symbol"] = bars["symbol"].str.zfill(6)
    bars = bars.drop_duplicates(["symbol", "d"], keep="last")
    bars = bars.sort_values(["symbol", "d"]).reset_index(drop=True)
    r1 = (1.0 + bars["change_pct"] / 100.0).to_numpy(dtype=float)
    bars = bars.assign(r1=r1)
    g = bars.groupby("symbol", sort=False)["r1"]
    cump = g.cumprod()
    fwd1 = g.cumprod().groupby(bars["symbol"], sort=False).shift(-1) / cump - 1.0
    fwd5 = g.cumprod().groupby(bars["symbol"], sort=False).shift(-5) / cump - 1.0
    bars["fwd1"] = np.where(np.isfinite(fwd1), fwd1, np.nan)
    bars["fwd5"] = np.where(np.isfinite(fwd5), fwd5, np.nan)

    active = _active_szsh()
    fin = _fin_frame()
    val = _val_frame(hist[0] - pd.Timedelta(days=730))
    snap = _Snap(fin, val)

    out, t0 = [], time.time()
    for D in hist:
        snap.advance(D)
        day_bars = bars[bars["d"] == D]
        df = _assemble_snapshot(snap, day_bars, active)
        dstr = D.strftime("%Y-%m-%d")
        if len(df) < MIN_POOL_N:
            out.append({"date": dstr, "n_pool": int(len(df)), "n_scored": 0,
                        "scores": {}, "fwd1": {}, "fwd5": {},
                        "ln_size": {}, "industry": {}, "meta": {},
                        "gp4_scores": {}, "gp4_meta": {}})
            continue
        gp4_sc, gp4_md = {}, {}
        try:
            gp4_sc, gp4_md = gp4_watch_scores(df)
        except Exception as e:  # noqa: BLE001
            gp4_md = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        res = _score_df(df, dstr, elapsed=time.time() - t0)
        sc = res.get("scores") or {}
        hit = bars[(bars["d"] == D) & (bars["symbol"].isin(sc))]
        hm = hit.set_index("symbol")
        rows = {"date": dstr, "n_pool": res.get("n_pool", 0),
                "n_scored": len(sc),
                "coverage": res.get("coverage", 0.0),
                "scores": sc,
                "fwd1": {s: float(v) for s, v in
                         hm["fwd1"].dropna().items()},
                "fwd5": {s: float(v) for s, v in
                         hm["fwd5"].dropna().items()},
                "ln_size": {r.symbol: (float(r.ln_size)
                                       if pd.notna(r.ln_size) else None)
                            for r in df[df["symbol"].isin(sc)].itertuples()},
                "industry": {r.symbol: r.industry
                             for r in df[df["symbol"].isin(sc)].itertuples()},
                "meta": res.get("meta", {}),
                "gp4_scores": gp4_sc, "gp4_meta": gp4_md}
        out.append(rows)
    return {"as_of_start": hist[0].strftime("%Y-%m-%d"),
            "as_of_end": hist[-1].strftime("%Y-%m-%d"),
            "n_days": len(hist), "samples": out}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else None
    r = cross_section_scores(d or "2026-09-04")
    print(json.dumps({k: v for k, v in r.items() if k != "scores"},
                     ensure_ascii=False, indent=2, default=str))
    print("scored", len(r.get("scores", {})),
          "sample", list(r.get("scores", {}).items())[:5])
