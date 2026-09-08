# -*- coding: utf-8 -*-
"""腾讯行情快照估值源 (替代东财 spot_em, 2026-09-08 起东财全市场接口被反爬断连).

qt.gtimg.cn 批量行情 v_ 字段(以 ~ 分割, 单位标注):
  [3]现价(元)  [37]成交额(万元)  [38]换手率(%)  [39]市盈率TTM
  [44]流通市值(亿) [45]总市值(亿) [46]市净率  [53]市盈率(动)

与 valuation_snapshot 列对齐 (原东财口径):
  price=现价元 / pe_ttm=[53]市盈率-动(对齐东财'市盈率-动态'历史口径) / pb=市净率
  total_mv=[45]总市值(亿) / float_mv=[44]流通市值(亿) / amount=[37]万*1e4->元
  turnover=[38]% / float_shares,total_shares,is_st 置空(同原实现)

用法:
  python scripts/tencent_valuation.py --day 2026-09-08 --dry   # 只拉取解析, 不写库
  python scripts/tencent_valuation.py --day 2026-09-08          # 拉取+写 snapshot+并入 valuation
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_MIN_ROWS = 4500  # merge 到 valuation 的门槛, 与 valuation_backfill 一致


def _prefix_map() -> pd.DataFrame:
    """universe 代码全集 -> (qt 前缀) 映射."""
    from factor_fusion import load_symbols
    df = load_symbols()[["symbol", "market"]].copy()
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df = df[df["market"].isin(("sh", "sz", "bj"))]
    return df


def fetch_qt(codes: list[str], batch: int = 60, wait: float = 0.25,
             retry: int = 3) -> pd.DataFrame:
    """按批请求 qt.gtimg.cn, 返回 88 字段原始拆分表(index=qt code)."""
    rows: dict[str, list[str]] = {}
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        q = ",".join(chunk)
        last = None
        for _ in range(retry):
            try:
                r = requests.get("http://qt.gtimg.cn/q=" + q, timeout=10,
                                 headers=_UA)
                r.encoding = "gbk"
                for line in r.text.split(";"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    parts = val.strip('"').split("~")
                    if len(parts) >= 60:          # 无效/异常响应字段不足则丢弃
                        rows[key.strip()] = parts[:88]
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(wait * 4)
        else:
            print(f"  qt batch@{i} fail: {type(last).__name__} {str(last)[-80:]}")
        time.sleep(wait)
    return pd.DataFrame.from_dict(rows, orient="index")


def build_snapshot(day: str, df: pd.DataFrame) -> pd.DataFrame:
    """原始拆分表 -> valuation_snapshot 列(未含 ts/股本空列)."""
    ts = datetime.fromisoformat(f"{day} 15:00:00")
    out = pd.DataFrame({
        "symbol": [str(c)[-6:] for c in df.index],
        "name": df.iloc[:, 1].astype(str),
        "price": pd.to_numeric(df.iloc[:, 3], errors="coerce"),
        "pe_ttm": pd.to_numeric(df.iloc[:, 53], errors="coerce"),   # 市盈率(动)
        "pb": pd.to_numeric(df.iloc[:, 46], errors="coerce"),
        "total_mv": pd.to_numeric(df.iloc[:, 45], errors="coerce"),  # 亿元
        "float_mv": pd.to_numeric(df.iloc[:, 44], errors="coerce"),  # 亿元
        "amount": pd.to_numeric(df.iloc[:, 37], errors="coerce") * 1e4,  # 万->元
        "turnover": pd.to_numeric(df.iloc[:, 38], errors="coerce"),
    })
    out["ts"] = ts
    out = out[(out["price"].notna()) & (out["price"] > 0)
              & (out["float_mv"].notna()) & (out["float_mv"] > 0)]
    return out


def fetch_snapshot(day: str) -> pd.DataFrame:
    pm = _prefix_map()
    codes = [(pm.iloc[i]["market"] + pm.iloc[i]["symbol"])
             for i in range(len(pm))]
    print(f"[{day}] 腾讯全市场快照: {len(codes)} 只 (qt.gtimg.cn)")
    raw = fetch_qt(codes)
    df = build_snapshot(day, raw)
    print(f"  raw codes: {len(raw)}  valid rows: {len(df)}")
    return df


def write_snapshot(day: str, df: pd.DataFrame) -> dict:
    """append valuation_snapshot + 并入逐日 valuation."""
    import pyarrow as pa
    import h5i_db
    H5I = os.path.join(_BASE, "data", "h5i", "market.db")
    db = h5i_db.Database(H5I)

    keep = ["ts", "symbol", "name", "price", "pe_ttm", "pb", "total_mv",
            "float_mv", "amount", "float_shares", "total_shares", "is_st",
            "turnover"]
    for c in ("float_shares", "total_shares"):
        df[c] = np.nan
    df["is_st"] = ""
    df = df[keep].sort_values(["ts", "symbol"])

    schema = db._get_table_schema("valuation_snapshot")
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    db.append("valuation_snapshot", table)
    print(f"  append valuation_snapshot rows: {len(df)}")

    from valuation_backfill import merge_snapshot_to_valuation
    rep = merge_snapshot_to_valuation(day=day)
    print("  merge to valuation:", (rep if not isinstance(rep, dict)
                                    else {k: rep[k] for k in list(rep)[:6]}))
    return {"snapshot_rows": len(df), "merge": rep}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="估值目标交易日 YYYY-MM-DD")
    ap.add_argument("--dry", action="store_true", help="只拉取解析不写库")
    args = ap.parse_args()

    df = fetch_snapshot(args.day)
    if df.empty:
        print("无有效行, 退出")
        sys.exit(1)
    if len(df) < _MIN_ROWS:
        print(f"有效行 {len(df)} < 门槛 {_MIN_ROWS}, 不写库(避免污染 valuation)")
        sys.exit(1)
    # 样例核对
    probe = df[df["symbol"] == "600519"]
    if len(probe):
        p = probe.iloc[0]
        print("  茅台校验: price=%.2f pe_ttm=%.2f pb=%.2f total_mv=%.0f亿 "
              "amount=%.0f元 turnover=%.2f%%" %
              (p["price"], p["pe_ttm"], p["pb"], p["total_mv"],
               p["amount"], p["turnover"]))
    if args.dry:
        print("[dry] 验证通过, 未写库")
        return
    write_snapshot(args.day, df)


if __name__ == "__main__":
    main()
