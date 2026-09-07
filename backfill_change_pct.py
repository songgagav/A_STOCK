# ============================================================
# backfill_change_pct.py -- 一次性批量回填 DuckDB daily_bars 中 change_pct IS NULL 的行
#
# 触发场景:
#   1) AKShare / free_stockdb 写入时首日行 prev_close 缺失 -> change_pct=NULL
#   2) 历史同步断层 / 老数据导入时 change_pct 字段缺失
#
# 修复策略:
#   a) 首日行: 查该 symbol 在 DuckDB 中 <该日 的最大日期 close 作 prev_close.
#      若查到 -> 回填; 查不到 (上市前) -> 保持 NULL.
#   b) 非首日但 prev 为 NULL: 查上一交易日 (DuckDB LAG) close 回填.
#
# 用法:
#   python backfill_change_pct.py [--dry-run] [--limit N]
# ============================================================

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import duckdb

from config import DUCKDB_PATH


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [backfill_change_pct] {msg}", flush=True)


def count_nulls(con) -> dict:
    """统计当前 change_pct IS NULL 行数."""
    total = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
    nan = con.execute("SELECT COUNT(*) FROM daily_bars WHERE change_pct IS NULL").fetchone()[0]
    return {"total": total, "null": nan}


def backfill_non_first(con, dry_run: bool = False) -> int:
    """非首日 NULL: 用 LAG(close) OVER (PARTITION BY symbol ORDER BY date) 回填.
    条件: 前一行在 DuckDB 存在 (即 LAG 不为 NULL) 且 close > 0.
    """
    sql_select_nan = """
        WITH ranked AS (
          SELECT symbol, date, close, change_pct,
                 LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
          FROM daily_bars
        )
        SELECT COUNT(*) FROM ranked
        WHERE change_pct IS NULL
          AND prev_close IS NOT NULL AND prev_close > 0
          AND close IS NOT NULL AND close > 0
    """
    fixable = con.execute(sql_select_nan).fetchone()[0]
    if fixable == 0:
        _log("非首日 NULL: 无可回填行")
        return 0
    if dry_run:
        _log(f"非首日 NULL: 可回填 {fixable} 行 (dry_run, 未执行)")
        return 0
    # 一次性 UPDATE 用 CTE 算出 prev_close
    sql_update = """
        UPDATE daily_bars
        SET change_pct = ROUND(
            (daily_bars.close - ranked.prev_close) / ranked.prev_close * 100, 4)
        FROM (
            SELECT symbol, date,
                   LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
            FROM daily_bars
        ) ranked
        WHERE daily_bars.symbol = ranked.symbol
          AND daily_bars.date = ranked.date
          AND daily_bars.change_pct IS NULL
          AND ranked.prev_close IS NOT NULL
          AND ranked.prev_close > 0
          AND daily_bars.close IS NOT NULL
          AND daily_bars.close > 0
    """
    _log(f"非首日 NULL: 正在 UPDATE {fixable} 行 ...")
    con.execute(sql_update)
    con.commit()
    _log(f"非首日 NULL: 完成")
    return fixable


