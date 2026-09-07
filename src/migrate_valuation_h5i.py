# -*- coding: utf-8 -*-
"""流式迁移 valuation(日频 14.0M) 至 h5i (ts=trade_date)"""
import os
import duckdb, h5i_db, pyarrow as pa, pyarrow.compute as pc, time

DD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "legacy_stockdb.duckdb")
H5I = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "h5i", "market.db")
SCH = pa.schema([
    ("ts", pa.timestamp("us")), ("symbol", pa.string()),
    ("pe_ttm", pa.float64()), ("pb", pa.float64()), ("ps_ttm", pa.float64()),
    ("pcf_ncf_ttm", pa.float64()), ("is_st", pa.bool_()),
    ("market_cap", pa.float64()), ("free_cap", pa.float64()),
    ("float_shares", pa.float64()), ("source", pa.string())])

con = duckdb.connect(DD, read_only=True)
db = h5i_db.Database(H5I)
db.create_table("valuation", SCH, time_column="ts")

q = "SELECT symbol, trade_date, pe_ttm, pb, ps_ttm, pcf_ncf_ttm, is_st, " \
    "market_cap, free_cap, float_shares, source FROM valuation ORDER BY trade_date, symbol"
cur = con.cursor().execute(q)
n = 0
t0 = time.time()
while True:
    df = cur.fetch_df_chunk(2_000_000)
    if df is None or len(df) == 0:
        break
    df["ts"] = df["trade_date"].astype("datetime64[us]")
    df = df.drop(columns=["trade_date"])
    df["source"] = df["source"].fillna("calculated").astype(str)
    df["is_st"] = df["is_st"].fillna(False)
    t = pa.Table.from_pandas(df, schema=SCH, preserve_index=False)
    db.append("valuation", t)
    n += len(df)
    print("appended", n, "elapse", round(time.time() - t0), flush=True)
con.close(); db.close()
print("valuation migrated total", n)
