# -*- coding: utf-8 -*-
# ============================================================
# valuation_backfill.py -- valuation 全市场估值覆盖率回补 / 快照并入 / 整表重建
#
# 背景
# ----
# h5i 库 data/h5i/market.db 中 valuation 表(ts=交易日, symbol, pe_ttm, pb, ps_ttm,
# pcf_ncf_ttm, is_st, market_cap, free_cap, float_shares, source) 全市场日频覆盖到
# 2025-08-01(约 5426 只/日), 2025-08-04 起骤降至约 293~360 只跟踪池(至 2026-08-19),
# 此后无行。因子 pb_inv 等对该段其余标的只能取"最近可见(可能滞后约 1 年)"的估值,
# 存在明显滞后。
#
# 本模块完成三件事(均只使用 h5i + pyarrow + pandas, 不引用 DuckDB, 无常驻进程):
#   [1] 历史近似 PB 回补 2025-08-04..2026-08-19:
#         pb_est = close / bvps_pit   (bvps_pit = 该股当日"最新可见报告期" bvps,
#         报告期披露截止日: 年报次年04-30/Q1 04-30/中报08-31/Q3 10-31, 只取 <=当日可见
#         最新一期; 缺 bvps 或 bvps<=0 的该行 pb 置空但行保留)
#         close 取 daily_bars 当日收盘; float_shares 取 valuation 该股最近可得
#         (向前, PIT); free_cap=close*float_shares; source='approx_pb_rebuild';
#         pe_ttm/ps_ttm/pcf_ncf_ttm/market_cap/is_st 置空。
#         仅补齐"该日原行数 < 4000"的交易日, 且仅针对 whitelist(sz/sh 裸6位且
#         is_active 且当日有 bar)中当日尚缺 symbol。
#   [2] 快照并入 merge_snapshot_to_valuation(): valuation_snapshot 某 fetch 覆盖
#         >=4500 行(如 2026-09-04 的 5181)时, 把该快照全部行作为 fetch_time 所在
#         交易日写入 valuation; 并已把 2026-08-31 与 2026-09-04 两次现存快照并入。
#   [3] 整表重建: 旧行(历史全保留) + 回补近似行 + 快照并入日, 按 (ts,symbol) 升序
#         分块 append(30 万行/块) 重建 valuation(先删旧表再重建, 不动同文件其它表)。
#
# CLI:
#   python valuation_backfill.py check                 # 重建前校验/导出备份
#   python valuation_backfill.py approx-build --out PQ # 生成近似行 parquet + 统计
#   python valuation_backfill.py snapshot-merge        # 并入最近一次 >=4500 的快照
#   python valuation_backfill.py snapshot-merge --day 2026-08-31
#   python valuation_backfill.py rebuild --approx PQ   # 整表重建
#   python valuation_backfill.py stats                 # 回补段逐日行数/pb非空率复评
# ============================================================
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

H5I_PATH = os.path.join(_BASE, "data", "h5i", "market.db")
SYMBOLS_PQ = os.path.join(_BASE, "data", "h5i", "static", "symbols.parquet")
REPORT_JSON = os.path.join(_BASE, "data", "factor_mine", "valuation_backfill_report.json")

WINDOW_LO = "2025-08-04"
WINDOW_HI = "2026-08-19"
SNAP_DATES_LEGACY = ["2026-08-31", "2026-09-04"]   # 现存两次快照并入日
APPEND_CHUNK = 300_000
SOURCE_APPROX = "approx_pb_rebuild"
SOURCE_SNAPSHOT = "snapshot"

VAL_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us")), ("symbol", pa.string()),
    ("pe_ttm", pa.float64()), ("pb", pa.float64()), ("ps_ttm", pa.float64()),
    ("pcf_ncf_ttm", pa.float64()), ("is_st", pa.bool_()),
    ("market_cap", pa.float64()), ("free_cap", pa.float64()),
    ("float_shares", pa.float64()), ("source", pa.string())])


def _open_db(read_only=False):
    import h5i_db
    return h5i_db.Database(H5I_PATH, read_only=read_only)