def backfill_first(con, dry_run: bool = False) -> int:
    """首日 NULL: 用子查询找每只 symbol 在该日之前的最大交易日 close 回填."""
    # 先统计可回填的首日行
    sql_count = """
        WITH firsts AS (
            SELECT symbol, MIN(date) AS first_date
            FROM daily_bars
            GROUP BY symbol
        )
        SELECT COUNT(*) FROM daily_bars d
        INNER JOIN firsts f ON d.symbol = f.symbol AND d.date = f.first_date
        WHERE d.change_pct IS NULL
          AND EXISTS (
              SELECT 1 FROM daily_bars p
              WHERE p.symbol = d.symbol AND p.date < d.date
          )
    """
    fixable = con.execute(sql_count).fetchone()[0]
    if fixable == 0:
        _log("首日 NULL: 无可回填行")
        return 0
    if dry_run:
        _log(f"首日 NULL: 可回填 {fixable} 行 (dry_run, 未执行)")
        return 0
    # UPDATE: JOIN (每 symbol prev_close = MAX(date)<first_date 的 close)
    sql_update = """
        UPDATE daily_bars d
        SET change_pct = ROUND(
            (d.close - p.prev_close) / p.prev_close * 100, 4)
        FROM (
            SELECT d2.symbol, d2.date, p2.close AS prev_close
            FROM daily_bars d2
            INNER JOIN LATERAL (
                SELECT close FROM daily_bars p2
                WHERE p2.symbol = d2.symbol AND p2.date < d2.date
                ORDER BY date DESC LIMIT 1
            ) p2 ON true
            WHERE d2.date = (SELECT MIN(date) FROM daily_bars WHERE symbol = d2.symbol)
              AND d2.change_pct IS NULL
        ) p
        WHERE d.symbol = p.symbol AND d.date = p.date
          AND d.change_pct IS NULL
          AND p.prev_close IS NOT NULL AND p.prev_close > 0
          AND d.close IS NOT NULL AND d.close > 0
    """
    _log(f"首日 NULL: 正在 UPDATE {fixable} 行 ...")
    con.execute(sql_update)
    con.commit()
    _log(f"首日 NULL: 完成")
    return fixable


def run_backfill(dry_run: bool = False) -> dict:
    """无入口函数: 完整跑一次回填 (供 run_daily.py 调度 / 其它脚本调用).

    Returns:
        {"ok": bool, "filled_non_first": int, "filled_first": int,
         "total_filled": int, "remaining_nulls": int, "total_rows": int,
         "note": str}
    """
    out = {"ok": False, "filled_non_first": 0, "filled_first": 0,
           "total_filled": 0, "remaining_nulls": 0, "total_rows": 0,
           "note": ""}
    if not os.path.exists(DUCKDB_PATH):
        # [m4] DuckDB 已退役: 回填依赖 duck 写源, 优雅降级 (不重建空库)
        out["note"] = "DuckDB 已退役/不存在, change_pct 回填跳过 (h5i 由同步时计算)"
        return out
    con = duckdb.connect(DUCKDB_PATH, read_only=False)
    try:
        before = count_nulls(con)
        out["total_rows"] = before["total"]
        n_nonfirst = backfill_non_first(con, dry_run=dry_run)
        n_first = backfill_first(con, dry_run=dry_run)
        after = count_nulls(con)
        out.update({
            "ok": True,
            "filled_non_first": n_nonfirst,
            "filled_first": n_first,
            "total_filled": n_nonfirst + n_first,
            "remaining_nulls": after["null"],
            "note": "用 DuckDB 前一日 close 重算 change_pct",
        })
        if dry_run:
            out["note"] += " (dry_run)"
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:200]
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="仅统计, 不实际 UPDATE")
    ap.add_argument("--limit", type=int, default=0,
                    help="限制回填行数 (0=不限)")
    args = ap.parse_args()

    con = duckdb.connect(DUCKDB_PATH, read_only=False)
    try:
        before = count_nulls(con)
        _log(f"DuckDB daily_bars: total={before['total']:,} "
             f"change_pct IS NULL={before['null']:,} "
             f"({before['null']/before['total']*100:.2f}%)")

        n_nonfirst = backfill_non_first(con, dry_run=args.dry_run)
        n_first = backfill_first(con, dry_run=args.dry_run)

        after = count_nulls(con)
        fixed = before["null"] - after["null"]
        _log(f"完成: 回填 {fixed} 行 "
             f"(非首日 {n_nonfirst} + 首日 {n_first})")
        _log(f"剩余 NULL: {after['null']:,} "
             f"({after['null']/after['total']*100:.2f}%) "
             f"(通常为上市前 / 数据断层的首日, 无法修复)")

        if args.dry_run:
            _log("dry_run 模式, 未实际改动")
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()