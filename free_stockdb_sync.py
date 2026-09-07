# ============================================================
# free_stockdb_sync.py -- free-stockDB 增量同步到 DuckDB daily_bars
#
# 数据源: <本地行情根>/kline_parts/{mkt}_{code6}.parquet (按市场+代码分片; 可用 STOCKDB_ROOT 指定)
#          * sh_*.parquet / sz_*.parquet / bj_*.parquet
#          * 列: trade_date, symbol, open, close, high, low, volume, amount
# 目标表: stockdb.duckdb daily_bars (symbol, date, open, high, low, close,
#                                    volume, amount, change_pct, turnover)
#
# 增量策略:
#   1) DuckDB 端: 先查 daily_bars 每只 symbol 的 max(date) 作为"水位线"
#   2) free_stockdb 端: 读 parquet, 取 trade_date > waterline 的新行
#   3) 缺列填空: change_pct 由 (close-prev_close)/prev_close 算;
#                turnover 留空 (free_stockdb 未提供, 后续可由其它源补)
#   4) 首行 NaN 回填: 查 DuckDB 中该 symbol 在首行日期之前最大交易日 close 作分母重算
#   5) 主键 (symbol, date) ON CONFLICT DO NOTHING (自然防重)
#   6) 按 parquet 分片并行写 (DuckDB 单连接串行, 用 executemany 加速)
#
# 一次性历史回填工具: backfill_change_pct.py
#   把 daily_bars 中历史遗留的 change_pct IS NULL 行用 LAG(close) 批量修复.
#
# 用法:
#   from free_stockdb_sync import sync_incremental
#   sync_incremental()                    # 全增量
#   sync_incremental(symbols=["000001"])  # 仅同步指定代码
#   sync_incremental(since_date="2026-08-20")  # 从指定日期开始 (覆盖水位线)
# ============================================================

from __future__ import annotations

import datetime as dt
import glob
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import duckdb
import pandas as pd

_LOG = logging.getLogger("free_stockdb_sync")

# 路径配置
FREE_STOCKDB_DIR = _lake_root()
KLINE_PARTS_DIR = os.path.join(FREE_STOCKDB_DIR, "kline_parts")
# 与现有 update_db 共用 DuckDB
from config import DUCKDB_PATH  # noqa: E402

# parquet -> DuckDB 列名映射
_COL_MAP = {
    "trade_date": "date",
    "symbol": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
}

# 前缀 -> 市场分类
_MARKET_PREFIX = ("sh_", "sz_", "bj_")


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [free_stockdb] {msg}", flush=True)


def _h5i_write_enabled() -> bool:
    """h5i 主写开关 (m4 起默认开; H5I_WRITE=0|false|no 关闭)."""
    return os.environ.get("H5I_WRITE", "1").strip().lower() not in ("0", "false", "no")


def _h5i_global_waterline() -> dt.date | None:
    """h5i daily_bars 全局最大交易日 (作为 duck 缺失时的水位线回退)."""
    try:
        from h5i_sync import max_bar_date
        return max_bar_date(force=True)
    except Exception:
        return None


def _market_of(prefix: str) -> str | None:
    if prefix == "sh_":
        return "SH"
    if prefix == "sz_":
        return "SZ"
    if prefix == "bj_":
        return "BJ"
    return None


def _list_parquet_files(symbols: list[str] | None = None) -> list[tuple[str, str, str]]:
    """返回 [(market, symbol6, parquet_path), ...]
    symbols: 可选过滤; 不传则扫描全部分片.
    """
    out: list[tuple[str, str, str]] = []
    if not os.path.isdir(KLINE_PARTS_DIR):
        _log(f"kline_parts 目录不存在: {KLINE_PARTS_DIR}")
        return out
    targets = set(str(s).zfill(6) for s in (symbols or [])) if symbols else None
    for prefix in _MARKET_PREFIX:
        mkt = _market_of(prefix)
        if mkt is None:
            continue
        for path in glob.glob(os.path.join(KLINE_PARTS_DIR, f"{prefix}*.parquet")):
            base = os.path.basename(path)
            sym6 = base[len(prefix):].split(".")[0]
            if targets is not None and sym6 not in targets:
                continue
            out.append((mkt, sym6, path))
    return out


