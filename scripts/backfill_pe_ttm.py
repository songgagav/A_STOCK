# -*- coding: utf-8 -*-
"""回补 valuation.pe_ttm 近期缺口 (backfill_pe_ttm.py).

背景 (2026-09-12 实测):
  2025-08 起 valuation.pe_ttm 覆盖仅 4~5% (每日约 224 只有值), 而 pb 覆盖 100%,
  financials.eps/roe 覆盖 100%. 缺口使历史回测中 governance 的 PE 过滤与
  任何 PE 相关因子失效.

做法:
  用东财个股历史估值接口 ak.stock_value_em(symbol) 逐只拉取历史
  PE(TTM)/市净率/市销率/总市值/流通市值/流通股本, 仅对"目标区间内缺 pe_ttm 的
  (ts, symbol)"追加行(source='em_hist'). h5i 表为 append-only, 因此不回改既有行;
  读端 db._valuation_asof_h5i 的 as-of 查询已在同日多行时优先取 pe_ttm 非空的行.

用法:
  python scripts/backfill_pe_ttm.py --days 400 --dry --limit 10   # 小样本试跑
  python scripts/backfill_pe_ttm.py --days 400                    # 全量(5000+ 只, 约 30~60 分钟)
  python scripts/backfill_pe_ttm.py --days 400 --resume           # 断点续跑
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))  # src/ 布局: 模块位于 src/
os.chdir(_BASE)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

PROGRESS = os.path.join(_BASE, "data", "backfill_pe_ttm_progress.json")
H5I = os.path.join(_BASE, "data", "h5i", "market.db")
PATCH_TABLE = "valuation_pe_patch"   # 补丁表: 主表 append 受"时间单调"约束, 无法回填历史
COLMAP = {
    "数据日期": "ts", "PE(TTM)": "pe_ttm", "市净率": "pb", "市销率": "ps_ttm",
    "总市值": "market_cap", "流通市值": "free_cap", "流通股本": "float_shares",
}
KEEP = ["ts", "symbol", "pe_ttm", "pb", "ps_ttm", "pcf_ncf_ttm", "is_st",
        "market_cap", "free_cap", "float_shares", "source"]


def _sql(q: str):
    from factor_fusion import _sql as f
    return f(q)


def missing_targets(days: int) -> dict[str, set[str]]:
    """返回 {symbol: {缺失日期...}}, 仅统计目标窗口内 pe_ttm 为空的 (ts,symbol)."""
    start = (date.today() - timedelta(days=days)).isoformat()
    q = ("SELECT symbol, CAST(ts AS DATE) d FROM valuation "
         f"WHERE CAST(ts AS DATE) >= DATE '{start}' AND pe_ttm IS NULL")
    df = _sql(q)
    out: dict[str, set[str]] = {}
    if df.empty:
        return out
    for sym, d in zip(df["symbol"].astype(str), df["d"].astype(str)):
        out.setdefault(sym, set()).add(d)
    return out


def fetch_one(symbol: str, wanted: set[str], retry: int = 3, sleep: float = 0.25):
    """拉取单只股票历史估值, 返回目标日期子集 DataFrame(已映射列名)."""
    import akshare as ak
    for i in range(retry):
        try:
            df = ak.stock_value_em(symbol=symbol)
            break
        except Exception as e:  # noqa: BLE001
            if i == retry - 1:
                return None, f"{type(e).__name__}:{str(e)[:80]}"
            time.sleep(sleep * (i + 1))
    if df is None or df.empty:
        return None, "empty"
    df = df.rename(columns=COLMAP)
    if "ts" not in df.columns:
        return None, "no_date_col"
    df["ts"] = df["ts"].astype(str).str[:10]
    df = df[df["ts"].isin(wanted)]
    if df.empty:
        return None, "no_overlap"
    df = df.copy()
    df["symbol"] = str(symbol).zfill(6)
    df["source"] = "em_hist"
    for c in ("pcf_ncf_ttm",):
        df[c] = np.nan
    df["is_st"] = False
    for c in ("pe_ttm", "pb", "ps_ttm", "market_cap", "free_cap", "float_shares"):
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df["pcf_ncf_ttm"] = pd.to_numeric(df["pcf_ncf_ttm"], errors="coerce").astype("float64")
    df["is_st"] = df["is_st"].astype(bool)
    df["ts"] = pd.to_datetime(df["ts"]).astype("datetime64[us]")
    return df[KEEP].sort_values(["ts", "symbol"]), None


def write_rows(df: pd.DataFrame) -> int:
    """写入 parquet 补丁存储 data/pit/pe_patch/.

    为何不用 h5i 表: valuation 主表 append 要求"按时间单调且 min(ts) >= 表 max(ts)",
    历史缺口无法回填; 且 h5i 无公开建表 API(write/append 均要求表已存在)。
    故这里落到独立 parquet 分片, 由 db._pe_patch_asof 在读取时按 as-of 合并(仍无前视)。
    """
    PATCH_DIR = os.path.join(_BASE, "data", "pit", "pe_patch")
    os.makedirs(PATCH_DIR, exist_ok=True)
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"]).astype("datetime64[us]")
    df = df.sort_values(["ts", "symbol"])
    fn = os.path.join(PATCH_DIR, "pe_patch_%s_%s.parquet" % (
        datetime.now().strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6]))
    df.to_parquet(fn, index=False)
    return len(df)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400, help="回补窗口(自然日), 默认 400")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只(调试)")
    ap.add_argument("--dry", action="store_true", help="只计算与校验, 不写库")
    ap.add_argument("--resume", action="store_true", help="断点续跑(跳过已完成 symbol)")
    ap.add_argument("--sleep", type=float, default=0.25, help="请求重试间隔基数秒")
    ap.add_argument("--workers", type=int, default=6, help="并发拉取线程数")
    ap.add_argument("--flush", type=int, default=2000000, help="每批写库行数(默认单批)")
    args = ap.parse_args()

    need = missing_targets(args.days)
    print(f"缺 pe_ttm 的 symbol 数: {len(need)}, 缺口行数: {sum(len(v) for v in need.values()):,}")

    done: set[str] = set()
    if args.resume and os.path.exists(PROGRESS):
        try:
            done = set(json.load(open(PROGRESS, encoding="utf-8")).get("done", []))
            print(f"断点续跑: 已完成 {len(done)} 只")
        except Exception:
            pass

    syms = [s for s in sorted(need) if s not in done]
    if args.limit:
        syms = syms[:args.limit]
    print(f"本次待处理: {len(syms)} 只 (dry={args.dry})")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    frames, t0, n_ok, n_fail = [], time.time(), 0, 0
    fail_samples: list = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(fetch_one, s, need[s], 3, args.sleep): s for s in syms}
        for i, fu in enumerate(as_completed(futs), 1):
            sym = futs[fu]
            try:
                df, err = fu.result()
            except Exception as e:  # noqa: BLE001
                df, err = None, f"{type(e).__name__}:{str(e)[:80]}"
            if df is None:
                n_fail += 1
                if len(fail_samples) < 5:
                    fail_samples.append((sym, err))
            else:
                frames.append(df)
                n_ok += 1
            if i % 300 == 0:
                print(f"  拉取 [{i}/{len(syms)}] ok={n_ok} fail={n_fail} "
                      f"耗时 {time.time()-t0:.0f}s", flush=True)
    n_rows = sum(len(f) for f in frames)
    print(f"拉取完成: ok={n_ok} fail={n_fail} 行={n_rows:,} 耗时 {time.time()-t0:.0f}s")

    if frames and not args.dry:
        all_df = pd.concat(frames, ignore_index=True).sort_values(["ts", "symbol"])
        written = 0
        for s0 in range(0, len(all_df), args.flush):
            written += write_rows(all_df.iloc[s0:s0 + args.flush])
            print(f"  写库 {written:,}/{len(all_df):,} -> {PATCH_TABLE}", flush=True)
        done.update(syms)
        json.dump({"done": sorted(done), "rows": n_rows},
                  open(PROGRESS, "w", encoding="utf-8"))

    print(f"\n完成: 处理 {len(syms)} 只, 成功 {n_ok}, 失败 {n_fail}, 生成 {n_rows:,} 行")
    if fail_samples:
        print("失败样例:", fail_samples)
    if args.dry:
        print("[dry] 未写库")
    else:
        print("提示: 重跑 scripts/check_data_completeness.py 复核, 或按日验证 pe_ttm 覆盖")


if __name__ == "__main__":
    main()