def _sql(db, q):
    return db.sql(q).to_pandas()


def _ts_unit_us(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.as_unit("us")


# ---------------------------------------------------------------------------
# 报告期披露可见规则 (与 factor_fusion._avail_date 一致)
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
    return pd.Timestamp(f"{y}-12-31")


def trading_days_in(start: str, end: str) -> list[str]:
    db = _open_db(read_only=True)
    try:
        q = ("SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars "
             f"WHERE CAST(ts AS DATE) >= DATE '{start}' "
             f"AND CAST(ts AS DATE) <= DATE '{end}' ORDER BY 1")
        days = _sql(db, q)["d"].astype(str).tolist()
    finally:
        db.close()
    return days


def load_whitelist() -> set[str]:
    d = pd.read_parquet(SYMBOLS_PQ)
    d = d[d["market"].isin(["sz", "sh"])]
    d = d[d["is_active"] == True]  # noqa: E712
    return set(d["symbol"].astype(str).str.zfill(6))


def _fin_frame_bvps(db) -> pd.DataFrame:
    """financials -> {symbol, period(ts), avail, bvps}, 同(symbol,avail)只留最新报告期."""
    df = _sql(db, "SELECT CAST(ts AS DATE) ts, symbol, bvps FROM financials")
    df["ts"] = pd.to_datetime(df["ts"])
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df = df[df["ts"].dt.month.isin([3, 6, 9, 12])].copy()
    df = df[(df["bvps"].notna()) & (df["bvps"] > 0)]
    df["avail"] = df["ts"].map(avail_date)
    df = (df.sort_values(["symbol", "ts"])
            .drop_duplicates(["symbol", "ts"], keep="last")
            .sort_values(["symbol", "avail", "ts"])
            .drop_duplicates(["symbol", "avail"], keep="last"))
    return df[["symbol", "avail", "bvps"]].reset_index(drop=True)


def _val_float_frame(db) -> pd.DataFrame:
    """valuation 从 2024-06 起按日浮盈 float_shares, 用于 PIT 前进式 vmap 预热."""
    df = _sql(db, "SELECT CAST(ts AS DATE) d, symbol, float_shares, pb "
                  "FROM valuation WHERE CAST(ts AS DATE) >= DATE '2024-06-01'")
    df["d"] = pd.to_datetime(df["d"])
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df = df.sort_values("d").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# [1] 近似 PB 回补
# ---------------------------------------------------------------------------
def build_approx_pb_rows(out_parquet: str) -> dict:
    """按窗口内每个交易日补齐 whitelist 中当日缺失的 symbol 近似行.

    输出 DataFrame 列 == valuation schema 列(ts 为该日零点), source=approx_pb_rebuild;
    同时把逐日统计写入返回 dict (rows_all 一并落盘 parquet)。
    """
    t0 = time.time()
    days = trading_days_in(WINDOW_LO, WINDOW_HI)
    whitelist = load_whitelist()
    db = _open_db()
    try:
        bars = _sql(db, "SELECT CAST(ts AS DATE) d, symbol, close FROM daily_bars "
                        f"WHERE CAST(ts AS DATE) >= DATE '{WINDOW_LO}' "
                        f"AND CAST(ts AS DATE) <= DATE '{WINDOW_HI}'")
        bars["d"] = pd.to_datetime(bars["d"])
        bars["symbol"] = bars["symbol"].astype(str).str.zfill(6)
        bars = bars[(bars["close"].notna()) & (bars["close"] > 0)]
        bars = bars[bars["symbol"].isin(whitelist)].copy()
        bars = bars.drop_duplicates(["d", "symbol"], keep="last")

        # 当日既有行(窗口内 valuation 原行): symbol 集合 + pb 非空统计
        exist = _sql(db, "SELECT CAST(ts AS DATE) d, symbol, pb FROM valuation "
                         f"WHERE CAST(ts AS DATE) >= DATE '{WINDOW_LO}' "
                         f"AND CAST(ts AS DATE) <= DATE '{WINDOW_HI}'")
        exist["d"] = pd.to_datetime(exist["d"])
        exist["symbol"] = exist["symbol"].astype(str).str.zfill(6)
        ex_set = {d: set(g["symbol"]) for d, g in exist.groupby("d")}
        ex_pb_ok = {d: int(g["pb"].notna().sum()) for d, g in exist.groupby("d")}
        ex_n = {d: int(len(g)) for d, g in exist.groupby("d")}

        # 原行数<4000 才回补 (>=4000 视为该日已有全市场真值)
        fill_days = {d for d in days if ex_n.get(pd.Timestamp(d), 0) < 4000}

        fin = _fin_frame_bvps(db).sort_values("avail").reset_index(drop=True)
        f_key = fin["avail"].to_numpy(dtype="datetime64[us]")
        f_fp, f_map = 0, {}

        valf = _val_float_frame(db)
        v_key = valf["d"].to_numpy(dtype="datetime64[us]")
        v_fp, v_map = 0, {}
        val_by_day = {d: g for d, g in bars.groupby("d")}

        out_rows: list[pd.DataFrame] = []
        per_day: list[dict] = []
        total = 0
        for i, D in enumerate(days):
            Dts = pd.Timestamp(D)
            u = np.datetime64(Dts, "us")
            # 财务可见性推进
            pos = int(np.searchsorted(f_key, u, side="right"))
            if pos > f_fp:
                blk = fin.iloc[f_fp:pos]
                f_fp = pos
                if len(blk):
                    tail = blk.drop_duplicates("symbol", keep="last")
                    for r in tail.itertuples(index=False):
                        f_map[r.symbol] = r.bvps
            # 估值 float_shares 可见性推进
            pos = int(np.searchsorted(v_key, u, side="right"))
            if pos > v_fp:
                blk = valf.iloc[v_fp:pos]
                v_fp = pos
                if len(blk):
                    tail = blk[blk["float_shares"].notna() &
                               (blk["float_shares"] > 0)].drop_duplicates("symbol", keep="last")
                    for r in tail.itertuples(index=False):
                        v_map[r.symbol] = float(r.float_shares)

            if D not in fill_days:
                continue
            day_bars = val_by_day.get(Dts)
            if day_bars is None or len(day_bars) == 0:
                continue
            existing = ex_set.get(Dts)
            if existing is None:
                existing = set()
            missing = sorted(set(day_bars["symbol"]) - existing)
            if not missing:
                continue
            b = day_bars[day_bars["symbol"].isin(missing)].copy()
            b["bvps"] = b["symbol"].map(f_map)
            b["fs"] = b["symbol"].map(v_map)
            close = b["close"].to_numpy(dtype=float)
            bvps = b["bvps"].to_numpy(dtype=float)
            fs = b["fs"].to_numpy(dtype=float)
            pb = np.full(len(b), np.nan, dtype=float)
            ok = np.isfinite(bvps) & (bvps > 0)
            pb[ok] = close[ok] / bvps[ok]
            fc = np.full(len(b), np.nan, dtype=float)
            okf = np.isfinite(fs) & (fs > 0)
            fc[okf] = close[okf] * fs[okf]
            fs_out = np.where(okf, fs, np.nan)
            out = pd.DataFrame({
                "ts": pd.Timestamp(Dts.date()).as_unit("us"),
                "symbol": b["symbol"].to_numpy(),
                "pe_ttm": np.full(len(b), np.nan),
                "pb": pb,
                "ps_ttm": np.full(len(b), np.nan),
                "pcf_ncf_ttm": np.full(len(b), np.nan),
                "is_st": pd.array([None] * len(b), dtype="boolean"),
                "market_cap": np.full(len(b), np.nan),
                "free_cap": fc,
                "float_shares": fs_out,
                "source": SOURCE_APPROX,
            })
            out_rows.append(out)
            total += len(out)
            en = ex_n.get(Dts, 0)
            epb = ex_pb_ok.get(Dts, 0)
            tot = en + len(out)
            pbo = int(epb + int(np.isfinite(pb).sum()))
            per_day.append({
                "date": D, "exist_n": en, "approx_n": len(out),
                "approx_pb_ok": int(np.isfinite(pb).sum()),
                "total_n": tot, "pb_nonnull": pbo,
                "pb_nonnull_rate": round(pbo / tot, 4) if tot else None,
            })
            if (i + 1) % 25 == 0:
                print(f"  ...{D} approx_rows={total} elapse={time.time() - t0:.0f}s",
                      flush=True)
        if out_rows:
            full = pd.concat(out_rows, ignore_index=True)
            pq.write_table(pa.Table.from_pandas(full, schema=VAL_SCHEMA,
                                                preserve_index=False), out_parquet)
            n_rows = len(full)
        else:
            n_rows = 0
    finally:
        db.close()

    day_df = pd.DataFrame(per_day)
    stats = {
        "window": [WINDOW_LO, WINDOW_HI],
        "days_total": len(days), "days_filled": len(day_df),
        "approx_rows_total": int(n_rows),
        "day_min_n": (int(day_df["total_n"].min()) if len(day_df) else None),
        "day_max_n": (int(day_df["total_n"].max()) if len(day_df) else None),
        "day_mean_n": (round(float(day_df["total_n"].mean()), 1) if len(day_df) else None),
        "day_min_pb_rate": (float(day_df["pb_nonnull_rate"].min()) if len(day_df) else None),
        "day_mean_pb_rate": (round(float(day_df["pb_nonnull_rate"].mean()), 4) if len(day_df) else None),
        "days_below_4600": (int((day_df["total_n"] < 4600).sum()) if len(day_df) else None),
        "days_pb_rate_below_09": (int((day_df["pb_nonnull_rate"] < 0.9).sum()) if len(day_df) else None),
        "out_parquet": out_parquet,
        "elapsed_s": round(time.time() - t0, 1),
    }
    if len(day_df):
        stats["per_day"] = day_df.to_dict(orient="records")
    print(json.dumps(stats, ensure_ascii=False, default=str))
    return stats


# ---------------------------------------------------------------------------
# [2] 快照并入
# ---------------------------------------------------------------------------
def _snap_rows_to_valuation_frame(db, day: pd.Timestamp) -> pd.DataFrame:
    """把某日 valuation_snapshot 全部 symbol 规整成 valuation schema 行 (ts=day 零点).

    市值口径: 快照 total_mv/float_mv 存在"亿"与"元"混段(实测 08-31 段=元, 09-04 段=亿),
    此处统一为"元"(与 valuation 历史 free_cap=close*float_shares 的口径一致):
    段中位数 >1e6 视为已是元; 否则(亿)乘 1e8。
    """
    dstr = day.strftime("%Y-%m-%d")
    df = _sql(db, f"SELECT symbol, price, pe_ttm, pb, total_mv, float_mv, "
                  f"float_shares, is_st FROM valuation_snapshot "
                  f"WHERE CAST(ts AS DATE) = DATE '{dstr}'")
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df = df.sort_values("symbol").drop_duplicates("symbol", keep="last")
    for c in ("total_mv", "float_mv"):
        x = pd.to_numeric(df[c], errors="coerce")
        med = x.median()
        if pd.notna(med) and not np.isfinite(med):
            med = np.nan
        if pd.notna(med) and med <= 1e6:      # 亿段 -> 元
            x = x * 1e8
        df[c] = x
    is_st = df["is_st"].astype(str).str.strip().str.lower().map(
        {"true": True, "false": False, "1": True, "0": False})
    out = pd.DataFrame({
        "ts": day.as_unit("us"),
        "symbol": df["symbol"].to_numpy(),
        "pe_ttm": pd.to_numeric(df["pe_ttm"], errors="coerce").to_numpy(),
        "pb": pd.to_numeric(df["pb"], errors="coerce").to_numpy(),
        "ps_ttm": np.full(len(df), np.nan),
        "pcf_ncf_ttm": np.full(len(df), np.nan),
        "is_st": pd.array(is_st.to_numpy(), dtype="boolean"),
        "market_cap": df["total_mv"].to_numpy(),
        "free_cap": df["float_mv"].to_numpy(),
        "float_shares": pd.to_numeric(df["float_shares"], errors="coerce").to_numpy(),
        "source": SOURCE_SNAPSHOT,
    })
    return out


def _append_frames(db, frames: list[pd.DataFrame]):
    """按 (ts,symbol) 升序排序后分块 append 到 valuation (30万行/块)."""
    if not frames:
        return 0
    full = pd.concat(frames, ignore_index=True)
    full["ts"] = _ts_unit_us(full["ts"])
    full = full.sort_values(["ts", "symbol"]).reset_index(drop=True)
    total = 0
    for i in range(0, len(full), APPEND_CHUNK):
        sl = full.iloc[i:i + APPEND_CHUNK]
        t = pa.Table.from_pandas(sl, schema=VAL_SCHEMA, preserve_index=False)
        db.append("valuation", t)
        total += len(sl)
    return total


def merge_snapshot_to_valuation(snapshot_day_or_latest=True, day=None) -> dict:
    """把 valuation_snapshot 的"整段全市场快照"并入 valuation 当日行.

    snapshot_day_or_latest=True 时自动选最近一个 覆盖(distinct symbol) >=4500 的
    fetch 所在交易日; 否则用显式 day (YYYY-MM-DD)。已在当日存在的 symbol 跳过。
    """
    db = _open_db()
    try:
        if day is not None:
            days = [pd.Timestamp(str(day)[:10]).normalize()]
        else:
            q = ("SELECT CAST(ts AS DATE) d, COUNT(DISTINCT symbol) n "
                 "FROM valuation_snapshot GROUP BY 1 HAVING COUNT(DISTINCT symbol) >= 4500 "
                 "ORDER BY 1 DESC LIMIT 1")
            hit = _sql(db, q)
            if hit.empty:
                db.close()
                return {"ok": False, "reason": "无 >=4500 行的快照段"}
            days = [pd.Timestamp(hit.iloc[0, 0])]
        reports = []
        for D in days:
            dstr = D.strftime("%Y-%m-%d")
            snap = _sql(db, f"SELECT COUNT(DISTINCT symbol) n FROM valuation_snapshot "
                            f"WHERE CAST(ts AS DATE)=DATE '{dstr}'")
            snap_n = int(snap.iloc[0, 0]) if len(snap) else 0
            exist = _sql(db, f"SELECT symbol FROM valuation WHERE CAST(ts AS DATE)=DATE '{dstr}'")
            exist_set = set(exist["symbol"].astype(str).str.zfill(6))
            fr = _snap_rows_to_valuation_frame(db, D)
            if fr.empty:
                reports.append({"day": dstr, "snapshot_symbols": snap_n,
                                "ok": False, "reason": "snapshot 无行"})
                continue
            fr = fr[~fr["symbol"].isin(exist_set)]
            n_app = _append_frames(db, [fr])
            reports.append({
                "day": dstr, "snapshot_symbols": snap_n,
                "exist_skip": len(exist_set), "appended": int(n_app),
                "ok": True,
            })
    finally:
        db.close()
    return {"ok": True, "merged": reports}


# ---------------------------------------------------------------------------
# [3] 整表重建
# ---------------------------------------------------------------------------
def export_valuation_backup(out_parquet: str) -> dict:
    db = _open_db(read_only=True)
    try:
        tab = db.read("valuation")
        n = tab.num_rows
        pq.write_table(tab, out_parquet, compression="snappy")
        return {"rows": int(n), "backup": out_parquet,
                "size_mb": round(os.path.getsize(out_parquet) / 1e6, 1)}
    finally:
        db.close()


def rebuild_valuation(approx_parquet: str | None = None) -> dict:
    """重建 valuation: 保留行 + 近似行(可选) -> 按 (ts,symbol) 升序重建.

    内存优化: symbol 转 Categorical(按字典序), 避免 14M 行 object 字符串高占用。
    分块 append(30 万行/块), 全程保持 ts 递增。
    注意: 旧表内 source=='approx_pb_rebuild' 的行会在传入了 approx_parquet 时被剔除,
    改用 parquet 中的完整近似行集, 保证重复执行幂等; symbol 为空的脏行(如历史遗留
    误转 NaN)一律剔除。
    """
    t0 = time.time()
    db = _open_db()
    try:
        tab = db.read("valuation")
        old_n = tab.num_rows
        print(f"[rebuild] read old valuation rows={old_n}", flush=True)
        df = tab.to_pandas()
        del tab
        gc.collect()
        df["ts"] = _ts_unit_us(df["ts"])
        df["symbol"] = df["symbol"].astype(str).str.strip()
        df = df[df["symbol"].str.fullmatch(r"\d{6}")].copy()
        df["symbol"] = df["symbol"].str.zfill(6)
        df["is_st"] = df["is_st"].astype("boolean")

        extras = []
        if approx_parquet and os.path.exists(approx_parquet):
            # 旧表中曾以近似源写入的行全部剔除(用 parquet 完整集重灌, 幂等)
            df = df[df["source"] != SOURCE_APPROX].copy()
            a = pd.read_parquet(approx_parquet)
            a["ts"] = _ts_unit_us(a["ts"])
            a["symbol"] = a["symbol"].astype(str).str.strip()
            a = a[a["symbol"].str.fullmatch(r"\d{6}")].copy()
            a["symbol"] = a["symbol"].str.zfill(6)
            a["is_st"] = a["is_st"].astype("boolean")
            extras.append(a)
            print(f"[rebuild] approx rows={len(a)}", flush=True)

        # 类别覆盖旧表 + 全部近似行(新增上市公司 symbol 只出现在近似集)
        all_cats = df["symbol"].dropna().unique()
        for x in extras:
            all_cats = np.union1d(all_cats, x["symbol"].dropna().unique())
        cats = pd.CategoricalDtype(sorted(all_cats))
        df["symbol"] = df["symbol"].astype(cats)
        for x in extras:
            x["symbol"] = x["symbol"].astype(cats)

        if extras:
            df = pd.concat([df] + extras, ignore_index=True)

        df = df.sort_values(["ts", "symbol"], kind="mergesort").reset_index(drop=True)
        before_dedup = len(df)
        df = df[~df.duplicated(subset=["ts", "symbol"], keep="last")].reset_index(drop=True)
        dup_removed = before_dedup - len(df)
        print(f"[rebuild] combined rows={before_dedup} dup_removed={dup_removed}",
              flush=True)

        # 先删旧表再整体重建 (不动同文件其它表)
        db.drop_table("valuation")
        db.create_table("valuation", VAL_SCHEMA, time_column="ts")
        total = 0
        n_chunk = 0
        for i in range(0, len(df), APPEND_CHUNK):
            sl = df.iloc[i:i + APPEND_CHUNK]
            t = pa.Table.from_pandas(sl, schema=VAL_SCHEMA, preserve_index=False)
            db.append("valuation", t)
            total += len(sl)
            n_chunk += 1
            print(f"  append chunk#{n_chunk} {total}/{len(df)} "
                  f"elapse={time.time() - t0:.0f}s", flush=True)
        # 验证
        chk = _sql(db, "SELECT COUNT(*) n FROM valuation")
        new_n = int(chk.iloc[0, 0])
        return {"old_rows": int(old_n),
                "approx_rows": int(sum(len(x) for x in extras)),
                "dup_removed": int(dup_removed), "new_rows": int(new_n),
                "ok": new_n == total, "elapsed_s": round(time.time() - t0, 1)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 复评: 回补段逐日行数 / pb 非空率
# ---------------------------------------------------------------------------
def stats_backfilled_window() -> dict:
    db = _open_db(read_only=True)
    try:
        q = ("SELECT CAST(ts AS DATE) d, COUNT(*) n, "
             "SUM(CASE WHEN pb IS NOT NULL THEN 1 ELSE 0 END) pb_ok, "
             "SUM(CASE WHEN source='approx_pb_rebuild' THEN 1 ELSE 0 END) approx_n "
             "FROM valuation "
             f"WHERE CAST(ts AS DATE) >= DATE '{WINDOW_LO}' "
             f"AND CAST(ts AS DATE) <= DATE '{WINDOW_HI}' GROUP BY 1 ORDER BY 1")
        df = _sql(db, q)
        df["pb_rate"] = df["pb_ok"] / df["n"]
        q2 = "SELECT COUNT(*) n, MIN(ts), MAX(ts) FROM valuation"
        r = _sql(db, q2)
        return {
            "window_days": len(df),
            "rows_total_window": int(df["n"].sum()),
            "approx_rows_window": int(df["approx_n"].sum()),
            "day_min_n": int(df["n"].min()), "day_max_n": int(df["n"].max()),
            "day_mean_n": round(float(df["n"].mean()), 1),
            "days_below_4600": int((df["n"] < 4600).sum()),
            "pb_rate_min": float(df["pb_rate"].min()),
            "pb_rate_mean": round(float(df["pb_rate"].mean()), 4),
            "days_pb_rate_below_09": int((df["pb_rate"] < 0.9).sum()),
            "valuation_total_rows": int(r.iloc[0, 0]),
            "valuation_min_ts": str(r.iloc[0, 1])[:10],
            "valuation_max_ts": str(r.iloc[0, 2])[:10],
            "per_day": df.to_dict(orient="records"),
        }
    finally:
        db.close()


def verify_pre_rebuild(expected_rows: int = 14_037_803) -> dict:
    db = _open_db(read_only=True)
    try:
        rep = {"ok": True, "checks": []}
        c = int(_sql(db, "SELECT COUNT(*) n FROM valuation").iloc[0, 0])
        rep["rows"] = c
        rep["checks"].append({"name": "row_count", "expect": expected_rows,
                              "got": c, "ok": c == expected_rows})
        # 抽样完好: 近端/中段/远端各抽 3 天校验非空行与键唯一
        for d in ("2025-08-01", "2026-08-19", "2024-06-28"):
            s = _sql(db, f"SELECT symbol, COUNT(*) c FROM valuation "
                         f"WHERE CAST(ts AS DATE)=DATE '{d}' GROUP BY 1 HAVING COUNT(*)>1")
            dup = 0 if s.empty else int(len(s))
            n = int(_sql(db, f"SELECT COUNT(*) n FROM valuation "
                             f"WHERE CAST(ts AS DATE)=DATE '{d}'").iloc[0, 0])
            rep["checks"].append({"name": f"sample_{d}", "rows": n,
                                  "dup_symbol": dup, "ok": n > 0 and dup == 0})
        rep["ok"] = all(x["ok"] for x in rep["checks"])
        return rep
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check")
    p2 = sub.add_parser("approx-build")
    p2.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "approx_pb_rows.parquet"))
    p3 = sub.add_parser("snapshot-merge")
    p3.add_argument("--day", default=None)
    p4 = sub.add_parser("rebuild")
    p4.add_argument("--approx", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "approx_pb_rows.parquet"))
    sub.add_parser("stats")
    args = ap.parse_args()

    if args.cmd == "check":
        rep = verify_pre_rebuild()
        print(json.dumps(rep, ensure_ascii=False, default=str))
    elif args.cmd == "approx-build":
        st = build_approx_pb_rows(args.out)
        json.dump(st, open(REPORT_JSON, "w", encoding="utf-8"),
                  ensure_ascii=False, default=str, indent=1)
        print("report ->", REPORT_JSON)
    elif args.cmd == "snapshot-merge":
        rep = merge_snapshot_to_valuation(day=args.day)
        print(json.dumps(rep, ensure_ascii=False, default=str, indent=1))
    elif args.cmd == "rebuild":
        rep = rebuild_valuation(args.approx)
        print(json.dumps(rep, ensure_ascii=False, default=str, indent=1))
        rep["stats"] = stats_backfilled_window()
        json.dump(rep, open(REPORT_JSON, "w", encoding="utf-8"),
                  ensure_ascii=False, default=str, indent=1)
        print("report ->", REPORT_JSON)
    elif args.cmd == "stats":
        rep = stats_backfilled_window()
        print(json.dumps(rep, ensure_ascii=False, default=str, indent=1))
    else:
        ap.print_help()
