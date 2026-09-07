# ============================================================
# fml_accumulate.py -- f_ml 融合因子样本积累 + 到期收益回填
#
# 目的: 按项目纪律"融合因子先独立累积实盘样本、稳定后再注入 DRL 观测", 这里
#   建立 f_ml 的纵向样本表. 每个交易日盘后:
#     1) accumulate_day: 取当日选股池(pool_snapshot)的标的, 以完整候选池横截面
#        调 compute_fml 生成权威 f_ml, 追加 (date, symbol, fml) 到样本表(幂等).
#     2) settle_mature:  对已到期(预测日后 hold=5 个交易日已过)的行, 用 DuckDB
#        回填真实未来5日收益 fwd_5, 并把 settled 置 True.
#
# 产出: data/fml_samples.parquet
#   columns: date(datetime 预测日), symbol(canon), fml, settled(bool),
#            close(预测日收盘), fwd_5(未来5日收益, 未到期为 NaN)
#
# 遵循项目数据纪律:
#   - 只读 DuckDB(read_only), 不写滥用并发; 幂等写入 parquet(按 (date,symbol)).
#   - fml 一律以当日完整候选池一次性 compute_fml 生成(industry_excess_return 是
#     横截面特征, 必须整池算才与 select 打分支口径一致).
#   - 任意异常容错, 绝不抛异常阻断 run_daily 主链路.
# ============================================================

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np
import pandas as pd

from config import DATA_DIR, DUCKDB_PATH
from db import _canon_to_db

_LOG = logging.getLogger("fml_accumulate")

SAMPLES_FILE = os.path.join(DATA_DIR, "fml_samples.parquet")
HOLD = 5            # 未来5日收益(与 fwd_5 监督目标一致)
COLUMNS = ["date", "symbol", "fml", "settled", "close", "fwd_5"]


# ---------- 读写样本表 ----------

def _load_samples() -> pd.DataFrame:
    if not os.path.exists(SAMPLES_FILE):
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_parquet(SAMPLES_FILE)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    df["date"] = pd.to_datetime(df["date"])
    return df


def _save_samples(df: pd.DataFrame) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    df.reset_index(drop=True).to_parquet(SAMPLES_FILE, index=False)