def _duckdb_waterlines(symbols: list[str] | None = None) -> dict[str, dt.date]:
    """读 DuckDB daily_bars 每个 symbol 的 max(date) 作为水位线."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        if symbols:
            placeholders = ",".join(["?"] * len(symbols))
            rows = con.execute(
                f"SELECT symbol, MAX(date) FROM daily_bars WHERE symbol IN ({placeholders}) GROUP BY symbol",
                symbols,
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT symbol, MAX(date) FROM daily_bars GROUP BY symbol"
            ).fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass
    out: dict[str, dt.date] = {}
    for sym, d in rows:
        if d is None:
            continue
        # d 可能是 date / datetime / str
        if hasattr(d, "date"):
            out[str(sym).zfill(6)] = d
        else:
            out[str(sym).zfill(6)] = dt.date.fromisoformat(str(d)[:10])
    return out


def _read_parquet_incremental(path: str, waterline: dt.date | None) -> pd.DataFrame | None:
    """读单个 parquet, 只保留 trade_date > waterline 的新行."""
    try:
        df = pd.read_parquet(path, columns=list(_COL_MAP.keys()))
    except Exception as e:
        _log(f"读 {path} 失败: {e}")
        return None
    if df.empty:
        return None
    df = df.rename(columns=_COL_MAP)
    # trade_date str/object -> date
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if waterline is not None:
        df = df[df["date"] > waterline]
    if df.empty:
        return None
    return df


def _lookup_prev_close_db(symbol: str, before_date: dt.date) -> float | None:
    """从 DuckDB daily_bars 查指定 symbol 在 before_date 之前的最大交易日的 close.
    用于回填 free_stockdb 同步时每只 symbol 首行的 change_pct 分母.
    返回 None 表示无前一日数据 (上市前 / 数据缺口).
    [m4] DuckDB 缺失时回退 h5i daily_bars (close_upto).
    """
    try:
        if os.path.exists(DUCKDB_PATH):
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            try:
                row = con.execute(
                    "SELECT close FROM daily_bars WHERE symbol=? AND date<? "
                    "ORDER BY date DESC LIMIT 1",
                    [str(symbol).zfill(6), before_date],
                ).fetchone()
            finally:
                try:
                    con.close()
                except Exception:
                    pass
            if row and row[0] is not None:
                return float(row[0])
    except Exception:
        pass
    # h5i 兜底 (DuckDB 已退役 / 读失败)
    try:
        from h5i_bar_store import H5iBarStore
        s = H5iBarStore()
        try:
            v = s.close_upto(str(symbol).zfill(6), str(before_date))
            return float(v) if v is not None else None
        finally:
            s.close()
    except Exception:
        return None
    return None


def _compute_change_pct(df: pd.DataFrame, backfill_db: bool = True) -> pd.DataFrame:
    """按 symbol 分组算 change_pct = (close - prev_close) / prev_close * 100.

    Args:
        df: 含 symbol/date/close 三列的 DataFrame (按 symbol+date 已排好序)
        backfill_db: 当 df 内首行无 shift(1) 时 (symbol 首次出现), 是否查 DuckDB 找
                     该 symbol 在 df 首行日期之前的最大日期 close 作回填分母.
                     默认 True, 可显著减少 NaN 行数 (尤其历史回灌 / 缺口回填).
    """
    if "close" not in df.columns or "symbol" not in df.columns:
        return df
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    prev_close = df.groupby("symbol")["close"].shift(1)
    df["change_pct"] = ((df["close"] - prev_close) / prev_close * 100).where(prev_close > 0, None)

    # 回填: 每个 symbol 首行 (prev_close 为 NaN) 从 DuckDB 查前一日 close 重算
    if backfill_db and "date" in df.columns:
        head_mask = prev_close.isna() & df["close"].notna() & (df["close"] > 0)
        if head_mask.any():
            symbols_need = df.loc[head_mask, ["symbol", "date"]].to_dict("records")
            fill_count = 0
            for row in symbols_need:
                sym = str(row["symbol"]).zfill(6)
                before_dt = row["date"]
                # before_dt 可能是 pd.Timestamp / date / str
                if hasattr(before_dt, "date"):
                    before_dt = before_dt.date()
                elif isinstance(before_dt, str):
                    before_dt = dt.date.fromisoformat(before_dt[:10])
                prev = _lookup_prev_close_db(sym, before_dt)
                if prev is None or prev <= 0:
                    continue
                # 定位 df 行索引
                idx_list = df.index[(df["symbol"] == sym) & (df["date"] == row["date"])].tolist()
                for i in idx_list:
                    cur = float(df.at[i, "close"])
                    df.at[i, "change_pct"] = round((cur - prev) / prev * 100, 4)
                    fill_count += 1
            if fill_count:
                _log(f"change_pct 回填: {fill_count} 行 (DB 前一日 close)")
    return df


def _duckdb_insert_daily_bars(df: pd.DataFrame) -> int:
    """把增量 df 插入 daily_bars (用临时表 + WHERE NOT EXISTS 防重).

    [m4] DuckDB 已退役/文件缺失时直接跳过 (防止 duckdb.connect 自动重建空库),
    h5i 主写由 sync_incremental 的收集器统一负责.
    """
    if df is None or df.empty:
        return 0
    if not os.path.exists(DUCKDB_PATH):
        return 0
    # 对齐表结构 (symbol, date, open, high, low, close, volume, amount, change_pct, turnover)
    table_cols = ["symbol", "date", "open", "high", "low", "close",
                  "volume", "amount", "change_pct", "turnover"]
    for c in table_cols:
        if c not in df.columns:
            df[c] = None
    df = df[table_cols].copy()
    # 标化空值
    df = df.where(pd.notnull(df), None)
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    rows = [tuple(r) for r in df.itertuples(index=False, name=None)]

    # 构造 SQL (避免 f-string 内反斜杠)
    qcol = '"'  # 双引号常量
    col_list = ", ".join(qcol + c + qcol for c in table_cols)
    cast_types = {
        "symbol": "VARCHAR",
        "date": "DATE",
    }
    type_list = ", ".join(qcol + c + qcol + " " + cast_types.get(c, "DOUBLE") for c in table_cols)
    placeholders = ",".join(["?"] * len(table_cols))
    insert_select = (
        f"INSERT INTO daily_bars ({col_list}) "
        f"SELECT * FROM _tmp_sync "
        f"WHERE NOT EXISTS (SELECT 1 FROM daily_bars d "
        f"                  WHERE d.symbol=_tmp_sync.symbol AND d.date=_tmp_sync.date)"
    )

    con = duckdb.connect(DUCKDB_PATH, read_only=False)
    try:
        n = 0
        BATCH = 5000
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            con.execute("BEGIN")
            try:
                con.execute(
                    f"CREATE TEMP TABLE IF NOT EXISTS _tmp_sync ({type_list})"
                )
                con.executemany(
                    f"INSERT INTO _tmp_sync VALUES ({placeholders})", batch
                )
                con.execute(insert_select)
                con.execute("DELETE FROM _tmp_sync")
                n += len(batch)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return n
    finally:
        try:
            con.close()
        except Exception:
            pass


def _process_one(market: str, sym6: str, path: str,
                 waterline: dt.date | None,
                 batch_size: int = 50,
                 h5i_collect=None) -> dict:
    """处理单只标的: 读 parquet -> 增量 -> 算 change_pct -> 写 DuckDB (尽力).

    [m4] DuckDB 写失败(退役/锁冲突/被删)仅告警, 不再阻断; h5i_collect 收集
    该批新行, 由 sync_incremental 末尾单线程做 h5i 整日单调追加 (主写).
    """
    df = _read_parquet_incremental(path, waterline)
    if df is None or df.empty:
        return {"symbol": sym6, "market": market, "new_rows": 0, "waterline": waterline}
    df = _compute_change_pct(df)
    duck_err = None
    try:
        n = _duckdb_insert_daily_bars(df)
    except Exception as e:  # noqa: BLE001
        n = 0
        duck_err = f"{type(e).__name__}: {str(e)[:160]}"
        _log(f"DuckDB 写入失败 ({sym6}) 已降级, 转 h5i 主写: {duck_err}")
    if h5i_collect is not None:
        try:
            h5i_collect(df)
        except Exception as e:  # noqa: BLE001
            _log(f"h5i 收集失败 ({sym6}): {e}")
    out = {"symbol": sym6, "market": market, "new_rows": len(df), "waterline": waterline,
           "new_first": str(df["date"].min()), "new_last": str(df["date"].max()),
           "duck_rows": n, "duck_err": duck_err}
    return out


def make_h5i_collector(h5i_max_date):
    """构造线程安全收集器: 只保留 date > h5i 现有最大日 的新行.

    h5i 单调追加不允许 <= 现有最大日期的回填, 提前裁剪可避免整文件驻留内存.
    """
    buf: list[pd.DataFrame] = []
    lock = __import__("threading").Lock()

    def collect(df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        sub = df.copy()
        if h5i_max_date is not None and "date" in sub.columns:
            try:
                sub["_d"] = pd.to_datetime(sub["date"], errors="coerce").dt.date
                sub = sub[sub["_d"] > h5i_max_date]
            except Exception:
                pass
        if sub is None or sub.empty:
            return
        with lock:
            buf.append(sub)

    def flush() -> pd.DataFrame | None:
        with lock:
            if not buf:
                return None
            all_ = pd.concat(buf, ignore_index=True)
            buf.clear()
            return all_

    return collect, flush


def refresh_parquet_from_duckdb(symbols: list[str] | None = None) -> dict:
    """用 DuckDB 中更新的 daily_bars 修复本地 free-stockDB parquet 水位。

    外部更新器不跟随 HTTP 302 时，daily_bars 仍会由 AKShare/VNPY 推进；本函数把
    DuckDB 中 parquet 水位之后的数据追加回各分片，保持 free-stockDB 可用且幂等。
    """
    started = time.time()
    files = _list_parquet_files(symbols)
    if not files:
        return {"ok": False, "updated_files": 0, "new_rows": 0,
                "error": f"kline_parts 无文件: {KLINE_PARTS_DIR}"}
    if not os.path.exists(DUCKDB_PATH):
        # [m4] DuckDB 已退役: 反向修复依赖 duck 主源, 直接降级为空结果
        return {"ok": False, "updated_files": 0, "new_rows": 0,
                "failed_files": 0, "max_trade_date": None,
                "elapsed_seconds": 0.0, "error": "DuckDB 已退役/不存在 (反向修复跳过)"}
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    updated_files = new_rows = failed = 0
    max_date = None
    try:
        for _, sym6, path in files:
            try:
                old = pd.read_parquet(path)
                if old.empty:
                    watermark = dt.date(1900, 1, 1)
                else:
                    watermark = pd.to_datetime(old["trade_date"], errors="coerce").max().date()
                fresh = con.execute(
                    "SELECT date AS trade_date, symbol, open, close, high, low, volume, amount "
                    "FROM daily_bars WHERE symbol=? AND date>? ORDER BY date",
                    [sym6, watermark],
                ).fetchdf()
                if fresh.empty:
                    continue
                fresh["trade_date"] = pd.to_datetime(fresh["trade_date"]).dt.strftime("%Y-%m-%d")
                fresh["symbol"] = fresh["symbol"].astype(str).str.zfill(6)
                merged = pd.concat([old, fresh], ignore_index=True)
                merged = merged.drop_duplicates(["symbol", "trade_date"], keep="last")
                merged = merged.sort_values("trade_date").reset_index(drop=True)
                tmp = path + ".tmp.parquet"
                merged.to_parquet(tmp, index=False)
                os.replace(tmp, path)
                updated_files += 1
                new_rows += len(fresh)
                dmax = fresh["trade_date"].max()
                if max_date is None or dmax > max_date:
                    max_date = dmax
            except Exception as e:
                failed += 1
                _log(f"反向修复 {sym6} 失败: {e}")
    finally:
        con.close()
    return {"ok": failed == 0, "updated_files": updated_files,
            "new_rows": new_rows, "failed_files": failed,
            "max_trade_date": max_date,
            "elapsed_seconds": round(time.time() - started, 2),
            "source": "duckdb_daily_bars_fallback"}


# ============================================================
# 主入口
# ============================================================
def sync_incremental(symbols: list[str] | None = None,
                     since_date: str | None = None,
                     max_workers: int = 4,
                     progress_every: int = 500) -> dict:
    """增量同步主入口.

    Args:
        symbols: 可选, 仅同步指定代码列表 (e.g. ["000001", "600000"])
        since_date: 可选 ISO date (e.g. "2026-08-20"), 覆盖 DuckDB 水位线
                    (用于 DuckDB 没记录但 free_stockdb 已有的历史回灌)
        max_workers: 并行分片数 (默认 4)
        progress_every: 每处理 N 个标的打印一次进度

    Returns:
        {
          "ok": bool,
          "total_files": int,
          "scanned": int,
          "new_rows": int,
          "symbols_with_data": int,
          "started_at": iso, "finished_at": iso,
          "min_waterline": date, "max_waterline": date,
          "min_new_date": date, "max_new_date": date,
          "free_stockdb_max_date": date,
          "note": str
        }
    """
    started = dt.datetime.now()
    result: dict = {
        "ok": False,
        "total_files": 0,
        "scanned": 0,
        "new_rows": 0,
        "symbols_with_data": 0,
        "started_at": started.isoformat(timespec="seconds"),
        "note": "",
    }
    files = _list_parquet_files(symbols=symbols)
    result["total_files"] = len(files)
    if not files:
        result["note"] = f"kline_parts 无文件: {KLINE_PARTS_DIR}"
        return result

    # 水位线: since_date 强制基线 > DuckDB 水位线 > h5i 全局水位线 (duck 缺失回退)
    h5i_wl = _h5i_global_waterline() if _h5i_write_enabled() else None
    if since_date:
        # 用 since_date 作为强制基线 (回灌场景)
        forced_waterline = {sym6: dt.date.fromisoformat(since_date) - dt.timedelta(days=1)
                            for _, sym6, _ in files}
        waterlines = forced_waterline
        result["note"] = f"since_date={since_date} 强制覆盖水位线"
    else:
        try:
            waterlines = _duckdb_waterlines(symbols=symbols)
        except Exception as e:  # noqa: BLE001
            _log(f"DuckDB 水位线读取失败, 回退 h5i: {e}")
            waterlines = {}
        if not waterlines:
            if h5i_wl is not None:
                waterlines = {sym6: h5i_wl for _, sym6, _ in files}
                result["note"] = (f"DuckDB 不可用, 水位线回退 h5i 全局最大日 "
                                  f"{h5i_wl.isoformat()}")
            else:
                result["note"] = "DuckDB/h5i 均不可用, 全量扫描 (h5i 单调去重兜底)"

    if waterlines:
        all_lines = list(waterlines.values())
        result["min_waterline"] = min(all_lines).isoformat()
        result["max_waterline"] = max(all_lines).isoformat()

    # 进度回调
    counter = {"scanned": 0, "new_rows": 0, "symbols_with_data": 0,
               "duck_write_failures": 0,
               "min_new_date": None, "max_new_date": None}
    lock = __import__("threading").Lock()

    def _on_done(fut):
        with lock:
            counter["scanned"] += 1
            try:
                r = fut.result()
            except Exception as e:
                _log(f"分片处理异常: {e}")
                return
            if r.get("duck_err"):
                counter["duck_write_failures"] += 1
            if r.get("new_rows", 0) > 0:
                counter["new_rows"] += r["new_rows"]
                counter["symbols_with_data"] += 1
                # 跟踪全局最大日期
                d_first = r.get("new_first")
                d_last = r.get("new_last")
                if d_first:
                    if counter["min_new_date"] is None or d_first < counter["min_new_date"]:
                        counter["min_new_date"] = d_first
                if d_last:
                    if counter["max_new_date"] is None or d_last > counter["max_new_date"]:
                        counter["max_new_date"] = d_last
            if counter["scanned"] % progress_every == 0:
                _log(f"进度 {counter['scanned']}/{len(files)} "
                     f"新增行={counter['new_rows']} 有数标的={counter['symbols_with_data']}")

    _log(f"开始增量同步: {len(files)} 个分片, workers={max_workers}")
    collect, flush = (make_h5i_collector(h5i_wl)
                      if (_h5i_write_enabled() and h5i_wl is not None)
                      else (None, lambda: None))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = []
        for market, sym6, path in files:
            wl = waterlines.get(sym6)
            futs.append(pool.submit(_process_one, market, sym6, path, wl,
                                    h5i_collect=collect))
        for fut in as_completed(futs):
            _on_done(fut)

    finished = dt.datetime.now()
    result.update({
        "ok": True,
        "scanned": counter["scanned"],
        "new_rows": counter["new_rows"],
        "symbols_with_data": counter["symbols_with_data"],
        "duck_write_failures": counter["duck_write_failures"],
        "min_new_date": counter["min_new_date"],
        "max_new_date": counter["max_new_date"],
        "finished_at": finished.isoformat(timespec="seconds"),
        "elapsed_seconds": round((finished - started).total_seconds(), 2),
    })
    _log(f"完成: 扫描 {result['scanned']} / 新增 {result['new_rows']} 行 / "
         f"有数标的 {result['symbols_with_data']} / "
         f"duck写失败 {result['duck_write_failures']} / "
         f"用时 {result['elapsed_seconds']}s")
    # [m4] h5i 主写: 收集到的(晚于 h5i 现有最大日)新行单线程整日单调追加.
    if _h5i_write_enabled():
        try:
            from h5i_sync import append_daily_bars
            comb = flush()
            if comb is not None and not comb.empty:
                result["h5i_primary_write"] = append_daily_bars(comb)
                if result["h5i_primary_write"].get("appended", 0) > 0:
                    _log(f"h5i 主写追加 {result['h5i_primary_write']['appended']} 行")
                elif result["h5i_primary_write"].get("skipped_rows", 0) > 0:
                    _log(f"h5i 主写跳过 {result['h5i_primary_write']['skipped_rows']} 行"
                         f" (回填/重复, 单调约束)")
            else:
                result["h5i_primary_write"] = {
                    "ok": True, "appended": 0, "skipped_rows": 0,
                    "note": "无晚于 h5i 现有最大日期的新行"}
        except Exception as e:  # noqa: BLE001
            result["h5i_primary_write"] = {"ok": False, "error": str(e)[:200]}
    else:
        result["h5i_primary_write"] = {"ok": True, "enabled": False}
    return result


def get_status() -> dict:
    """汇总 free_stockdb 与 DuckDB 数据现状 (供 dashboard / 健康检查)."""
    out = {"free_stockdb": {}, "duckdb_daily_bars": {}}
    # free_stockdb 端
    try:
        files = _list_parquet_files()
        out["free_stockdb"]["files"] = len(files)
        # 全量扫描日期水位。不能只抽样前 50 个分片，否则新文件可能被漏掉。
        max_date = None
        failed_files = 0
        for _, _, p in files:
            try:
                d = pd.read_parquet(p, columns=["trade_date"])
                if d.empty:
                    continue
                m = pd.to_datetime(d["trade_date"], errors="coerce").max()
                if pd.notna(m) and (max_date is None or m > max_date):
                    max_date = m
            except Exception:
                failed_files += 1
        if max_date is not None:
            out["free_stockdb"]["max_trade_date"] = str(max_date.date())
        out["free_stockdb"]["date_scan"] = "full"
        out["free_stockdb"]["read_failures"] = failed_files
    except Exception as e:
        out["free_stockdb"]["error"] = str(e)[:120]
    # DuckDB 端
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            row = con.execute(
                "SELECT COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM daily_bars"
            ).fetchone()
        finally:
            try:
                con.close()
            except Exception:
                pass
        if row:
            out["duckdb_daily_bars"] = {
                "symbols": row[0],
                "min_date": str(row[1]) if row[1] else None,
                "max_date": str(row[2]) if row[2] else None,
            }
    except Exception as e:
        out["duckdb_daily_bars"]["error"] = str(e)[:120]
    return out


if __name__ == "__main__":
    import argparse, json

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", help="指定代码 (e.g. 000001 600000)")
    ap.add_argument("--since-date", help="强制基线 (e.g. 2026-08-20)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--status", action="store_true", help="仅打印状态, 不增量")
    args = ap.parse_args()
    if args.status:
        print(json.dumps(get_status(), ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(
            sync_incremental(symbols=args.symbols, since_date=args.since_date,
                             max_workers=args.workers),
            ensure_ascii=False, indent=2, default=str
        ))