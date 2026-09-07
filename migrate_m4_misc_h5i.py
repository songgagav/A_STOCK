# -*- coding: utf-8 -*-
"""migrate_m4_misc_h5i: DuckDB 退役前最后一批杂项读源迁入 h5i (m4/A 部分)

把 DuckDB 中 dashboard/build_factor_views 仍直读的表迁入 h5i 时间列库:
  - northbound_money      -> h5i.northbound_money      (ts=trade_date 00:00)
  - money_flow_estimate   -> h5i.money_flow_estimate   (ts=fetch_time 解析)

market_panel.py 实际只读 daily_bars + symbols, 二者已分别在 h5i / parquet,
无需额外迁移 (market_panel 读取源在 A3 步骤切到 h5i)。

提供:
  migrate()                  一次性迁移 (幂等: 已存在同名表则跳过)
  append_northbound_money(df) 每日写入辅助 (单调追加, 历史日跳过并告警)
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd
import pyarrow as pa

_BASE = os.path.dirname(os.path.abspath(__file__))
H5I_PATH = os.path.join(_BASE, "data", "h5i", "market.db")
DUCKDB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "legacy_stockdb.duckdb")

NB_COLS = [
    "trade_date", "market_type", "market_code", "net_buy_amount", "buy_amount",
    "sell_amount", "cumulative_net_buy", "daily_inflow", "daily_balance",
    "holding_market_value", "leading_stock", "leading_change_pct", "sh_index",
    "sh_index_change_pct", "leading_stock_code", "source", "ingested_at",
]
NB_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us")),
    ("trade_date", pa.date32()),
    ("market_type", pa.string()),
    ("market_code", pa.string()),
    ("net_buy_amount", pa.float64()),
    ("buy_amount", pa.float64()),
    ("sell_amount", pa.float64()),
    ("cumulative_net_buy", pa.float64()),
    ("daily_inflow", pa.float64()),
    ("daily_balance", pa.float64()),
    ("holding_market_value", pa.float64()),
    ("leading_stock", pa.string()),
    ("leading_change_pct", pa.float64()),
    ("sh_index", pa.float64()),
    ("sh_index_change_pct", pa.float64()),
    ("leading_stock_code", pa.string()),
    ("source", pa.string()),
    ("ingested_at", pa.timestamp("us")),
])

# money_flow_estimate: duck 中列为中文, 迁入 h5i 统一转英文 (供 SQL 无引号访问)
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


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] [migrate_m4_misc] {msg}", flush=True)


def _open():
    import h5i_db
    if not os.path.isdir(H5I_PATH):
        return None
    return h5i_db.Database(H5I_PATH)


def _tables(db) -> set:
    try:
        return {r["table_name"] for _, r in db.sql("SHOW TABLES").to_pandas().iterrows()}
    except Exception:
        return set()


def _max_ts(db, table: str):
    try:
        df = db.sql(f"SELECT MAX(ts) m FROM {table}").to_pandas()
        v = df.iloc[0, 0] if df is not None and len(df) else None
        return None if v is None else pd.Timestamp(v)
    except Exception:
        return None


def _norm_ts(v):
    if v is None:
        return pd.NaT
    if isinstance(v, dt.datetime):
        return pd.Timestamp(v)
    if isinstance(v, dt.date):
        return pd.Timestamp(v)
    try:
        return pd.Timestamp(v)
    except Exception:
        return pd.NaT


def migrate() -> dict:
    """一次性把两表从 DuckDB 迁入 h5i (幂等). DuckDB 不在时跳过对应表."""
    import duckdb
    rep: dict = {"duckdb_exists": os.path.exists(DUCKDB_PATH)}
    if not rep["duckdb_exists"]:
        rep["error"] = "DuckDB 不存在, 无法迁移 (需在退役前完成)"
        return rep
    db = _open()
    if db is None:
        rep["error"] = "h5i 库不存在"
        return rep
    have = _tables(db)
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        # ---- northbound_money ----
        if "northbound_money" not in have:
            df = con.execute("SELECT * FROM northbound_money").fetchdf()
            if not df.empty:
                df["ts"] = pd.to_datetime(df["trade_date"]).dt.normalize()
                for c in NB_SCHEMA.names:
                    if c not in df.columns and c != "ts":
                        df[c] = None
                df = df[NB_SCHEMA.names]
                df = df.sort_values(["ts", "market_type", "market_code"])
                db.create_table("northbound_money", NB_SCHEMA, time_column="ts")
                db.append("northbound_money",
                          pa.Table.from_pandas(df, schema=NB_SCHEMA, preserve_index=False))
                rep["northbound_money"] = {"migrated": True, "rows": int(len(df))}
                _log(f"northbound_money 迁入 {len(df)} 行")
            else:
                rep["northbound_money"] = {"migrated": False, "rows": 0}
        else:
            rep["northbound_money"] = {"migrated": False, "exists": True}
        # ---- money_flow_estimate ----
        if "money_flow_estimate" not in have:
            df = con.execute("SELECT * FROM money_flow_estimate").fetchdf()
            if df is not None and not df.empty:
                df["_ts"] = df["fetch_time"].map(_norm_ts)
                df = df.dropna(subset=["_ts"]).copy()
                df["ts"] = pd.to_datetime(df["_ts"])
                df = df.rename(columns=MFE_MAP)
                for c in MFE_SCHEMA.names:
                    if c not in df.columns:
                        df[c] = None
                df = df[MFE_SCHEMA.names]
                # int/float 化
                for c in ("seq", "company_count"):
                    df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
                for c in MFE_SCHEMA.names:
                    if MFE_SCHEMA.field(c).type == pa.float64() and df[c].dtype != "float64":
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.sort_values(["ts", "sector"])
                df["seq"] = df["seq"].fillna(0).astype("int64")
                df["company_count"] = df["company_count"].fillna(0).astype("int64")
                db.create_table("money_flow_estimate", MFE_SCHEMA, time_column="ts")
                db.append("money_flow_estimate",
                          pa.Table.from_pandas(df, schema=MFE_SCHEMA, preserve_index=False))
                rep["money_flow_estimate"] = {"migrated": True, "rows": int(len(df)),
                                              "dropped_null_fetch_time": int(
                                                  (len(con.execute(
                                                      "SELECT * FROM money_flow_estimate").fetchdf()) - len(df))
                                                  if not con.execute(
                                                      "SELECT * FROM money_flow_estimate").fetchdf().empty else 0)}
                _log(f"money_flow_estimate 迁入 {len(df)} 行 (跳过无 fetch_time 行)")
            else:
                rep["money_flow_estimate"] = {"migrated": False, "rows": 0}
        else:
            rep["money_flow_estimate"] = {"migrated": False, "exists": True}
    finally:
        con.close()
        try:
            db.close()
        except Exception:
            pass
    return rep


# ============================================================
# 每日写入辅助 (供 update_db 在 duck 写成功后同步 h5i; H5I_WRITE=1 时启用)
# ============================================================
def append_northbound_money(df: pd.DataFrame) -> dict:
    """把当日北向 4 行(duck 风格: trade_date + 全列)按单调约束 append 到 h5i.

    只追加 ts(交易日) > h5i 现有最大 ts 的行; 同日/回填跳过并告警 (防重复).
    """
    base = {"table": "northbound_money", "appended": 0, "skipped_rows": 0,
            "dates": [], "ok": True}
    if df is None or df.empty or "trade_date" not in df.columns:
        return base
    db = _open()
    if db is None or "northbound_money" not in _tables(db):
        base.update({"ok": False, "error": "h5i northbound_money 不存在, 先跑 migrate()"})
        if db is not None:
            db.close()
        return base
    try:
        m = _max_ts(db, "northbound_money")
        work = df.copy()
        work["_d"] = pd.to_datetime(work["trade_date"], errors="coerce").dt.normalize()
        work = work.dropna(subset=["_d"])
        if m is None:
            base.update({"ok": False, "error": "h5i northbound_money 为空, 请迁移"})
            return base
        old = work[work["_d"] <= m]
        new = work[work["_d"] > m]
        base["skipped_rows"] = int(len(old))
        if len(old):
            _log(f"northbound 跳过回填/重复 {len(old)} 行 (<=h5i 最大 {m.date()})")
        if new.empty:
            return base
        new = new.sort_values("_d")
        part = new.copy()
        for c in NB_SCHEMA.names:
            if c not in part.columns and c != "ts":
                part[c] = None
        part["ts"] = part["_d"]
        part = part[NB_SCHEMA.names]
        db.append("northbound_money",
                  pa.Table.from_pandas(part, schema=NB_SCHEMA, preserve_index=False))
        base["appended"] = int(len(part))
        base["dates"] = sorted(str(x.date()) for x in part["_d"].unique())
    except Exception as e:  # noqa: BLE001
        base.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        try:
            db.close()
        except Exception:
            pass
    return base


if __name__ == "__main__":
    import json
    print(json.dumps(migrate(), ensure_ascii=False, indent=2, default=str))
