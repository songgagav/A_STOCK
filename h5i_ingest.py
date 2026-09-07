# -*- coding: utf-8 -*-
"""h5i-db 导入 daily_bars + DuckDB 奇偶校验/压测
新架构: ArcticDB(存储) + h5i-db(分析/回测). 本步把 DuckDB 权威 daily_bars
导入 h5i 版本化库并做 SQL 一致性校验.
"""
import json
import os
import time

import duckdb
import h5i_db
import pyarrow as pa

DB_DUCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "legacy_stockdb.duckdb")
H5I_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "h5i")
H5I_PATH = os.path.join(H5I_DIR, "market.db")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "h5i_ingest_report.json")
CHUNK_ROWS = 3_000_000

SCHEMA = pa.schema([
    ("ts", pa.timestamp("us")),
    ("symbol", pa.string()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("amount", pa.float64()),
    ("change_pct", pa.float64()),
    ("turnover", pa.float64()),
])

SQL_FULL = ("SELECT date::TIMESTAMP AS ts, symbol, open, high, low, close, volume, "
            "amount, change_pct, turnover FROM daily_bars ORDER BY ts, symbol")


def ingest():
    os.makedirs(H5I_DIR, exist_ok=True)
    if os.path.exists(H5I_PATH):
        if os.path.isdir(H5I_PATH):
            import shutil
            shutil.rmtree(H5I_PATH, ignore_errors=True)
        else:
            os.remove(H5I_PATH)
    db = h5i_db.Database(H5I_PATH, create=True)
    db.create_table("daily_bars", SCHEMA, time_column="ts")
    con = duckdb.connect(DB_DUCK, read_only=True)
    total = 0
    t0 = time.time()
    # 流式取 Arrow 批次
    reader = con.execute(SQL_FULL).fetch_record_batch(4096)
    batches, n = [], 0
    while True:
        try:
            b = reader.read_next_batch()
        except StopIteration:
            break
        batches.append(b)
        n += b.num_rows
        if n >= CHUNK_ROWS:
            db.append("daily_bars", pa.Table.from_batches(batches))
            total += n
            print(f"  appended {total} rows elapse={time.time() - t0:.0f}s", flush=True)
            batches, n = [], 0
    if batches:
        db.append("daily_bars", pa.Table.from_batches(batches))
        total += n
    con.close()
    print("ingest total:", total, "elapse", round(time.time() - t0, 1), flush=True)
    return db


def parity(db):
    con = duckdb.connect(DB_DUCK, read_only=True)
    report = {}
    # 1) 行数与区间
    for tag, fn in (("duckdb", lambda: con), ("h5i", lambda: db)):
        pass
    r = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM daily_bars").fetchone()
    report["duckdb"] = {"rows": r[0], "min": str(r[1]), "max": str(r[2])}
    r2 = db.sql("SELECT COUNT(*), CAST(MIN(ts) AS VARCHAR), CAST(MAX(ts) AS VARCHAR) FROM daily_bars").to_pandas()
    report["h5i"] = {"rows": int(r2.iloc[0, 0]), "min": r2.iloc[0, 1], "max": r2.iloc[0, 2]}
    # 2) 抽样奇偶: 随机20只近端行情一致(close/volume 计数与和)
    syms = [x[0] for x in con.execute(
        "SELECT DISTINCT symbol FROM daily_bars WHERE date='2026-09-04' ORDER BY random() LIMIT 20").fetchall()]
    place = ",".join(["?"] * len(syms))
    d = con.execute(f"SELECT symbol, COUNT(*), SUM(close), SUM(volume) FROM daily_bars "
                    f"WHERE date>='2026-01-01' AND symbol IN ({place}) GROUP BY 1 ORDER BY 1", syms).fetchall()
    dd = {x[0]: (x[1], round(x[2], 4), round(x[3], 4)) for x in d}
    # h5i 端
    ph = ",".join(["'%s'" % s for s in syms])
    h = db.sql(f"SELECT symbol, COUNT(*) AS n, SUM(close) AS sc, SUM(volume) AS sv FROM daily_bars "
               f"WHERE ts >= TIMESTAMP '2026-01-01' AND symbol IN ({ph}) GROUP BY symbol ORDER BY symbol").to_pandas()
    hh = {x["symbol"]: (int(x["n"]), round(float(x["sc"]), 4), round(float(x["sv"]), 4))
          for _, x in h.iterrows()}
    eq = all(hh.get(s) == dd.get(s) for s in syms)
    report["sample_symbols_n"] = len(syms)
    report["sample_parity_ok"] = eq
    # 3) 压测: 近1年 symbol 过滤 + 全库 count(热路径各跑2次取第2次)
    def bench(fn):
        fn(); t = time.time(); fn(); return round(time.time() - t, 3)
    qd = lambda: con.execute("SELECT COUNT(*) FROM daily_bars WHERE date>='2025-09-05'").fetchone()
    qh = lambda: db.sql("SELECT COUNT(*) FROM daily_bars WHERE ts >= TIMESTAMP '2025-09-05'").to_pandas()
    report["duckdb_filter_ms"] = bench(qd) * 1000
    report["h5i_filter_ms"] = bench(qh) * 1000
    con.close()
    return report


def main():
    db = ingest()
    rep = parity(db)
    rep["db"] = H5I_PATH
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    json.dump(rep, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("saved", OUT)
    db.close()


if __name__ == "__main__":
    main()
