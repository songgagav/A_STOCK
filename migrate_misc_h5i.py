# -*- coding: utf-8 -*-
"""迁移 静态/财务 数据到 h5i + symbols->parquet (DuckDB 退役前阶段)
- financials: ts=报告期, 列名转英文(供因子)
- valuation_snapshot: ts=fetch_time, 列 pe/pb/mv...
- symbols: parquet 静态
"""
import json
import os
import time

import duckdb
import h5i_db
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

DD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "legacy_stockdb.duckdb")
H5I_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "h5i")
H5I = os.path.join(H5I_DIR, "market.db")
STATIC = os.path.join(H5I_DIR, "static")
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "h5i_migrate_misc.json")

FIN_MAP = {
    "营业总收入": "revenue", "营业总收入同比增长率": "rev_yoy",
    "净利润": "net_profit", "净利润同比增长率": "np_yoy",
    "扣非净利润": "ded_np", "扣非净利润同比增长率": "ded_np_yoy",
    "基本每股收益": "eps", "每股净资产": "bvps", "每股资本公积金": "capg_ps",
    "每股未分配利润": "und_ps", "每股经营现金流": "ocf_ps",
    "销售净利率": "net_margin", "净资产收益率": "roe",
    "净资产收益率-摊薄": "roe_diluted", "资产负债率": "debt_ratio",
    "流动比率": "current_ratio", "速动比率": "quick_ratio",
    "保守速动比率": "cons_quick", "产权比率": "debt_eq",
    "营业周期": "op_cycle", "应收账款周转天数": "ar_days",
    "销售毛利率": "gross_margin", "存货周转率": "inv_turn", "存货周转天数": "inv_days",
}

FIN_SCHEMA = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string())] +
                       [(v, pa.float64()) for v in FIN_MAP.values()] +
                       [("source", pa.string())])


def main():
    os.makedirs(STATIC, exist_ok=True)
    con = duckdb.connect(DD, read_only=True)
    db = h5i_db.Database(H5I)
    rep = {}

    # ---- financials -> h5i ----
    df = con.execute('SELECT * FROM financials').fetchdf()
    df = df.rename(columns=FIN_MAP)
    df["ts"] = pd.to_datetime(df["报告期"], errors="coerce")
    df = df.dropna(subset=["ts"])
    df["ts"] = df["ts"].dt.tz_localize(None) if df["ts"].dt.tz is not None else df["ts"]
    keep = ["ts", "symbol"] + list(FIN_MAP.values()) + ["source"]
    df = df[keep]
    df = df.sort_values(["ts", "symbol"])
    # float 化
    for c in FIN_MAP.values():
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("source",):
        df[c] = df[c].fillna("").astype(str)
    db.create_table("financials", FIN_SCHEMA, time_column="ts")
    t = pa.Table.from_pandas(df, schema=FIN_SCHEMA, preserve_index=False)
    db.append("financials", t)
    rep["financials_rows"] = len(df)

    # ---- valuation_snapshot -> h5i (pe/pb/mv) ----
    vd = con.execute("SELECT symbol, price, pe_ttm, pb, total_mv, float_mv, turnover, "
                     "CAST(fetch_time AS TIMESTAMP) ts FROM valuation_snapshot "
                     "WHERE fetch_time IS NOT NULL").fetchdf()
    vschema = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string()),
                         ("price", pa.float64()), ("pe_ttm", pa.float64()), ("pb", pa.float64()),
                         ("total_mv", pa.float64()), ("float_mv", pa.float64()),
                         ("turnover", pa.float64())])
    vd = vd.sort_values(["ts", "symbol"])
    db.create_table("valuation_snapshot", vschema, time_column="ts")
    db.append("valuation_snapshot", pa.Table.from_pandas(vd, schema=vschema, preserve_index=False))
    rep["valuation_rows"] = len(vd)

    # ---- symbols -> parquet ----
    sy = con.execute("SELECT * FROM symbols").fetchdf()
    pq.write_table(pa.Table.from_pandas(sy, preserve_index=False),
                   os.path.join(STATIC, "symbols.parquet"))
    rep["symbols_rows"] = len(sy)

    # ---- 校验 financials 抽样 ----
    h = db.sql("SELECT symbol, CAST(ts AS DATE) d, roe, rev_yoy FROM financials "
               "WHERE symbol='000001' ORDER BY ts DESC LIMIT 3").to_pandas()
    duck = con.execute('SELECT symbol, CAST("报告期" AS DATE) d, "净资产收益率" roe, '
                       '"营业总收入同比增长率" rev_yoy FROM financials WHERE symbol=\'000001\' '
                       'ORDER BY "报告期" DESC LIMIT 3').fetchall()
    rep["check_000001_h5i"] = h.values.tolist()
    rep["check_000001_duck"] = [list(x) for x in duck]
    con.close()
    db.close()
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    json.dump(rep, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
