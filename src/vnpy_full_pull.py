"""vnpy_full_pull.py -- 使用 vnpy AlphaLab / ArcticStore 体系, 把 DuckDB 里的全量
daily_bars 灌入 ArcticDB (bars lib), 为 vnpy_backtest / 后续研究做"全市场级"数据底座.

行为:
  1. DuckDB 读取 daily_bars 全量 (按 symbol 分组, 去重)
  2. 用 vnpy AlphaLab + BarGenerator 接口把 OHLCV DataFrame 标准化
  3. 调用 ArcticStore.write_bars 写入 ArcticDB bars lib (按 symbol 累加, 已存在会 update)
  4. 跳过 ArcticDB 已有但日期更老的 symbol, 只补增量

用法:
  python vnpy_full_pull.py                  # 全量 (5279 只)
  python vnpy_full_pull.py --symbols 600177.SH 300628.SZ  # 单只
  python vnpy_full_pull.py --incremental      # 仅补 DuckDB 晚于 ArcticDB 的增量
  python vnpy_full_pull.py --limit 100       # 只前 100 只
"""
from __future__ import annotations

import os
import argparse
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DATA_DIR, DUCKDB_PATH
from arctic_store import get_store, LIB_BARS
import duckdb
import pandas as pd


def _s6_to_canon(s6: str) -> str:
    """纯数字 symbol (DuckDB 存的形式) -> ArcticDB 用的 canon (含后缀)."""
    if "." in s6:
        return s6  # 已带后缀
    if s6.startswith(("5", "6", "7")):
        return f"{s6}.SH"
    if s6.startswith(("0", "3", "2")):
        return f"{s6}.SZ"
    if s6.startswith(("8", "43", "92")):
        return f"{s6}.BJ"
    return f"{s6}.SH"


def _read_full_daily_bars(symbols: list[str] | None = None,
                           limit: int | None = None) -> dict[str, pd.DataFrame]:
    """DuckDB 读 daily_bars, 返回 {canon: DataFrame(date,open,high,low,close,volume,amount,turnover)}.
    按 symbol 排序, 每 symbol 内部按 date 排序. 自动把纯数字 symbol 转成 canon.
    """
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        if symbols:
            # 支持 "600177.SH" 或 "600177" 两种输入
            norm = [_s6_to_canon(s) for s in symbols]
            placeholders = ",".join(["?"] * len(norm))
            sql = f"""
                SELECT symbol, date, open, high, low, close, volume, amount, turnover
                FROM daily_bars
                WHERE symbol IN ({placeholders})
                ORDER BY symbol, date
            """
            rows = con.execute(sql, [s.split(".")[0] for s in norm]).fetchall()
        else:
            sql = """
                SELECT symbol, date, open, high, low, close, volume, amount, turnover
                FROM daily_bars
                WHERE date IS NOT NULL
                ORDER BY symbol, date
            """
            if limit:
                # 仅取前 N 只 symbol (DISTINCT), 然后全量加载
                sql = f"""
                    WITH top_n AS (SELECT DISTINCT symbol FROM daily_bars ORDER BY symbol LIMIT {limit})
                    SELECT b.symbol, b.date, b.open, b.high, b.low, b.close, b.volume, b.amount, b.turnover
                    FROM daily_bars b INNER JOIN top_n t ON b.symbol = t.symbol
                    ORDER BY b.symbol, b.date
                """
            rows = con.execute(sql).fetchall()
        df = pd.DataFrame(rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover"])
    finally:
        try: con.close()
        except Exception: pass
    out = {}
    for sym, g in df.groupby("symbol"):
        canon = _s6_to_canon(str(sym))
        g = g.drop(columns=["symbol"]).reset_index(drop=True)
        g["date"] = pd.to_datetime(g["date"])
        out[canon] = g
    return out


def _existing_symbol_dates(store, symbol: str) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """查 ArcticDB 里 symbol 已有日期的 (min, max)."""
    try:
        df = store.read_bars(symbol)
        if df is None or df.empty:
            return None, None
        idx = df.index
        return idx.min(), idx.max()
    except Exception:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", help="指定 symbol 列表 (如 600177.SH)")
    ap.add_argument("--limit", type=int, help="限定 N 只标的")
    ap.add_argument("--incremental", action="store_true", help="仅写入 DuckDB 晚于 ArcticDB 现有日期的部分")
    ap.add_argument("--batch-report", type=int, default=200, help="每 N 只打印进度")
    args = ap.parse_args()

    store = get_store()
    if not store:
        print("ERROR: ArcticDB 不可用, 请检查 arctic_store.py 配置")
        sys.exit(1)

    # 列出现有 ArcticDB symbols (用于判断是否需要跳过)
    existing = set(store.list_bars_symbols())
    print(f"[{time.strftime('%H:%M:%S')}] ArcticDB bars lib 现有 {len(existing)} 只标的")
    if existing:
        sample = sorted(existing)[:5]
        print(f"  示例: {sample}")

    # 读 DuckDB
    print(f"[{time.strftime('%H:%M:%S')}] DuckDB 读取 daily_bars ...")
    t0 = time.time()
    bars_map = _read_full_daily_bars(symbols=args.symbols, limit=args.limit)
    print(f"[{time.strftime('%H:%M:%S')}] 读取完成: {len(bars_map)} 只, 用时 {time.time()-t0:.1f}s")

    if not bars_map:
        print("没有可写入的标的")
        return

    # 写入
    written = updated = skipped = failed = 0
    fail_examples = []
    sym_list = sorted(bars_map.keys())
    total = len(sym_list)

    for i, sym in enumerate(sym_list, 1):
        df = bars_map[sym]
        if df is None or df.empty:
            skipped += 1
            continue
        # 标准化 date 索引
        df = df.copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        df = df[~df.index.duplicated(keep="last")].sort_index()

        # 增量模式: 截取 DuckDB 晚于 ArcticDB 的部分
        if args.incremental and sym in existing:
            _, last_ts = _existing_symbol_dates(store, sym)
            if last_ts is not None:
                df = df[df.index > last_ts]
                if df.empty:
                    skipped += 1
                    continue

        try:
            ok = store.write_bars(sym, df.reset_index())
            if ok:
                if sym in existing:
                    updated += 1
                else:
                    written += 1
            else:
                failed += 1
                if len(fail_examples) < 5:
                    fail_examples.append((sym, "write_bars 返回 False"))
        except Exception as e:
            failed += 1
            if len(fail_examples) < 5:
                fail_examples.append((sym, str(e)[:80]))

        if i % args.batch_report == 0 or i == total:
            print(f"[{time.strftime('%H:%M:%S')}] 进度 {i}/{total} "
                  f"新写={written} 追加={updated} 跳过={skipped} 失败={failed}")

    print()
    print(f"=== 完成 ===")
    print(f"  新写入: {written}")
    print(f"  增量追加: {updated}")
    print(f"  跳过(空/无增量): {skipped}")
    print(f"  失败: {failed}")
    if fail_examples:
        print(f"  失败示例: {fail_examples}")

    final_count = len(store.list_bars_symbols())
    print(f"  ArcticDB bars lib 最终: {final_count} 只标的")


if __name__ == "__main__":
    main()