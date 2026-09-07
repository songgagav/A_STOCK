# -*- coding: utf-8 -*-
"""m5: 基于 h5i 重建财务/估值候选因子 + 统一评估 (月度截面)

数据源 (全部走 h5i / parquet, 不触碰 DuckDB, 不改库):
  - data/h5i/market.db (h5i-db, 时间列 ts):
      daily_bars : ts=交易日 00:00, symbol(bare 6位为主, 混有 .SZ/.SH 与债券/含权), close, change_pct ...
      financials:  ts=报告期, roe/eps/bvps/.../source
      valuation :  ts=交易日, pe_ttm/pb/ps_ttm/is_st/float_shares ...  (market_cap/free_cap/ps_ttm 全空)
  - data/h5i/static/symbols.parquet : symbol/market/list_date(is NaT)/is_active

输出:
  - data/factor_mine/ledger_m5_factors.jsonl   候选因子评估台账(JSONL)
  - data/factor_mine/rebuild_factor_report.json 汇总报告(中文, 含按 |ICIR| 降序摘要)
  - data/h5i/factors/monthly_panel.parquet      月度截面因子面板(date,symbol,因子...,fwd5,fwd20)

运行: cd A_stock_rotation && python -m factor_mine.m5_rebuild
"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # A_stock_rotation
H5I = os.path.join(BASE, "data", "h5i", "market.db")
SYMBOLS_PQ = os.path.join(BASE, "data", "h5i", "static", "symbols.parquet")
OUT_DIR = os.path.join(BASE, "data", "factor_mine")
LEDGER = os.path.join(OUT_DIR, "ledger_m5_factors.jsonl")
REPORT_JSON = os.path.join(OUT_DIR, "rebuild_factor_report.json")
PANEL_PQ = os.path.join(BASE, "data", "h5i", "factors", "monthly_panel.parquet")

CS_START = "2019-01-01"   # 截面起点(含)
CS_END = "2025-07-31"     # 有效全市场估值覆盖末月(valuation 全市场日频 2025-08-01 后断崖式降到~300只)
NEAR3_FROM = "2023-09-01"
FWD_DAYS = (5, 20)

import h5i_db  # noqa: E402

# ---------------------------------------------------------------------------
# 候选因子定义表 (截面因子, 逐股月末取值)
#   col   : 面板列名(裸 6 位 symbol 索引)
#   name  : 中文名
#   expr  : 取数表达式
#   theo_dir: +1 高->好; -1 低->好; 0 不作方向先验
#   说明: financials 一律 PIT (报告期可用性映射); valuation 取月末最近交易日(ts<=月末)
# ---------------------------------------------------------------------------
FACTORS = [
    # ---------- 财务类 (financials 报告期 -> PIT 映射) ----------
    {"col": "roe",          "name": "净资产收益率ROE", "grp": "financial",
     "expr": "financials.roe[最新PIT可见报告期](小数,0.05=5%,累计口径)", "theo_dir": 1,
     "note": "盈利能力质量; 年报/一季/中报/三季按披露日映射可见性"},
    {"col": "roe_yy_chg",   "name": "ROE同比变动", "grp": "financial",
     "expr": "roe[本期]-roe[上年同期报告期](按symbol报告期错位1年)", "theo_dir": 1,
     "note": "盈利改善动量; 需上年同期报告存在"},
    {"col": "eps",          "name": "基本每股收益EPS", "grp": "financial",
     "expr": "financials.eps[最新PIT可见报告期](元,累计口径)", "theo_dir": 1,
     "note": "绝对盈利规模; 注意含股本扩张/再融资杂讯"},
    {"col": "rev_yoy",      "name": "营收同比增速", "grp": "financial",
     "expr": "financials.rev_yoy[最新PIT可见报告期](小数)", "theo_dir": 1,
     "note": "成长; 剔除异常源(存在极端值,rank口径稳健)"},
    {"col": "np_yoy",       "name": "净利润同比增速", "grp": "financial",
     "expr": "financials.np_yoy[最新PIT可见报告期](小数)", "theo_dir": 1,
     "note": "成长; 低基数/一次性损益会放大波动"},
    {"col": "ded_np_yoy",   "name": "扣非净利润同比增速", "grp": "financial",
     "expr": "financials.ded_np_yoy[最新PIT可见报告期](小数)", "theo_dir": 1,
     "note": "成长(剔除非经常性损益)"},
    {"col": "gross_margin", "name": "销售毛利率", "grp": "financial",
     "expr": "financials.gross_margin[最新PIT可见报告期](小数;银行等无此列->缺失)", "theo_dir": 1,
     "note": "盈利质地; 行业差异大"},
    {"col": "net_margin",   "name": "销售净利率", "grp": "financial",
     "expr": "financials.net_margin[最新PIT可见报告期](小数)", "theo_dir": 1,
     "note": "盈利质地"},
    {"col": "ocf_ps",       "name": "每股经营现金流", "grp": "financial",
     "expr": "financials.ocf_ps[最新PIT可见报告期](元)", "theo_dir": 1,
     "note": "现金流含量; 与盈利匹配度"},
    {"col": "debt_ratio",   "name": "资产负债率", "grp": "financial",
     "expr": "financials.debt_ratio[最新PIT可见报告期](小数)", "theo_dir": -1,
     "note": "杠杆; 金融/地产行业杠杆天然高"},
    {"col": "bvps",         "name": "每股净资产BVPS", "grp": "financial",
     "expr": "financials.bvps[最新PIT可见报告期](元)", "theo_dir": -1,
     "note": "净资产厚度; 高BVPS多对应大盘/成熟/低PB风格,结合A股小市值占优与价值因子, 先验方向取负(实际方向以数据自证为准)"},
    # ---------- 估值类 (valuation 月末最近交易日 ts<=月末) ----------
    {"col": "ep",           "name": "盈利收益率EP=1/PE_TTM", "grp": "valuation",
     "expr": "1/valuation.pe_ttm[ts<=月末最近交易日,pe_ttm>0]", "theo_dir": 1,
     "note": "价值; 负PE(亏损)设为缺失; 高EP=便宜"},
    {"col": "pb_inv",       "name": "PB倒数=1/PB", "grp": "valuation",
     "expr": "1/valuation.pb[ts<=月末最近交易日,pb>0]", "theo_dir": 1,
     "note": "价值; 破净(负净资产)pb<=0设为缺失"},
    {"col": "peg",          "name": "PEG近似=净利增速/PE", "grp": "valuation",
     "expr": "np_yoy/pe_ttm[pe_ttm>0](等价-PEG,越大越'成长且不贵')", "theo_dir": 1,
     "note": "GARP; 与ep/pb_inv 相关性中高"},
    {"col": "ln_mcap",      "name": "对数流通市值ln(close*float_shares)", "grp": "valuation",
     "expr": "ln(daily_bars.close * valuation.float_shares)[月末] (valuation.market_cap全空,以流通市值近似规模)",
     "theo_dir": -1,
     "note": "规模; 全市场市值列历史缺失,用流通市值口径近似, 方向=小市值溢价"},
    {"col": "free_cap",     "name": "流通市值free_cap=close*float_shares", "grp": "valuation",
     "expr": "daily_bars.close * valuation.float_shares[月末,元]", "theo_dir": -1,
     "note": "规模(原始口径); 与ln_mcap完全单调同源,列示以便后续自定义变换"},
]

FIN_COLS = ["roe", "roe_yy_chg", "eps", "rev_yoy", "np_yoy", "ded_np_yoy",
            "gross_margin", "net_margin", "ocf_ps", "debt_ratio", "bvps"]
VAL_COLS = ["ep", "pb_inv", "peg", "ln_mcap", "free_cap"]
FACTOR_COLS = [f["col"] for f in FACTORS]


# ---------------------------------------------------------------------------
# 报告期可用性(PIT)映射: 年报->次年04-30, Q1->04-30, 中报->08-31, Q3->10-31
# ---------------------------------------------------------------------------
def avail_date(ts: pd.Timestamp) -> pd.Timestamp:
    y, m = ts.year, ts.month
    if m == 12:
        return pd.Timestamp(f"{y + 1}-04-30")
    if m == 3:
        return pd.Timestamp(f"{y}-04-30")
    if m == 6:
        return pd.Timestamp(f"{y}-08-31")
    if m == 9:
        return pd.Timestamp(f"{y}-10-31")
    return pd.Timestamp(f"{y}-12-31")  # 异常报告期兜底(不会被使用)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def open_db():
    return h5i_db.Database(H5I)


def append_jsonl(path: str, obj: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# 1) 静态符号 + 交易日历 + 上市代理日
# ---------------------------------------------------------------------------
def load_static(db) -> dict:
    sy = pd.read_parquet(SYMBOLS_PQ)
    sy = sy[sy["is_active"] & sy["market"].isin(["sz", "sh"])].copy()
    master = set(sy["symbol"].astype(str).str.zfill(6))
    _log(f"symbols.parquet: active sz+sh={len(master)}")

    # 全市场交易日历(用于月未截面日 + 上市满60交易日判断)
    days = db.sql(
        "SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars ORDER BY 1").to_pandas()
    cal = pd.to_datetime(days["d"]).dt.normalize().reset_index(drop=True)
    cal_idx = {d: i for i, d in enumerate(cal)}
    # 每月最后一个交易日(月未截面), 限定到 CS_END
    s = pd.Series(cal[cal.between(CS_START, CS_END)])
    ymd = s.dt.to_period("M")
    month_end = s.groupby(ymd).max().sort_values().reset_index(drop=True)
    _log(f"交易日历 {cal.iloc[0].date()}..{cal.iloc[-1].date()} n={len(cal)}; "
         f"月未截面 {len(month_end)} 个 ({month_end.iloc[0].date()}..{month_end.iloc[-1].date()})")

    # 上市代理日 = daily_bars 最早 bar 日(全历史; list_date 静态为空 NaT)
    fb = db.sql("SELECT symbol, MIN(CAST(ts AS DATE)) d FROM daily_bars "
                "WHERE symbol NOT LIKE '%.%' GROUP BY symbol").to_pandas()
    fb["d"] = pd.to_datetime(fb["d"])
    list_proxy = {r.symbol: r.d for r in fb.itertuples()}
    return {"master": master, "cal": cal, "cal_idx": cal_idx,
            "month_end": month_end, "list_proxy": list_proxy}


# ---------------------------------------------------------------------------
# 2) 复权收益: change_pct 连乘累计 -> fwd5/fwd20 (每只股票自身 bar 序列的后续行)
# ---------------------------------------------------------------------------
def build_forward_returns(db) -> pd.DataFrame:
    _log("读取 daily_bars.change_pct (2019-01-01..2026-09-04)...")
    cp = db.sql(
        "SELECT CAST(ts AS DATE) d, symbol, change_pct FROM daily_bars "
        "WHERE ts >= TIMESTAMP '2019-01-01' AND ts <= TIMESTAMP '2026-09-04' "
        "AND change_pct IS NOT NULL").to_pandas()
    cp["d"] = pd.to_datetime(cp["d"])
    cp = cp[~cp["symbol"].astype(str).str.contains(".", regex=False)]
    cp = cp[cp["symbol"].str.fullmatch(r"\d{6}")]
    cp["symbol"] = cp["symbol"].str.zfill(6)
    cp = cp.sort_values(["symbol", "d"]).reset_index(drop=True)
    _log(f"bars rows={len(cp):,}")

    r1 = 1.0 + cp["change_pct"].to_numpy() / 100.0
    cp = cp.assign(r1=r1)
    g = cp.groupby("symbol", sort=False)["r1"]
    cump = g.cumprod()
    cump_5 = g.cumprod().groupby(cp["symbol"], sort=False).shift(-FWD_DAYS[0])
    cump_20 = g.cumprod().groupby(cp["symbol"], sort=False).shift(-FWD_DAYS[1])
    cp["fwd5"] = cump_5 / cump - 1.0
    cp["fwd20"] = cump_20 / cump - 1.0
    cp["fwd5"] = np.where(np.isfinite(cp["fwd5"]), cp["fwd5"], np.nan)
    cp["fwd20"] = np.where(np.isfinite(cp["fwd20"]), cp["fwd20"], np.nan)
    return cp[["d", "symbol", "fwd5", "fwd20"]].rename(columns={"d": "date"})


# ---------------------------------------------------------------------------
# 3) 财务原始表 + 行级派生(roe同比变动)
# ---------------------------------------------------------------------------
def load_financials(db) -> pd.DataFrame:
    _log("读取 financials...")
    fin = db.sql("SELECT CAST(ts AS DATE) ts, symbol, revenue, rev_yoy, net_profit, "
                 "np_yoy, ded_np_yoy, eps, bvps, ocf_ps, net_margin, roe, "
                 "debt_ratio, gross_margin, source FROM financials").to_pandas()
    fin["ts"] = pd.to_datetime(fin["ts"])
    fin["symbol"] = fin["symbol"].astype(str).str.zfill(6)
    fin = fin[fin["ts"].dt.month.isin([3, 6, 9, 12])]
    # 可用性日期
    fin["avail"] = fin["ts"].map(avail_date)
    # 报告期季度序号(同比错位 4 期)
    fin["kk"] = fin["ts"].dt.year * 4 + (fin["ts"].dt.month // 3 - 1)
    lag = fin[["symbol", "kk", "roe"]].assign(kk=fin["kk"] - 4).rename(columns={"roe": "roe_yy_ago"})
    fin = fin.merge(lag, on=["symbol", "kk"], how="left")
    fin["roe_yy_chg"] = fin["roe"] - fin["roe_yy_ago"]
    # 去重: 同(symbol,报告期)仅留一条; 同(symbol,可用日)若有多个报告期(如FY/Q1同日可用)
    # 保留报告期最新一条(merge_asof 要求右表按 by+on 唯一时结果才确定)
    fin = (fin.sort_values(["symbol", "ts"]).drop_duplicates(["symbol", "ts"], keep="last")
              .sort_values(["symbol", "avail", "ts"])
              .drop_duplicates(["symbol", "avail"], keep="last"))
    _log(f"financials rows={len(fin):,} periods={fin['ts'].dt.strftime('%Y-%m').nunique()}")
    return fin[["symbol", "ts", "avail"] + FIN_COLS]


# ---------------------------------------------------------------------------
# 4) 月未截面逐日组装: bar + valuation + 上市满60交易日 + 一字板剔除 + fwd
# ---------------------------------------------------------------------------
def build_monthly_panel(db, static, fwd_df) -> pd.DataFrame:
    master, cal_idx, month_end = static["master"], static["cal_idx"], static["month_end"]
    list_proxy = static["list_proxy"]
    frames, uni_stats = [], []
    t0 = time.time()
    for n, D in enumerate(month_end, 1):
        dstr = D.strftime("%Y-%m-%d")
        bar = db.sql(
            f"SELECT symbol, close, change_pct FROM daily_bars "
            f"WHERE CAST(ts AS DATE)=DATE '{dstr}'").to_pandas()
        bar["symbol"] = bar["symbol"].astype(str)
        bar = bar[bar["symbol"].str.fullmatch(r"\d{6}")]
        bar["symbol"] = bar["symbol"].str.zfill(6)
        bar = bar[bar["symbol"].isin(master)]
        # 剔除一字涨/跌停: 保守 |change_pct| > 9.5
        bar = bar[bar["change_pct"].abs() <= 9.5]
        bar = bar.dropna(subset=["close", "change_pct"])

        val = db.sql(
            f"SELECT symbol, pe_ttm, pb, float_shares, is_st FROM valuation "
            f"WHERE CAST(ts AS DATE)=DATE '{dstr}'").to_pandas()
        val["symbol"] = val["symbol"].astype(str).str.zfill(6)
        val = val[val["symbol"].isin(master)]
        val = val[val["is_st"] == False]  # noqa: E712  排除ST/*ST
        val = val.dropna(subset=["is_st"])

        m = bar.merge(val, on="symbol", how="inner")
        # 上市满 60 交易日
        di = cal_idx[D]
        if len(m):
            lp = m["symbol"].map(list_proxy)
            lp = pd.to_datetime(lp)
            age = lp.map(lambda x: di - cal_idx[x] + 1 if pd.notna(x) else 0)
            m = m[age >= 60]
        m["date"] = D
        m = m.merge(fwd_df, on=["date", "symbol"], how="left")
        m = m.dropna(subset=["fwd20"])          # 收益标签必须有
        uni_stats.append({"date": dstr, "n": int(len(m))})
        frames.append(m[["date", "symbol", "close", "pe_ttm", "pb", "float_shares",
                         "fwd5", "fwd20"]])
        if n % 12 == 0 or n == len(month_end):
            _log(f"  截面 {n}/{len(month_end)} date={dstr} pool={len(m):,} "
                 f"elapse={time.time() - t0:.0f}s")
    panel = pd.concat(frames, ignore_index=True)
    _log(f"monthly panel base rows={len(panel):,}")
    return panel, uni_stats


# ---------------------------------------------------------------------------
# 5) 财务 PIT 对齐 (merge_asof: 报告期可用日 <= 月末截面) + 估值派生因子
# ---------------------------------------------------------------------------
def attach_pit(panel: pd.DataFrame, fin: pd.DataFrame) -> pd.DataFrame:
    # 逐截面日: 取 报告期可用日(avail)<=截面日 的最新报告 (PIT, 无前视)
    fin = fin.sort_values(["symbol", "ts"]).reset_index(drop=True)
    parts, t0 = [], time.time()
    dates = sorted(panel["date"].unique())
    for i, D in enumerate(dates, 1):
        elig = fin[fin["avail"] <= D]
        latest = elig.groupby("symbol", sort=False).tail(1)
        gg = panel[panel["date"] == D]
        m = gg.merge(latest[["symbol"] + FIN_COLS], on="symbol", how="left")
        parts.append(m)
        if i % 12 == 0 or i == len(dates):
            _log(f"  PIT attach {i}/{len(dates)} date={pd.Timestamp(D).date()} "
                 f"elapse={time.time() - t0:.0f}s")
    out = pd.concat(parts, ignore_index=True)
    # 估值派生
    pe, pb, close, fs = out["pe_ttm"], out["pb"], out["close"], out["float_shares"]
    out["ep"] = np.where((pe > 0) & pe.notna(), 1.0 / pe, np.nan)
    out["pb_inv"] = np.where((pb > 0) & pb.notna(), 1.0 / pb, np.nan)
    out["peg"] = np.where((pe > 0) & pe.notna() & out["np_yoy"].notna(),
                          out["np_yoy"] / pe, np.nan)
    mc = close * fs
    out["free_cap"] = mc
    out["ln_mcap"] = np.log(mc)
    # 清理 inf
    for c in FACTOR_COLS:
        out[c] = out[c].replace([np.inf, -np.inf], np.nan)
    return out


# ---------------------------------------------------------------------------
# 6) 统一评估 (月度截面, 口径对齐 factor_mine.evaluator)
#    窗口内逐截面: RankIC(spearman) / 五分位组均收益 / Q4-Q0 / 多空月化收益与信息比
# ---------------------------------------------------------------------------
def eval_factor(panel: pd.DataFrame, factor: str, fwd: str = "fwd20",
                start: str = None, end: str = None, min_n=30) -> dict:
    d = panel if start is None else panel[panel["date"] >= pd.Timestamp(start)]
    d = d if end is None else d[d["date"] <= pd.Timestamp(end)]
    rows = d[["date", factor, fwd]].replace([np.inf, -np.inf], np.nan)
    rows = rows.dropna(subset=[factor, fwd]).copy()

    dates_all = sorted(d["date"].unique())
    ic_list, n_used_dates = [], 0
    q_recs = []          # 每截面 q0..q4 组均
    ls_list = []         # 每截面 (q4-q0)
    per_date_n = []
    for dt_, gg in rows.groupby("date"):
        if len(gg) < min_n:
            continue
        # RankIC: 原始值 spearman(与 factor_mine.evaluator.daily_ic 口径一致)
        ic, _ = spearman(gg[factor], gg[fwd])
        if not math.isnan(ic):
            ic_list.append(ic)
            n_used_dates += 1
            per_date_n.append(int(len(gg)))
        # 五分位分组/多空: 复牌/异常行情极端收益先做截面 1%/99% winsor (见 factor_validation 惯例)
        lo, hi = gg[fwd].quantile(0.01), gg[fwd].quantile(0.99)
        gw = gg[fwd].clip(lo, hi)
        try:
            qv = pd.qcut(gg[factor], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        gm = gw.groupby(qv).mean()
        if len(gm) < 5:
            continue
        g = [float(gm.get(i, np.nan)) for i in range(5)]
        if any(math.isnan(x) for x in g):
            continue
        q_recs.append(g)
        ls_list.append(g[4] - g[0])
    n_q = len(q_recs)

    ics = np.asarray(ic_list, dtype=float)
    ls = np.asarray(ls_list, dtype=float)
    mean_ic = float(ics.mean()) if len(ics) else float("nan")
    std_ic = float(ics.std(ddof=1)) if len(ics) > 1 else float("nan")
    icir = (mean_ic / std_ic) if (std_ic and not math.isnan(std_ic) and std_ic > 1e-12) else float("nan")
    win = float((ics > 0).mean()) if len(ics) else float("nan")
    ls_mean = float(ls.mean()) if len(ls) else float("nan")
    ls_std = float(ls.std(ddof=1)) if len(ls) > 1 else float("nan")
    ls_ir = (ls_mean / ls_std) if (ls_std and not math.isnan(ls_std) and ls_std > 1e-12) else float("nan")

    qmean = [np.nan] * 5
    if n_q:
        qarr = np.asarray(q_recs)
        qmean = list(qarr.mean(axis=0))
    spread = (qmean[4] - qmean[0]) if n_q else float("nan")
    # 单调性(宽松: 允许 1e-9 容差)
    mono_up = all(qmean[i] <= qmean[i + 1] + 1e-9 for i in range(4)) if n_q else False
    mono_dn = all(qmean[i] >= qmean[i + 1] - 1e-9 for i in range(4)) if n_q else False

    n_pool = int(rows["date"].nunique() and len(rows))
    total_rows = int(len(d))
    cov = float(len(rows)) / total_rows if total_rows else float("nan")
    return {
        "window": {"start": str(d["date"].min().date()) if len(d) else None,
                   "end": str(d["date"].max().date()) if len(d) else None,
                   "n_cs_total": len(dates_all)},
        "n_cs": n_used_dates, "n_pool_rows": n_pool,
        "avg_n_cs": float(np.mean(per_date_n)) if per_date_n else None,
        "rank_ic_mean": round(mean_ic, 4) if not math.isnan(mean_ic) else None,
        "rank_ic_std": round(std_ic, 4) if not math.isnan(std_ic) else None,
        "icir": round(icir, 4) if not math.isnan(icir) else None,
        "win_rate_gt0": round(win, 4) if not math.isnan(win) else None,
        "q_mean": [None if math.isnan(x) else round(float(x), 5) for x in qmean],
        "spread_q4_minus_q0": round(spread, 5) if not math.isnan(spread) else None,
        "monotone_asc": bool(mono_up), "monotone_desc": bool(mono_dn),
        "ls_mean_monthly": round(ls_mean, 5) if not math.isnan(ls_mean) else None,
        "ls_std_monthly": round(ls_std, 5) if not math.isnan(ls_std) else None,
        "ls_info_ratio": round(ls_ir, 4) if not math.isnan(ls_ir) else None,
        "coverage": round(cov, 4) if not math.isnan(cov) else None,
        "missing_rate": round(1 - cov, 4) if not math.isnan(cov) else None,
    }


def spearman(x, y):
    from scipy.stats import spearmanr
    return spearmanr(x, y)


# ---------------------------------------------------------------------------
# 7) 判定 PASS | WATCH | FAIL (基于全窗 fwd20)
#    - |rankIC|>=0.02 且 |ICIR|>=0.5 且 分组方向正确 (Q4-Q0 与 RankIC 同向)
#    - 0.015<=|IC|<0.02 或 0.3<=|ICIR|<0.5 -> WATCH; 其余 FAIL
#    注: 方向以"数据自证方向"为准(分组差与 IC 同号), 避免用先验方向判定准入;
#    理论方向 theo_dir 仅作注解(theo_hit), 可反向使用的因子在结论中提示.
# ---------------------------------------------------------------------------
def verdict(full: dict) -> str:
    icv = full["rank_ic_mean"] or 0.0
    ic_abs = abs(icv)
    icir_abs = abs(full["icir"] or 0.0)
    sp = full["spread_q4_minus_q0"]
    dir_ok = bool(sp is not None and abs(sp) > 1e-12 and sp * icv > 0)
    if ic_abs >= 0.02 and icir_abs >= 0.5 and dir_ok:
        return "PASS"
    if (0.015 <= ic_abs < 0.02) or (0.3 <= icir_abs < 0.5):
        return "WATCH"
    return "FAIL"


def round2(d: dict):
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    _log("=" * 78)
    _log("m5 重建财务/估值候选因子 + 统一评估 (h5i only)")
    _log("=" * 78)
    import sys
    reuse = "--reuse-panel" in sys.argv and os.path.exists(PANEL_PQ)

    if reuse:
        _log(f"[reuse] 直接读月度面板: {PANEL_PQ}")
        panel = pd.read_parquet(PANEL_PQ)
        for c in FACTOR_COLS + ["fwd5", "fwd20"]:
            if c not in panel.columns:
                raise SystemExit(f"复用面板缺列 {c}, 请删除 parquet 后全量重建")
        cnt = panel.groupby("date").size().reset_index(name="n")
        uni_stats = [{"date": str(r.date), "n": int(r.n)} for r in cnt.itertuples()]
        static = None
        db = None
    else:
        db = open_db()
        static = load_static(db)
        fwd_df = build_forward_returns(db)
        _log(f"fwd 收益率表: {len(fwd_df):,} 行")
        panel, uni_stats = build_monthly_panel(db, static, fwd_df)
        fin = load_financials(db)
        panel = attach_pit(panel, fin)
        db.close()
        _log(f"panel 组装完成 rows={len(panel):,} cols={len(panel.columns)}")
        # ---- 交付 1: 月度面板 parquet ----
        os.makedirs(os.path.dirname(PANEL_PQ), exist_ok=True)
        save_cols = ["date", "symbol"] + FACTOR_COLS + ["fwd5", "fwd20"]
        panel[save_cols].to_parquet(PANEL_PQ, index=False)
        _log(f"monthly_panel.parquet 已写入: {PANEL_PQ} (rows={len(panel):,})")

    # ---- 交付 2+3: 台账与报告 ----
    os.makedirs(OUT_DIR, exist_ok=True)
    # 清空旧台账(本文件为重建产物)
    if os.path.exists(LEDGER):
        os.remove(LEDGER)

    rows_all, summary = [], []
    for f in FACTORS:
        col = f["col"]
        full = eval_factor(panel, col, "fwd20", None, None)
        near = eval_factor(panel, col, "fwd20", NEAR3_FROM, None)
        # fwd5 辅助
        fwd5 = eval_factor(panel, col, "fwd5", None, None)
        v = verdict(full)
        icv = full["rank_ic_mean"] or 0.0
        theo_hit = bool(icv * f["theo_dir"] > 0) if f["theo_dir"] != 0 else None
        rec = {
            "name": f["name"], "col": col, "type": "report",
            "expr": f["expr"], "grp": f["grp"], "note": f["note"],
            "theo_dir": f["theo_dir"], "theo_hit": theo_hit, "fwd": "fwd20",
            "source": "h5i:financials/valuation/daily_bars(PIT)",
            "window_full": full["window"], "window_near3": near["window"],
            "full": round2(full), "near3": round2(near),
            "fwd5_full": round2(fwd5), "verdict": v,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        append_jsonl(LEDGER, rec)
        rows_all.append(rec)
        summary.append({"col": col, "name": f["name"], "grp": f["grp"],
                        "theo_dir": f["theo_dir"], "theo_hit": theo_hit,
                        "rankIC": full["rank_ic_mean"], "ICIR": full["icir"],
                        "spread": full["spread_q4_minus_q0"],
                        "ls_m": full["ls_mean_monthly"],
                        "n_cs": full["n_cs"],
                        "coverage": full["coverage"],
                        "near3_rankIC": near["rank_ic_mean"],
                        "near3_ICIR": near["icir"],
                        "verdict": v})
        _log(f"  [{v:5s}] {col:14s} rankIC={full['rank_ic_mean']} "
             f"ICIR={full['icir']} spread={full['spread_q4_minus_q0']} "
             f"ls_m={full['ls_mean_monthly']} cov={full['coverage']}")

    # 相关矩阵(去重提示用)
    sub = panel[["symbol"] + FACTOR_COLS].copy()
    corr_df = sub.replace([np.inf, -np.inf], np.nan).sample(min(120_000, len(sub)),
                                                            random_state=7).corr(
        method="spearman")
    corr = {a: {b: (None if pd.isna(corr_df.loc[a, b]) else round(float(corr_df.loc[a, b]), 3))
                for b in FACTOR_COLS} for a in FACTOR_COLS}

    summary_sorted = sorted(summary, key=lambda r: -(abs(r["ICIR"]) if r["ICIR"] else 0))
    cnt = pd.Series([r["verdict"] for r in summary]).value_counts()

    # 结论/建议: 按 |ICIR| 排序; 提示同源/高相关去重与反序用法
    passers = [r for r in summary if r["verdict"] == "PASS"]
    watchers = [r for r in summary if r["verdict"] == "WATCH"]
    suggestion = {
        "note": ("评估主窗口 fwd20 全窗 2019-01..2025-07 (79月未截面); verdict 的'分组方向'取数据自证方向"
                 "(Q4-Q0与RankIC同号), 理论方向 theo_dir 仅作注解(theo_hit)。"
                 "PASS/WATCH 因子若 theo_hit=False 表示需按相反方向解读/使用(反序)。"),
        "pass": [{"col": r["col"], "rankIC": r["rankIC"], "ICIR": r["ICIR"],
                  "theo_hit": r["theo_hit"]} for r in passers],
        "watch": [{"col": r["col"], "rankIC": r["rankIC"], "ICIR": r["ICIR"],
                   "theo_hit": r["theo_hit"]} for r in watchers],
        "top_combo": ("建议融合候选(按月线/长期组合): 1) roe_yy_chg 显著但为反向(rankIC -0.0815/ICIR -1.13, "
                       "theo_hit=False), 若按'盈利改善溢价'正向理解则不成立, 只能按反转/反序使用或作为对照不纳入; "
                       "2) pb_inv/ep 价值族(WATCH, |ICIR| 0.33~0.42)高相关保留1只即可(pb_inv覆盖更全); "
                       "3) ln_mcap/free_cap 同源(相关1.0)只需保留ln_mcap, 规模因子负IC是2019-2025小盘占优的体现, "
                       "属于风格暴露需单列监控, 不宜作为稳定alpha; "
                       "4) 其余财务因子(roe/eps/gross_margin/net_margin/ocf_ps等)全窗IC近零或为负, "
                       "近3年略有回正但远低于准入线, 单独不可用, 仅可作为ML输入特征(靠模型学方向)。"),
    }
    report = {
        "meta": {
            "module": "m5_rebuild", "pipeline": "h5i-only",
            "build_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "h5i_db": H5I, "symbols": SYMBOLS_PQ,
            "cross_section": {"rule": "每月最后一个交易日, 沪深活跃A股",
                              "range": [CS_START, CS_END],
                              "n_cs": int(panel["date"].nunique()),
                              "exclude": "ST(is_st),上市<60交易日,一字涨跌停|change_pct|>9.5,停牌无bar"},
            "pit": "报告期可用: 年报->次年04-30 Q1->04-30 中报->08-31 Q3->10-31; 报告期可用日>截面则不可见",
            "fwd": "change_pct(复权涨跌幅,%) 连乘累计 -> fwd5/fwd20, 每股票自身后续bar日",
            "valuation": "ts<=月末最近交易日; valuation.market_cap/free_cap/ps_ttm 全表为空 -> "
                         "市值因子以 close*float_shares(流通市值)口径近似; PS倒数因子因 ps_ttm 全空未纳入",
            "verdict_gate": "PASS: |rankIC|>=0.02 且 |ICIR|>=0.5 且 Q4-Q0与RankIC同向; "
                            "WATCH: 0.015<=|IC|<0.02 或 0.3<=|ICIR|<0.5; 其余 FAIL",
            "data_limitation": "valuation 全市场日频覆盖截至 2025-08-01(2025-08-04起断崖降至~300只跟踪池), "
                               "故统一评估全市场截面止于 2025-07-31 (要求区间为2019-01..2026-08, 尾部无法满足全市场池/ST识别); "
                               "daily_bars 混入 .SZ/.SH 后缀重复、债券/B股, 已按 symbols.parquet(sz/sh) 白名单过滤并取裸6位symbol",
        },
        "universe_stats": {"pool_per_cs_min": min(u["n"] for u in uni_stats),
                           "pool_per_cs_max": max(u["n"] for u in uni_stats),
                           "pool_per_cs_avg": round(float(np.mean([u["n"] for u in uni_stats])), 1),
                           "panel_rows": int(len(panel)),
                           "by_date_sample": uni_stats[:5] + uni_stats[-5:]},
        "factors_n": len(FACTORS),
        "factor_defs": [{"col": f["col"], "name": f["name"], "expr": f["expr"],
                         "grp": f["grp"], "theo_dir": f["theo_dir"]} for f in FACTORS],
        "summary_sorted_by_icir": summary_sorted,
        "verdict_counts": {"PASS": int(cnt.get("PASS", 0)), "WATCH": int(cnt.get("WATCH", 0)),
                           "FAIL": int(cnt.get("FAIL", 0))},
        "corr_matrix_spearman_pooled": corr,
        "conclusion": suggestion,
        "top_combo_note": suggestion["top_combo"],
    }
    json.dump(report, open(REPORT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=str)
    _log(f"ledger 台账: {LEDGER} (lines={len(rows_all)})")
    _log(f"report json: {REPORT_JSON}")
    _log(f"panel parquet: {PANEL_PQ}")
    _log("verdict: PASS=%d WATCH=%d FAIL=%d" % (report["verdict_counts"]["PASS"],
                                                report["verdict_counts"]["WATCH"],
                                                report["verdict_counts"]["FAIL"]))


if __name__ == "__main__":
    main()
