# -*- coding: utf-8 -*-
"""补录缺失交易日 daily_bars (来源: 新浪全市场收盘快照).

背景: 守护停机/同步缺失会导致某交易日 K 线未入库 (如 2026-09-07).
本工具用新浪全市场快照(收盘后快照=最近交易日, 已验证归属)按 h5i 现有
symbol 白名单过滤后单调 append 到 h5i daily_bars.

用法:
    python scripts/backfill_daily.py --day 2026-09-07
依赖网络(akshare/sina); turnover 列不提供(留 NaN)。

校验: 完成后打印该日行数与库内最大日期.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import factor_fusion as ff  # noqa: E402
from h5i_sync import append_daily_bars  # noqa: E402


def backfill(day: str) -> dict:
    syms = set(ff._sql("SELECT DISTINCT symbol FROM daily_bars").iloc[:, 0]
               .astype(str).str.zfill(6))
    import akshare as ak
    spot = ak.stock_zh_a_spot()
    rows = []
    for _, r in spot.iterrows():
        code = str(r["代码"])
        s6 = code[2:].zfill(6) if code[:2] in ("sh", "sz", "bj") else code
        if s6 not in syms:
            continue
        try:
            close = float(r["最新价"])
        except (TypeError, ValueError):
            continue
        if not np.isfinite(close) or close <= 0:
            continue
        rows.append({
            "symbol": s6, "date": day,
            "open": float(r["今开"]) if np.isfinite(r["今开"]) else close,
            "high": float(r["最高"]) if np.isfinite(r["最高"]) else close,
            "low": float(r["最低"]) if np.isfinite(r["最低"]) else close,
            "close": close,
            "volume": float(r["成交量"]) if np.isfinite(r["成交量"]) else np.nan,
            "amount": float(r["成交额"]) if np.isfinite(r["成交额"]) else np.nan,
            "change_pct": float(r["涨跌幅"]) if np.isfinite(r["涨跌幅"]) else np.nan,
        })
    df = pd.DataFrame(rows)
    res = append_daily_bars(df)
    n = ff._sql(f"SELECT COUNT(*) AS n FROM daily_bars "
                f"WHERE CAST(ts AS DATE) = DATE '{day}'").iloc[0, 0]
    mx = ff._sql("SELECT MAX(CAST(ts AS DATE)) AS m FROM daily_bars").iloc[0, 0]
    return {"append": {k: res.get(k) for k in
                       ("ok", "appended", "skipped_rows", "dates", "error")},
            "bars_on_day": int(n), "max_ts": str(mx)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="要补录的交易日 YYYY-MM-DD")
    args = ap.parse_args()
    import json
    print(json.dumps(backfill(args.day), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
