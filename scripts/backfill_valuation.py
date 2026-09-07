# -*- coding: utf-8 -*-
"""补录 valuation_snapshot / valuation (来源: 东财全市场快照).

东财 spot_em 提供 最新价/pe/pb/总市值/流通市值/成交额/换手率/涨跌幅,
映射到 h5i.valuation_snapshot(ts=fetch_time=当日15:00); 再由
valuation_backfill.merge_snapshot_to_valuation 并入逐日 valuation.

用法: python scripts/backfill_valuation.py --day 2026-09-07
依赖网络(东财); 非交易时段接口可能不稳, 失败可由 data_update_daemon 窗口重试.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)


def fetch_spot(retry: int = 3, wait: float = 8.0) -> pd.DataFrame:
    import akshare as ak
    last = None
    for i in range(retry):
        try:
            return ak.stock_zh_a_spot_em()
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  spot_em try{i} fail: {type(e).__name__} {str(e)[-80:]}")
            time.sleep(wait)
    raise RuntimeError(f"东财 spot_em 不可用: {last}")


def build_snapshot(day: str, spot: pd.DataFrame) -> pd.DataFrame:
    ts = datetime.fromisoformat(f"{day} 15:00:00")
    def col(*names):
        for n in names:
            if n in spot.columns:
                return spot[n]
        return pd.Series(np.nan, index=spot.index)

    code = spot["代码"].astype(str)
    df = pd.DataFrame({
        "symbol": code.str[-6:],
        "name": spot["名称"].astype(str),
        "price": pd.to_numeric(col("最新价"), errors="coerce"),
        "pe_ttm": pd.to_numeric(col("市盈率-动态", "市盈率"), errors="coerce"),
        "pb": pd.to_numeric(col("市净率"), errors="coerce"),
        "total_mv": pd.to_numeric(col("总市值"), errors="coerce"),
        "float_mv": pd.to_numeric(col("流通市值"), errors="coerce"),
        "amount": pd.to_numeric(col("成交额"), errors="coerce"),
        "turnover": pd.to_numeric(col("换手率"), errors="coerce"),
    })
    df["ts"] = ts
    # 过滤: 需要价格与市值
    df = df[df["price"].notna() & (df["price"] > 0) & (df["float_mv"].notna())]
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--retry", type=int, default=3)
    args = ap.parse_args()
    day = args.day

    print(f"[{day}] 拉取东财全市场快照 ...")
    spot = fetch_spot(retry=args.retry)
    print(f"  spot rows: {len(spot)}")

    df = build_snapshot(day, spot)
    print(f"  valid rows: {len(df)}")
    if df.empty:
        print("无有效行, 退出")
        sys.exit(1)

    import pyarrow as pa
    import h5i_db
    H5I = os.path.join(_BASE, "data", "h5i", "market.db")
    db = h5i_db.Database(H5I)

    # 与现有 valuation_snapshot 表列对齐
    keep = ["ts", "symbol", "name", "price", "pe_ttm", "pb", "total_mv",
            "float_mv", "amount", "float_shares", "total_shares", "is_st", "turnover"]
    for c in ("float_shares", "total_shares"):
        df[c] = np.nan
    df["is_st"] = ""
    df = df[keep].sort_values(["ts", "symbol"])

    schema = db._get_table_schema("valuation_snapshot") if hasattr(db, "_get_table_schema") else None
    if schema is None:
        print("无法读取 valuation_snapshot schema, 退出")
        sys.exit(2)
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    db.append("valuation_snapshot", table)
    print(f"  append valuation_snapshot rows: {len(df)}")

    # 并入逐日 valuation (当日行数>=4500 才合并, 与 valuation_backfill 一致)
    from valuation_backfill import merge_snapshot_to_valuation
    rep = merge_snapshot_to_valuation(day=day)
    print("  merge to valuation:", rep if not isinstance(rep, dict) else
          {k: rep[k] for k in list(rep)[:6]})


if __name__ == "__main__":
    main()