def _read_pool_snapshot(day_dir: str) -> list[dict]:
    p = os.path.join(DATA_DIR, "daily", day_dir, "pool_snapshot.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------- 调用方(selector/run_daily) ----------

def accumulate_day(day: str, day_dir: str) -> dict:
    """盘后调用: 取当日选股池, 以完整候选池重新 compute_fml 生成权威 f_ml 落样本.

    day      : YYYY-MM-DD (预测日, as_of)
    day_dir  : YYYYMMDD (pool_snapshot 目录键)
    """
    result = {"day": day, "ok": False, "n_symbols": 0, "n_appended": 0}
    try:
        from ml_fusion_bridge import compute_fml
        snap = _read_pool_snapshot(day_dir)
        if not snap:
            result["reason"] = "当日无 pool_snapshot"
            return result
        canons = [r.get("canon") for r in snap if r.get("canon")]
        if not canons:
            result["reason"] = "快照无标的"
            return result

        res = compute_fml(canons, as_of=day)
        scores = res.get("scores") or {}
        if not res.get("available") or not scores:
            result["reason"] = res.get("meta", {}).get("error", "f_ml 计算失败")
            return result

        # 组装当日样本行 (与已有样本按 (date,symbol) 幂等合并, 保留新值)
        rows = []
        for canon in canons:
            v = scores.get(canon)
            if v is None:
                continue
            rows.append({"date": day, "symbol": canon, "fml": float(v),
                         "settled": False, "close": np.nan, "fwd_5": np.nan})
        if not rows:
            result["reason"] = "无可落样本的 f_ml 分数"
            return result

        new = pd.DataFrame(rows, columns=COLUMNS)
        new["date"] = pd.to_datetime(new["date"])
        df = _load_samples()
        if df.empty:
            df = new
        else:
            prev = df[((df["date"].isin(new["date"])) & (df["symbol"].isin(new["symbol"])))]
            df = pd.concat([df[~df.index.isin(prev.index)], new], ignore_index=True)
        _save_samples(df)

        # 回填预测日 close (只读 DuckDB, 便于后续 DRL 观测对齐)
        _backfill_close(df)

        result.update({"ok": True, "n_symbols": len(canons),
                       "n_appended": len(rows),
                       "total_samples": int(df["fml"].notna().sum())})
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"[:200]
    return result


def _backfill_close(df: pd.DataFrame) -> None:
    """给新样本回填预测日的 close(用于未来回填 fwd_5 或 DRL 对齐)."""
    need = df[df["close"].isna() & df["fml"].notna()]
    if need.empty:
        return
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        for _, r in need.iterrows():
            code = _canon_to_db(r["symbol"])
            row = con.execute(
                "SELECT close FROM daily_bars WHERE symbol=? AND date<=? "
                "ORDER BY date DESC LIMIT 1",
                [code, r["date"].strftime("%Y-%m-%d")]).fetchone()
            if row and row[0]:
                df.loc[r.name, "close"] = float(row[0])
    finally:
        con.close()
    _save_samples(df)


# ---------- 到期收益回填 ----------

def settle_mature(db=None, hold: int = HOLD) -> dict:
    """对已到期的样本(unsettled 且 预测日后 hold 个交易日已收盘)回填 fwd_5.

    db: 可选 StockDB; 为 None 时内部开只读 duckdb 连接. 返回统计.
    """
    result = {"ok": False, "n_settled": 0, "n_matured": 0, "hold": hold}
    df = _load_samples()
    unsettled = df[df["settled"] != True].copy()  # noqa: E712
    if unsettled.empty:
        result.update({"ok": True, "note": "无未结算样本"})
        return result

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        max_db_date = con.execute("SELECT max(date) FROM daily_bars").fetchone()[0]
        max_db_ts = pd.Timestamp(max_db_date)
        # 每个预测日所需的最远回填日期 ≈ 该日 + hold 个交易日(用DB内该 symbol 序列精确对齐)
        matured_idx = []
        for sym, g in unsettled.groupby("symbol"):
            code = _canon_to_db(sym)
            rows = con.execute(
                "SELECT date, close FROM daily_bars WHERE symbol=? ORDER BY date",
                [code]).fetchall()
            if not rows:
                continue
            dts = pd.to_datetime([x[0] for x in rows])
            closes = pd.Series([x[1] for x in rows], index=dts, dtype=float)
            pos = {ts: i for i, ts in enumerate(dts)}
            for _, r in g.iterrows():
                i = pos.get(r["date"])
                if i is None:
                    continue
                j = i + hold
                if j >= len(dts) or not np.isfinite(closes.iloc[j]) \
                        or dts[j] > max_db_ts:
                    continue  # 尚未到期(未来第hold根K未落地)
                p0 = float(closes.iloc[i])
                p1 = float(closes.iloc[j])
                if not np.isfinite(p0) or p0 <= 0 or not np.isfinite(p1):
                    continue
                df.loc[r.name, "fwd_5"] = p1 / p0 - 1.0
                df.loc[r.name, "settled"] = True
                matured_idx.append(r.name)
        result["n_matured"] = len(matured_idx)
    finally:
        con.close()

    if matured_idx:
        _save_samples(df)
    result["ok"] = True
    result["n_settled"] = int(df["settled"].sum())
    result["total_samples"] = int(df["fml"].notna().sum())
    result["matured_rows"] = int(len(matured_idx))
    return result


# ---------- 概览 ----------

def summary() -> dict:
    df = _load_samples()
    if df.empty:
        return {"n_samples": 0, "n_settled": 0}
    return {
        "n_samples": int(len(df)),
        "n_with_fml": int(df["fml"].notna().sum()),
        "n_settled": int((df["settled"] == True).sum()),  # noqa: E712
        "n_fwd5": int(df["fwd_5"].notna().sum()),
        "date_min": str(df["date"].min().date()) if len(df) else None,
        "date_max": str(df["date"].max().date()) if len(df) else None,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="f_ml 样本积累工具")
    ap.add_argument("--day", help="预测日 YYYY-MM-DD (与 --day_dir 配合)")
    ap.add_argument("--day-dir", help="预测日目录键 YYYYMMDD")
    ap.add_argument("--settle", action="store_true", help="仅执行到期收益回填")
    ap.add_argument("--summary", action="store_true", help="打印样本表概览")
    a = ap.parse_args()

    if a.settle:
        print(json.dumps(settle_mature(), ensure_ascii=False, default=str))
    elif a.summary:
        print(json.dumps(summary(), ensure_ascii=False, default=str))
    else:
        if not a.day or not a.day_dir:
            raise SystemExit("需 --day 与 --day-dir")
        print(json.dumps(accumulate_day(a.day, a.day_dir),
                         ensure_ascii=False, default=str))