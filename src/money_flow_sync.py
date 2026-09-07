# -*- coding: utf-8 -*-
"""板块资金流 money_flow_sync: h5i.money_flow_estimate 建表 / 每日快照同步.

数据源: 东财市场资金流 (akshare.stock_market_fund_flow), 返回约 50-90 个行业板块
的当日资金快照 (一页全部); 列经 MFE_MAP 映射为英文, ts=采集时刻(当日快照版本).
表语义: 每日一次"行业资金流快照"; 同日重跑以新采集时刻再快照(保留历史采集版本),
由 (ts 日期) 幂等: 同日内不重复采集.

h5i 单调追加约束: append 需 ts >= 当前最大 ts; 每日采集时刻自然递增, 满足约束.
回填更早日期不可行(与 margin 同), 因此仅支持"当前或未来时刻"快照.

用法:
  python money_flow_sync.py create           # 建表(幂等)
  python money_flow_sync.py sync --force     # 采集当日快照(--force 忽略同日去重)
  python money_flow_sync.py stats
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

import pandas as pd
import pyarrow as pa

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H5I_PATH = os.path.join(_BASE, "data", "h5i", "market.db")

MFE_MAP = {
    "序号": "seq", "行业": "sector", "行业指数": "sector_index",
    "行业-涨跌幅": "sector_change_pct", "流入资金": "inflow",
    "流出资金": "outflow", "净额": "net_amount", "公司家数": "company_count",
    "领涨股": "leader_stock", "领涨股-涨跌幅": "leader_change_pct",
    "当前价": "price",
}
MFE_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us")),
    ("seq", pa.int64()),
    ("sector", pa.string()),
    ("sector_index", pa.float64()),
    ("sector_change_pct", pa.float64()),
    ("inflow", pa.float64()),
    ("outflow", pa.float64()),
    ("net_amount", pa.float64()),
    ("company_count", pa.int64()),
    ("leader_stock", pa.string()),
    ("leader_change_pct", pa.float64()),
    ("price", pa.float64()),
])
FLOAT_COLS = ["sector_index", "sector_change_pct", "inflow", "outflow",
              "net_amount", "leader_change_pct", "price"]
INT_COLS = ["seq", "company_count"]


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] [money_flow_sync] {msg}", flush=True)


def _open_db(read_only: bool = False):
    import h5i_db
    return h5i_db.Database(H5I_PATH, read_only=read_only)


def _tables(db) -> set:
    try:
        return {r["table_name"] for _, r in db.sql("SHOW TABLES").to_pandas().iterrows()}
    except Exception:
        return set()


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "None", "nan", "NaN", "--"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def fetch_rows() -> list[dict]:
    """拉取当日行业资金流 (同花顺行业资金流: stock_fund_flow_industry).

    列与 MFE_MAP 完全一致 (序号/行业/行业指数/.../当前价); 资金单位=亿元,
    与历史 money_flow_estimate 数据单位一致. 该接口可直连(不依赖东财).
    """
    import akshare as ak
    df = ak.stock_fund_flow_industry(symbol="即时")
    if df is None or df.empty:
        raise RuntimeError("stock_fund_flow_industry 返回空")
    rows = []
    for _, r in df.iterrows():
        row = {}
        for zh, en in MFE_MAP.items():
            v = r.get(zh)
            if en in INT_COLS:
                x = _num(v)
                row[en] = 0 if x is None else int(x)
            elif en in FLOAT_COLS:
                row[en] = _num(v)
            else:
                row[en] = "" if v is None else str(v)
        if row.get("sector"):
            rows.append(row)
    if not rows:
        raise RuntimeError("行业资金流解析后为空")
    return rows


def create_table() -> dict:
    db = _open_db()
    try:
        if "money_flow_estimate" in _tables(db):
            return {"ok": True, "exists": True}
        db.create_table("money_flow_estimate", MFE_SCHEMA, time_column="ts")
        return {"ok": True, "exists": False, "schema": MFE_SCHEMA.names}
    finally:
        db.close()


def sync_money_flow(force: bool = False, capture_at: dt.datetime | None = None) -> dict:
    """采集当日行业资金流快照并追加 h5i (幂等: 同日内去重, --force 可重采)."""
    t0 = time.time()
    db = _open_db()
    try:
        if "money_flow_estimate" not in _tables(db):
            db.create_table("money_flow_estimate", MFE_SCHEMA, time_column="ts")
        # 同日去重
        df_max = db.sql(
            "SELECT MAX(CAST(ts AS DATE)) d FROM money_flow_estimate").to_pandas()
        cur_day = None
        if df_max is not None and len(df_max) and df_max.iloc[0, 0] is not None:
            cur_day = df_max.iloc[0, 0]
        if isinstance(cur_day, str):
            cur_day = pd.Timestamp(cur_day).date()
        today = dt.date.today()
        if cur_day == today and not force:
            return {"ok": True, "skipped_same_day": True, "max_day": str(cur_day)}
        if cur_day is not None and cur_day > today:
            return {"ok": True, "note": "已有未来日快照, 跳过", "max_day": str(cur_day)}

        rows = fetch_rows()
        ts = capture_at or dt.datetime.now()
        ts = ts.replace(microsecond=0)
        df = pd.DataFrame(rows)
        df.insert(0, "ts", pd.Timestamp(ts).as_unit("us"))
        df = df[MFE_SCHEMA.names].sort_values(["ts", "sector"])
        for c in FLOAT_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in INT_COLS:
            df[c] = df[c].astype("int64")
        tbl = pa.Table.from_pandas(df, schema=MFE_SCHEMA, preserve_index=False)
        db.append("money_flow_estimate", tbl)
        return {"ok": True, "captured_at": ts.isoformat(), "rows": int(len(df)),
                "max_day_was": (str(cur_day) if cur_day else None),
                "elapsed_s": round(time.time() - t0, 1)}
    finally:
        db.close()


def stats() -> dict:
    db = _open_db(read_only=True)
    try:
        if "money_flow_estimate" not in _tables(db):
            return {"ok": False, "error": "money_flow_estimate 尚未建表"}
        agg = db.sql("SELECT COUNT(*) n, MIN(ts) mn, MAX(ts) mx, "
                     "COUNT(DISTINCT CAST(ts AS DATE)) days "
                     "FROM money_flow_estimate").to_pandas().iloc[0]
        return {"ok": True, "rows": int(agg["n"]),
                "min_ts": str(agg["mn"]), "max_ts": str(agg["mx"]),
                "days": int(agg["days"])}
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("create")
    p = sub.add_parser("sync")
    p.add_argument("--force", action="store_true")
    sub.add_parser("stats")
    args = ap.parse_args()
    if args.cmd == "create":
        print(json.dumps(create_table(), ensure_ascii=False, default=str))
    elif args.cmd == "sync":
        print(json.dumps(sync_money_flow(force=args.force), ensure_ascii=False,
                         default=str, indent=1))
    elif args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, default=str, indent=1))
    else:
        ap.print_help()
