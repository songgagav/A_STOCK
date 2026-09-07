# -*- coding: utf-8 -*-
"""重建 financials 数据底座 (CNEquity L1: 同花顺按报告期财务摘要)

- 并发拉全A沪深 active 标的(0/3/6 段) 财务摘要
- 数值标准化: 货币(亿/万->元) / 百分比(->小数) / 'False'/'-' -> NULL
- 按 (symbol, 报告期) upsert, 追加 source/ingested_at 溯源字段
"""
import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cn_lake_feed import _scale_cny

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "legacy_stockdb.duckdb")
WORKERS = 6
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "financials_rebuild.json")
PCT_COLS = {"净利润同比增长率", "扣非净利润同比增长率", "营业总收入同比增长率", "销售净利率",
            "净资产收益率", "净资产收益率-摊薄", "资产负债率", "销售毛利率"}
CNY_COLS = {"净利润", "扣非净利润", "营业总收入"}
RATE_COLS = {"营业周期", "应收账款周转天数", "存货周转天数", "流动比率", "速动比率",
             "保守速动比率", "产权比率", "存货周转率"}


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s in ("", "-", "False", "None", "nan", "True"):
        return None
    if s.endswith("%"):
        try:
            return round(float(s[:-1]) / 100.0, 6)
        except Exception:
            return None
    return _scale_cny(s)


def fetch_one(sym: str) -> pd.DataFrame | None:
    import akshare as ak
    last = None
    for k in range(3):
        try:
            df = ak.stock_financial_abstract_ths(symbol=sym, indicator="按报告期")
            if df is None or df.empty:
                return None
            return df
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (k + 1))
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help=">0 时仅跑前N只(试跑)")
    ap.add_argument("--workers", type=int, default=WORKERS, help="并发数")
    args = ap.parse_args()
    con = duckdb.connect(DB)
    try:
        syms = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM symbols WHERE is_active = true "
            "AND substr(symbol,1,1) IN ('0','3','6') ORDER BY symbol").fetchall()]
        # 断点续跑: 仅跳过"含最新报告期且带本次来源"的完整 symbol; 其余(含部分写入/旧行)重取
        old = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM financials").fetchall()}
        done = {r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM financials "
            "WHERE source='ak_stock_financial_abstract_ths' AND \"报告期\"='2026-06-30'").fetchall()}
        base = set(syms) | {s for s in old if not s.startswith(("11", "12"))}
        todo = sorted(s for s in base if s not in done)
        if args.limit > 0:
            todo = todo[:args.limit]
        print(f"待处理 {len(todo)} 只(完整已完成 {len(done)} 只)", flush=True)

        frames, fails = [], []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(fetch_one, s): s for s in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                s = futs[fut]
                try:
                    df = fut.result(timeout=40)
                    if df is not None and not df.empty:
                        d2 = df.copy()
                        d2.insert(0, "symbol", s)
                        frames.append(d2)
                    else:
                        fails.append(s)
                except Exception:
                    fails.append(s)
                if i % 300 == 0 or i == len(futs):
                    print(f"  {i}/{len(todo)} elapse={time.time() - t0:.0f}s fail={len(fails)}",
                          flush=True)
        if not frames:
            print("无数据"); return
        all_df = pd.concat(frames, ignore_index=True)

        # 规范化数值
        extra = {"销售毛利率", "存货周转率", "存货周转天数", "营业总收入"}
        for c in all_df.columns:
            if c in ("symbol", "报告期"):
                continue
            if c in CNY_COLS:
                all_df[c] = all_df[c].apply(_scale_cny)
            elif c in PCT_COLS or c in RATE_COLS:
                all_df[c] = all_df[c].apply(_num)
            else:  # 每股类 & 未知 -> float 或 None
                all_df[c] = all_df[c].apply(_num)
        # 补 financials 目标表可能缺失的列
        cols = [r[1] for r in con.execute("PRAGMA table_info('financials')").fetchall()]
        target = [c for c in cols if c not in ("source", "ingested_at")]
        for c in target:
            if c not in all_df.columns:
                all_df[c] = None
        all_df = all_df[target]
        all_df = all_df.where(pd.notnull(all_df), None)
        con.execute("ALTER TABLE financials ADD COLUMN IF NOT EXISTS source VARCHAR")
        con.execute("ALTER TABLE financials ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMP")
        now = dt.datetime.now().isoformat(sep=" ")
        all_df["报告期"] = all_df["报告期"].astype(str).str[:10]
        all_df["source"] = "ak_stock_financial_abstract_ths"
        all_df["ingested_at"] = now
        # 批量替换: 符号级删除 + 全量插入(避免逐行DELETE的O(n^2))
        con.register("fin_new", all_df)
        con.execute("DELETE FROM financials WHERE symbol IN (SELECT DISTINCT symbol FROM fin_new)")
        con.execute(f"INSERT INTO financials SELECT * FROM fin_new")
        con.unregister("fin_new")
        n = len(all_df)
        res = con.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(\"报告期\") FROM financials").fetchone()
        print(f"写入 {n} 行; 库内 total={res[0]} symbols={res[1]} 最新报告期={res[2]}", flush=True)
        json.dump({"fetched_symbols": len(todo) - len(fails), "failed": fails[:200],
                   "rows": n, "total": res[0], "symbols": res[1],
                   "latest_report": str(res[2]), "source": "ak_stock_financial_abstract_ths"},
                  open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
