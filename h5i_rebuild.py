# -*- coding: utf-8 -*-
"""整体重建 h5i 库(daily_bars + financials + 全字段 valuation_snapshot + symbols parquet)"""
import os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

H5I_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "h5i")
H5I = os.path.join(H5I_DIR, "market.db")

if __name__ == "__main__":
    if os.path.exists(H5I):
        shutil.rmtree(H5I, ignore_errors=True) if os.path.isdir(H5I) else os.remove(H5I)
    # 1) daily
    import h5i_ingest
    db = h5i_ingest.ingest()
    # 2) financials + valuation(full) + symbols
    import duckdb, h5i_db
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pandas as pd
    con = duckdb.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "legacy_stockdb.duckdb"), read_only=True)
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
    fdf = con.execute("SELECT * FROM financials").fetchdf().rename(columns=FIN_MAP)
    fdf["ts"] = pd.to_datetime(fdf["报告期"], errors="coerce")
    fdf = fdf.dropna(subset=["ts"]).sort_values(["ts", "symbol"])
    fcols = ["ts", "symbol"] + list(FIN_MAP.values()) + ["source"]
    fdf = fdf[fcols]
    for c in FIN_MAP.values():
        fdf[c] = pd.to_numeric(fdf[c], errors="coerce")
    fdf["source"] = fdf["source"].fillna("").astype(str)
    fsch = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string())] +
                     [(v, pa.float64()) for v in FIN_MAP.values()] + [("source", pa.string())])
    db.create_table("financials", fsch, time_column="ts")
    db.append("financials", pa.Table.from_pandas(fdf, schema=fsch, preserve_index=False))

    vdf = con.execute("SELECT symbol, name, price, pe_ttm, pb, total_mv, float_mv, amount, "
                      "float_shares, total_shares, is_st, turnover, "
                      "CAST(fetch_time AS TIMESTAMP) ts FROM valuation_snapshot WHERE fetch_time IS NOT NULL"
                      ).fetchdf().sort_values(["ts", "symbol"])
    vsch = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string()),
                      ("name", pa.string()), ("price", pa.float64()), ("pe_ttm", pa.float64()),
                      ("pb", pa.float64()), ("total_mv", pa.float64()), ("float_mv", pa.float64()),
                      ("amount", pa.float64()), ("float_shares", pa.float64()),
                      ("total_shares", pa.float64()), ("is_st", pa.string()),
                      ("turnover", pa.float64())])
    for c in ("name", "is_st"):
        vdf[c] = vdf[c].fillna("").astype(str)
    db.create_table("valuation_snapshot", vsch, time_column="ts")
    db.append("valuation_snapshot", pa.Table.from_pandas(vdf, schema=vsch, preserve_index=False))

    sy = con.execute("SELECT * FROM symbols").fetchdf()
    pq.write_table(pa.Table.from_pandas(sy, preserve_index=False),
                   os.path.join(H5I_DIR, "static", "symbols.parquet"))
    con.close()
    db.close()
    print("h5i rebuilt: daily/financials/valuation/symbols OK")
