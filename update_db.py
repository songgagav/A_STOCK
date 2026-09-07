# ============================================================
# update_db.py -- 收盘后统一数据库增量更新
# 自动补录 DB_UPDATE_TARGETS 配置中所有 DuckDB 表头对应的当日数据,
# 每个表独立异常处理, 单项失败不阻断其他表; 写入 sync_status 同步状态.
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_UPDATE_TARGETS, DUCKDB_PATH  # noqa: E402


# ---------- helpers ----------
def _today() -> dt.date:
    return dt.date.today()


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _h5i_mirror_if_enabled() -> dict:
    """[2026-09-05 h5i 双写/主写] DuckDB daily_bars 写成功后镜像到 h5i.

    默认启用 (H5I_WRITE, m4 起默认开; 设 H5I_WRITE=0 关闭). 任何异常只告警,
    不阻断 duck 写入.
    """
    if os.environ.get("H5I_WRITE", "1").strip().lower() in ("0", "false", "no"):
        return {"ok": True, "enabled": False}
    try:
        from h5i_sync import mirror_duck_to_h5i
        r = mirror_duck_to_h5i()
        _log(f"  h5i 双写 mirror: appended={r.get('appended')} "
             f"read={r.get('read_rows')} skipped={r.get('skipped_rows')} "
             f"err={(r.get('error') or '')[:120]}")
        return r
    except Exception as e:  # noqa: BLE001
        _log(f"  h5i 双写 mirror 异常(不阻断 duck 写入): {e}")
        return {"ok": False, "error": str(e)[:200]}


def _norm_date(d) -> dt.date | None:
    if d is None or (isinstance(d, float) and pd.isna(d)):
        return None
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    try:
        return pd.to_datetime(d).date()
    except Exception:
        return None


def _num(v):
    """安全转数值: 缺失/'-'/空/非数值 → None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "None", "nan"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _canon_to_symbol6(canon: str) -> str:
    return canon.split(".")[0]


def _list_active_symbols(con, start: str | None = None,
                         end: str | None = None) -> list[tuple[str, str]]:
    """返回 (symbol6, market) 列表, 仅含活跃股票. 支持分段."""
    sql = "select symbol, market from symbols where is_active = true"
    args: list = []
    if start:
        sql += " and symbol >= ?"
        args.append(start)
    if end:
        sql += " and symbol <= ?"
        args.append(end)
    sql += " order by symbol"
    rows = con.execute(sql, args).fetchall()
    return [(str(s), str(m or "")) for s, m in rows]


def _existing_symbols_for_day(con, table: str, day: dt.date) -> set[str]:
    """返回当天已经写入的 symbol 集合, 用于断点续传."""
    rows = con.execute(
        f"select symbol from \"{table}\" where date=?", [day]
    ).fetchall()
    return {r[0] for r in rows}


def _sina_symbol(symbol6: str, market: str) -> str | None:
    m = market.lower()
    if m == "sh":
        return f"sh{symbol6}"
    if m in ("sz", ""):
        return f"sz{symbol6}"
    if m == "bj":
        return f"bj{symbol6}"
    return None


# ---- A股代码段治理 (对应 "全A空返回缺口" 排查结论) ----
# 实测结论 (2026-08): stock_zh_a_hist_tx 对下列段不可用:
#   - 11/12 段: 上/深交所可转债, 非股票, 混入 symbols 表属于数据源污染;
#   - 83/87/43/92 段: 北交所, 腾讯源抛 KeyError('day') 不支持,
#     应由本地 kline_parts 镜像 (bj_*.parquet) 快路径负责, 而非 AKShare 兜底。
# 因此兜底层只对沪深股票段拉取, 转债与北交所段在此跳过。
_BOND_SEG = ("11", "12")             # 可转债段
_BSE_SEG = ("43", "82", "83", "87", "88", "89", "92")  # 北交所/三板段


def _is_bond_symbol(symbol6: str) -> bool:
    """11/12 段为可转债(非A股), 回归 stock_zh_a_hist_tx 必然空/异常, 应剔除."""
    return bool(symbol6) and symbol6[:2] in _BOND_SEG


def _is_bse_symbol(symbol6: str) -> bool:
    """83/87/43/92 等段为北交所; 腾讯源不支持, 由 kline_parts 镜像快路径负责."""
    return bool(symbol6) and symbol6[:2] in _BSE_SEG


def _safe_ak_call(func, *args, retries: int = 2, sleep: float = 0.8,
                   network_timeout: float = 8.0, **kwargs):
    """带重试的 AKShare 调用包装 (向后兼容: 直接返回 df).

    错误分类通过模块级状态变量暴露:
      - _safe_ak_call.last_status: "ok" | "empty_data" | "network_error" | "other_error" | "func_not_found"
      - _safe_ak_call.last_err: 最后一次错误对象 (str() 即可看)
      - _safe_ak_call.network_errors: 累计网络错误次数 (本进程级)
      - _safe_ak_call.empty_count: 累计 empty_data 次数
    老调用方: df = _safe_ak_call(...) 仍然拿到 df (可能 None).
    """
    import akshare as ak  # 本地导入, 失败不影响其他表
    if not hasattr(_safe_ak_call, "network_errors"):
        _safe_ak_call.network_errors = 0
        _safe_ak_call.empty_count = 0
        _safe_ak_call.last_status = "ok"
        _safe_ak_call.last_err = None

    last_err = None
    last_status = "ok"
    fn = getattr(ak, func, None)
    if fn is None:
        _safe_ak_call.last_status = "func_not_found"
        _safe_ak_call.last_err = RuntimeError(f"akshare 函数 {func} 不存在")
        return None

    # 网络类异常集
    net_excs = (
        TimeoutError, ConnectionError, OSError,
    )
    # urllib3 / requests 异常 (AKShare 底层)
    try:
        import requests
        net_excs = net_excs + (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError,
        )
    except Exception:
        pass

    for k in range(retries):
        try:
            df = fn(*args, **kwargs)
            # 区分 "正常空数据" vs "网络层失败但 fn 仍返回 None/Empty"
            if df is None or (hasattr(df, "empty") and df.empty):
                _safe_ak_call.last_status = "empty_data"
                _safe_ak_call.empty_count += 1
                return df
            _safe_ak_call.last_status = "ok"
            return df
        except (IndexError, KeyError, AttributeError, ValueError) as e:  # noqa: BLE001
            # 数据缺失: 该股可能新股/停牌/退市, 直接视为空数据
            _safe_ak_call.last_status = "empty_data"
            _safe_ak_call.empty_count += 1
            return None
        except net_excs as e:  # 网络层
            last_err = e
            last_status = "network_error"
            _safe_ak_call.network_errors += 1
            if k < retries - 1:
                time.sleep(sleep * (k + 1))
        except Exception as e:  # noqa: BLE001
            last_err = e
            last_status = "other_error"
            if k < retries - 1:
                time.sleep(sleep * (k + 1))
    _safe_ak_call.last_status = last_status
    _safe_ak_call.last_err = last_err
    return None


def _safe_ak_status() -> dict:
    """返回累计 AKShare 调用统计 (供 run_daily 步进诊断 / dashboard)."""
    return {
        "last_status": getattr(_safe_ak_call, "last_status", "ok"),
        "last_err": str(getattr(_safe_ak_call, "last_err", "") or "")[:200],
        "network_errors": getattr(_safe_ak_call, "network_errors", 0),
        "empty_count": getattr(_safe_ak_call, "empty_count", 0),
    }


def _table_columns(con, table: str) -> list[str]:
    rows = con.execute("pragma table_info('" + table + "')").fetchall()
    return [r[1] for r in rows]


def _upsert(con, table: str, df: pd.DataFrame, conflict_keys: list[str] | None = None) -> int:
    """把 df 写入 table, 自动按目标表 DDL 对齐列, 多余列忽略, 缺失列填空.

    P10: 只有当冲突键上有**真正的 PK 约束**时才用 ON CONFLICT.
    若表无 PK(A股库多数表如 valuation_snapshot/daily_bars 均无约束),
    改为 INSERT 前先按冲突键 DELETE 匹配行, 保证幂等不重复写.
    """
    if df is None or df.empty:
        return 0
    table_cols = _table_columns(con, table)
    if not table_cols:
        return 0
    # 检测真实 PK 约束: PRAGMA table_info 第6列(pk)标志
    try:
        pk_cols = {r[1] for r in con.execute(f"pragma table_info('{table}')").fetchall()
                   if r[5]}
    except Exception:
        pk_cols = set()
    # 对齐
    for c in table_cols:
        if c not in df.columns:
            df[c] = None
    cols = [c for c in table_cols if c in df.columns]
    n_cols = len(cols)
    if n_cols == 0:
        return 0
    col_list = ", ".join('"' + c + '"' for c in cols)
    placeholders = ", ".join(["?"] * n_cols)
    rows = []
    for _, r in df.iterrows():
        row = []
        for c in cols:
            v = r[c]
            if v is None:
                row.append(None)
            elif isinstance(v, float) and pd.isna(v):
                row.append(None)
            elif hasattr(v, "isoformat"):
                row.append(v)
            else:
                row.append(v)
        rows.append(tuple(row))

    # 决定去重策略
    keys = [k for k in (conflict_keys or []) if k in cols and k in table_cols]
    use_on_conflict = bool(keys) and set(keys) <= pk_cols  # 真PK才可 ON CONFLICT

    # 无 PK 但有冲突键: 先删后插, 避免重复行累积(幂等)
    if keys and not use_on_conflict:
        del_cols = ", ".join('"' + k + '"' for k in keys)
        seen = set()
        del_rows = []
        for row in rows:
            rk = tuple(row[cols.index(k)] for k in keys)
            if rk not in seen:
                seen.add(rk)
                del_rows.append(rk)
        for rk in del_rows:
            where = " AND ".join(f'"{k}" = ?' for k in keys)
            con.execute(f"DELETE FROM {table} WHERE {where}", list(rk))

    if use_on_conflict:
        conflict_cols = ", ".join('"' + k + '"' for k in keys)
        sql = (f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
               f"ON CONFLICT ({conflict_cols}) DO NOTHING")
        try:
            con.executemany(sql, rows)
            return len(rows)
        except Exception:
            pass  # 冲突键失效时回退普通 INSERT
    con.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", rows)
    return len(rows)


def _write_sync_status(con, table: str, ok: bool, rows: int, day: dt.date,
                       error: str | None = None) -> None:
    """兼容两种 sync_status 结构.
    - 新结构 (本脚本首次写入): 含 last_date/last_rows/last_ok/last_error
    - 旧结构 (db.py 已建): 含 stock_count/row_count/last_sync/date_range
    """
    cols = _table_columns(con, "sync_status")
    if not cols:
        con.execute(
            """
            CREATE TABLE sync_status (
                table_name VARCHAR PRIMARY KEY,
                last_date   DATE,
                last_rows   INTEGER,
                last_ok     BOOLEAN,
                last_error  VARCHAR,
                updated_at  TIMESTAMP
            )
            """,
        )
        cols = _table_columns(con, "sync_status")
    has_new = {"last_date", "last_rows", "last_ok", "last_error"}.issubset(set(cols))
    if has_new:
        con.execute(
            """
            INSERT INTO sync_status (table_name, last_date, last_rows, last_ok, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (table_name) DO UPDATE SET
                last_date  = EXCLUDED.last_date,
                last_rows  = EXCLUDED.last_rows,
                last_ok    = EXCLUDED.last_ok,
                last_error = EXCLUDED.last_error,
                updated_at = EXCLUDED.updated_at
            """,
            [table, day, int(rows or 0), bool(ok), (error or "")[:1000]],
        )
    else:
        # 旧表结构: stock_count/row_count/last_sync/date_range
        con.execute(
            """
            INSERT INTO sync_status (table_name, stock_count, row_count, last_sync, date_range)
            VALUES (?, 0, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT (table_name) DO UPDATE SET
                row_count = EXCLUDED.row_count,
                last_sync = EXCLUDED.last_sync,
                date_range = EXCLUDED.date_range
            """,
            [table, int(rows or 0), str(day)],
        )


# ============================================================
# 各表同步函数. 返回 (ok, rows, error)
# ============================================================

def sync_daily_bars(con, day: dt.date, start_symbol: str | None = None,
                     end_symbol: str | None = None,
                     use_fast_path: bool = True, **_) -> tuple[bool, int, str | None]:
    """按 symbol 拉取日线, 分批提交, 过滤日期 = day 的写入 daily_bars.
    断点续传: 已存在于 daily_bars 的 symbol 自动跳过; 支持 start/end 分段.

    == 快路径优先 (free-stockdb / local_pull) ==
    先走本地 kline_parts parquet 全A镜像增量补录 (零网络 ~30s),
    仅对本地仍缺失的 symbol 回退 AKShare 逐股兜底, 大幅降低全A拉取耗时.
    use_fast_path=False 可强制关闭 (纯 AKShare 旧路径)."""
    import akshare as ak  # noqa: F401

    # ---- 快通道: 本地 free-stockdb / CNEquity 镜像增量补录 (主源, 零网络) ----
    # [2026-09-05] 主源改造: 采用 free_stockdb_sync.sync_incremental 而非 local_pull.
    #   原因: (a) sync_incremental 读取时做列裁剪, 全A镜像 ~30-60s;
    #         (b) local_pull._read_parquet_incremental 整文件读入, 全A扫描 ~17min;
    #         (c) since_date 强制基线可覆盖"水位线跳洞"(某 symbol 缺 09-01 但已有
    #             09-02 时, max(date) 水位会永久跳过 09-01), 每次跑近窗天然自愈.
    #   仅对镜像仍缺的 symbol 回退 AKShare 逐股兜底 (辅助源)."""
    import akshare as ak  # noqa: F401

    if use_fast_path and start_symbol is None and end_symbol is None:
        fast_start = (day - dt.timedelta(days=10)).isoformat()
        try:
            from free_stockdb_sync import sync_incremental
        except Exception as e:  # noqa: BLE001
            _log(f"  free_stockdb_sync 导入失败, 走纯 AKShare 兜底: {e}")
        else:
            try:
                r = sync_incremental(max_workers=8, since_date=fast_start,
                                     progress_every=2000)
                _log(f"  快路径[镜像主源] scanned={r.get('scanned')} "
                     f"new_rows={r.get('new_rows')} syms={r.get('symbols_with_data')} "
                     f"max_new={r.get('max_new_date')} elapse={r.get('elapsed_seconds')}s")
            except Exception as e:  # noqa: BLE001
                _log(f"  镜像主源异常, 走 AKShare 兜底: {type(e).__name__}: {e}")
        # 快路径走独立连接写库; 此处重读 existing, 使 AKShare 仅补镜像未覆盖的 symbol
        # (避免同一日期的重复拉取, 兼作主源覆盖率兜底)。

    syms = _list_active_symbols(con, start=start_symbol, end=end_symbol)
    existing = _existing_symbols_for_day(con, "daily_bars", day)
    if existing:
        _log(f"  day={day} 已有 {len(existing)} 只在库, 其余走 AKShare 兜底")
    target_rows: list[dict] = []
    errors: list[str] = []
    total_written = 0

    start = (day - dt.timedelta(days=7)).strftime("%Y%m%d")
    end = day.strftime("%Y%m%d")

    def fetch_one(s6: str, mkt: str) -> list[dict]:
        prefix = {"sh": "sh", "sz": "sz", "bj": "bj"}.get(mkt, "sz")
        df = _safe_ak_call("stock_zh_a_hist_tx",
                           symbol=prefix + s6,
                           start_date=start, end_date=end,
                           adjust="")
        if df is None or df.empty:
            return []
        prev_close_map = {}
        for _, r in df.iterrows():
            d = _norm_date(r.get("date"))
            prev_close_map[d] = float(r["close"]) if r.get("close") not in (None, "-") else None
        prev_day = day - dt.timedelta(days=1)
        prev_close = prev_close_map.get(prev_day)
        out = []
        for _, r in df.iterrows():
            d = _norm_date(r.get("date"))
            if d == day:
                close = float(r["close"]) if r.get("close") not in (None, "-") else None
                chg = (close / prev_close - 1) if (close and prev_close) else None
                out.append({
                    "symbol": s6,
                    "date": day,
                    "open": float(r["open"]) if r.get("open") not in (None, "-") else None,
                    "high": float(r["high"]) if r.get("high") not in (None, "-") else None,
                    "low": float(r["low"]) if r.get("low") not in (None, "-") else None,
                    "close": close,
                    "volume": float(r["volume"]) if r.get("volume") not in (None, "-") else None,
                    "amount": float(r["amount"]) if r.get("amount") not in (None, "-") else None,
                    "change_pct": (chg * 100) if chg is not None else None,
                    "turnover": float(r["turnover"]) if r.get("turnover") not in (None, "-") else None,
                })
        return out

    todo = [(s, m) for s, m in syms
            if s not in existing
            and not _is_bond_symbol(s)
            and not _is_bse_symbol(s)]
    # skipped 仅在全量模式有统计意义 (existing 含库内全部历史, 分段时 existing
    # 大于分段内 syms, 差值会为负, 故用 max(0,..) 化并限制在 syms 范围内)。
    skipped = max(0, len(syms) - len(existing) - len(todo))
    if skipped:
        _log(f"  跳过非股票/北交所段 {skipped} 只 (转债11/12 或 北交所83/87/43/92, 走镜像快路径)")
    _log(f"  day={day} 待处理 {len(todo)} 只 (总 {len(syms)}, 已有 {len(existing)})")
    if not todo:
        _h5i_mirror_if_enabled()  # 兜底无新增时仍可追平快通道/其它写入的 duck 新行
        return True, 0, None

    acc_rows: list[dict] = []   # 本日 AKShare 兜底拉到的全部行 (用于 h5i 整日单调追加)
    with ThreadPoolExecutor(max_workers=6) as exe:
        futs = {exe.submit(fetch_one, s, m): s for s, m in todo}
        processed = 0
        for f in as_completed(futs):
            try:
                got = f.result(timeout=30)
                target_rows.extend(got)
                acc_rows.extend(got)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{futs[f]}: {type(e).__name__}")
            processed += 1
            # 每 100 只 commit 一次, 减少中断损失
            if processed % 100 == 0 or processed == len(futs):
                if target_rows:
                    # [m4] DuckDB 尽力写: 锁冲突/退役等写失败仅告警, 不阻断 h5i 主写
                    try:
                        n = _upsert(con, "daily_bars", pd.DataFrame(target_rows),
                                    conflict_keys=["symbol", "date"])
                        total_written += n
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"duck_write:{type(e).__name__}:{str(e)[:80]}")
                        _log(f"  DuckDB daily_bars 写失败(不阻断 h5i 主写): {e}")
                    _log(f"  day={day} progress={processed}/{len(futs)} "
                         f"committed={total_written} cum={total_written}")
                    target_rows.clear()

    # [m4] h5i 主写: 无论 duck 是否写成功, 把本日整批行交给 append_daily_bars
    # (整日单调追加语义; 回填/重复日期内部跳过并告警). 单线程整日提交, 防半日损坏.
    if acc_rows:
        try:
            from h5i_sync import append_daily_bars
            _hd = append_daily_bars(pd.DataFrame(acc_rows))
            if _hd.get("appended", 0) > 0:
                _log(f"  h5i 主写 daily_bars 追加 {_hd['appended']} 行 "
                     f"(day={day}, dates={_hd.get('dates')})")
            elif _hd.get("skipped_rows", 0) > 0:
                _log(f"  h5i 主写跳过 {_hd['skipped_rows']} 行 (回填/重复, <=h5i 最大日期)")
        except Exception as e:  # noqa: BLE001
            _log(f"  h5i 主写 daily_bars 异常(不阻断): {e}")

    if not target_rows and not acc_rows:
        # 注意: 这里 total_written 可能为 0, 但 errors 为空, 说明 AKShare 静默返回空数据.
        # 网络层可能完全不可达, 也可能是非交易时段/股已被摘牌.
        # 不再把 "total_written==0 && no errors" 视为 ok=False,
        # 改为: 有 errors 才视为失败; 否则视为 "ok (no data today)" 由 free_stockdb 兜底.
        if errors:
            return False, total_written, ("; ".join(errors[:5]))
        # 空但无 error: 返回 (True, 0, "no_data_<network_or_empty>")
        status = _safe_ak_status()
        _h5i_mirror_if_enabled()
        return True, total_written, (
            f"no_data_today (ak_status={status['last_status']}, "
            f"network_errors={status['network_errors']}, "
            f"empty_count={status['empty_count']})"
        )

    if target_rows:
        try:
            df = pd.DataFrame(target_rows)
            n = _upsert(con, "daily_bars", df, conflict_keys=["symbol", "date"])
            total_written += n
        except Exception as e:  # noqa: BLE001
            errors.append(f"duck_write:{type(e).__name__}:{str(e)[:80]}")
            _log(f"  DuckDB daily_bars 最终批写失败(不阻断): {e}")
    _h5i_mirror_if_enabled()
    return True, total_written, ("; ".join(errors[:5]) if errors else None)


def sync_valuation_snapshot(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    """从新浪 stock_zh_a_spot 拉取全表估值, 写入 fetch_time = now (date=day)."""
    import akshare as ak  # noqa: F401
    df = _safe_ak_call("stock_zh_a_spot")
    if df is None or df.empty:
        return False, 0, "stock_zh_a_spot 返回空"

    fetch_time = dt.datetime.combine(day, dt.time(15, 0))
    out_rows = []
    for _, r in df.iterrows():
        raw = str(r.get("代码") or "").strip()
        # 兼容 'bj920000' / 'sh600000' / 'sz000001' 等带前缀格式, 以及裸 6 位
        low = raw.lower()
        pfx = low[:2] if low[:2] in ("sh", "sz", "bj") else ""
        code = low[2:] if pfx else low
        if not code or len(code) != 6 or not code.isdigit():
            continue
        market = pfx if pfx else (
            "sh" if code.startswith(("5", "6", "7", "9")) and not code.startswith(("90", "20")) else
            "bj" if code.startswith(("8", "43", "92")) else "sz")
        def _f(key):
            """安全取数值: 列缺失/'-'/'None'/空 → None."""
            v = r.get(key)
            if v is None or (isinstance(v, str) and v.strip() in ("", "-", "None", "nan")):
                return None
            try:
                return float(str(v).replace("%", "").replace(",", ""))
            except Exception:
                return None
        out_rows.append({
            "symbol": code,
            "name": str(r.get("名称") or "")[:64],
            "price": _f("最新价"),
            "change_pct": _f("涨跌幅"),
            "change_amt": _f("涨跌额"),
            "volume": _f("成交量"),
            "amount": _f("成交额"),
            "open": _f("今开"),
            "high": _f("最高"),
            "low": _f("最低"),
            "prev_close": _f("昨收"),
            "turnover": _f("换手率"),
            "pe_ttm": _f("市盈率"),
            "pb": _f("市净率"),
            "total_mv": _f("总市值"),
            "float_mv": _f("流通市值"),
            "fetch_time": fetch_time,
            "market": market,
        })

    if not out_rows:
        return False, 0, "valuation 行解析后为空"

    # —— 段健康度校验 (治本, 对应 F2): float_mv/turnover 覆盖过低说明源接口
    # 字段列缺失(实测 08-28 新浪 stock_zh_a_spot 的"流通市值/换手率"全空),
    # 整段写入会污染最新 fetch_time, 导致 get_universe 选到脏段把池子抽干.
    # 覆盖 <80% 直接拒写, 保住历史完整段. ——
    # [2026-09-04] 新增兜底: 新浪 spot 长期字段缺失(08-25~09-04 连续拒写)会让
    # valuation_snapshot 停在残段. 拒写前先试 free-stockdb 引擎快通道全量重建
    # (0*/3*/6* 实测 5181 只, 字段齐全, 基准日滞后但估值字段可用). ——
    pdf = pd.DataFrame(out_rows)
    for fld in ("float_mv", "turnover"):
        if fld in pdf.columns:
            tot = len(pdf)
            ok = int(pdf[fld].notna().sum())
            cov = ok / tot if tot else 0.0
            if cov < 0.8:
                try:
                    from local_pull import pull_engine_valuation
                    r = pull_engine_valuation()
                    if r.get("ok") and int(r.get("rows", 0)) >= 3000:
                        return True, int(r["rows"]), (
                            f"新浪spot字段{fld}覆盖率{cov:.0%}, 引擎快通道兜底: "
                            f"{r['rows']}行 (估值基准日 {r.get('latest_date')})")
                except Exception:
                    pass
                return False, 0, (f"valuation 段 {fetch_time:%m-%d} 字段 {fld} 覆盖率 "
                                  f"{cov:.0%}({ok}/{tot})过低, 拒写避免污染历史完整段")

    # 按 (symbol, fetch_time) 去重更新
    cur = con.execute("PRAGMA table_info('valuation_snapshot')").fetchall()
    has_pkey = any(c[1] == "symbol" for c in cur)
    pdf = pd.DataFrame(out_rows)
    n = _upsert(con, "valuation_snapshot", pdf,
                conflict_keys=(["symbol", "fetch_time"] if has_pkey else None))
    return True, n, None


def sync_adj_factors(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    """拉全 A 当日复权因子 (AkShare stock_zh_a_daily 含 adj_factor 时使用, 这里兜底 None)."""
    # daily_bars 已写入不复权+前复权价格; 不重复实现
    return True, 0, "复权因子以 daily_bars 前复权为准"


def sync_northbound_money(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    import akshare as ak  # noqa: F401
    df = _safe_ak_call("stock_hsgt_north_net_flow_in_em", indicator="沪股通")
    # _safe_ak_call 对"函数不存在"返回 None 且 last_status='func_not_found'(不抛异常),
    # 因此旧版 try/except fallback 永远不会触发. 这里显式判断后走汇总 fallback.
    is_summary = False
    if df is None or _safe_ak_call.last_status == "func_not_found":
        is_summary = True
        df = _safe_ak_call("stock_hsgt_fund_flow_summary_em")
    if df is None or df.empty:
        return False, 0, f"北向资金接口返回空 (last={_safe_ak_call.last_status})"
    cur = con.execute("PRAGMA table_info('northbound_money')").fetchall()
    if not cur:
        return False, 0, "northbound_money 表结构未知"
    now = dt.datetime.now()

    if is_summary:
        # 东财汇总接口 stock_hsgt_fund_flow_summary_em, 中文列 → northbound_money 字段映射.
        # 板块 → market_type / market_code; 成交净买额→net_buy_amount; 资金净流入→daily_inflow;
        # 当日资金余额→daily_balance; 指数涨跌幅→sh_index_change_pct.
        # 真实表数字列允许 NULL: 数据源未提供的字段一律 None (与历史 NULL 口径一致), 避免 0 伪装成真实零值.
        # 北向两行 net_buy_amount 用 None: 东财已停更北向净买额, 接口返回的 0 实为"无数据".
        block_map = {
            "沪股通": "sh_hk_connect", "港股通(沪)": "hk_sh_connect",
            "深股通": "sz_hk_connect", "港股通(深)": "hk_sz_connect",
        }
        mt_map = {
            "沪股通": "沪股通", "港股通(沪)": "港股通沪",
            "深股通": "深股通", "港股通(深)": "港股通深",
        }
        rows = []
        for _, r in df.iterrows():
            block = str(r.get("板块") or "").strip()
            code = block_map.get(block)
            if code is None:
                continue  # 未知板块丢弃
            direction = str(r.get("资金方向") or "").strip()
            is_north = direction == "北向"
            rows.append({
                "trade_date": day,
                "market_type": mt_map.get(block, block),
                "market_code": code,
                "net_buy_amount": None if is_north else _num(r.get("成交净买额")),
                "buy_amount": None,
                "sell_amount": None,
                "cumulative_net_buy": None,
                "daily_inflow": _num(r.get("资金净流入")),
                "daily_balance": _num(r.get("当日资金余额")),
                "holding_market_value": None,
                "leading_stock": "",
                "leading_change_pct": None,
                "sh_index": None,          # 汇总接口无指数点位, 保留缺失而非 0
                "sh_index_change_pct": _num(r.get("指数涨跌幅")),
                "leading_stock_code": "",
                "source": "akshare_fund_flow_summary_em",
                "ingested_at": now,
            })
        if not rows:
            return False, 0, "北向资金汇总行解析后为空"
        pdf = pd.DataFrame(rows)
    else:
        # 旧接口路径 (AKShare 1.18.88 已移除, 此处兜底): 仅记录日期 + 净买额/累计值.
        pdf = df.copy()
        pdf["trade_date"] = day
        if "market_type" not in pdf.columns:
            pdf["market_type"] = "汇总"
        else:
            pdf["market_type"] = pdf["market_type"].fillna("汇总").astype(str)
    n = _upsert(con, "northbound_money", pdf)
    # [m4] DuckDB 写成功后同步到 h5i.northbound_money (单调追加, 同日跳过防重复);
    # 仅告警, 不阻断 duck 主流程.
    try:
        if os.environ.get("H5I_WRITE", "1").strip().lower() not in ("0", "false", "no"):
            from migrate_m4_misc_h5i import append_northbound_money
            _h = append_northbound_money(pdf)
            if not _h.get("ok") and _h.get("error"):
                _log(f"  h5i northbound 追加失败(不阻断): {_h['error']}")
            elif _h.get("appended", 0) > 0:
                _log(f"  h5i northbound 追加 {_h['appended']} 行")
            elif _h.get("skipped_rows", 0) > 0:
                _log(f"  h5i northbound 同日/回填跳过 {_h['skipped_rows']} 行 (防重复)")
    except Exception as e:  # noqa: BLE001
        _log(f"  h5i northbound 追加异常(不阻断): {e}")
    return True, n, None


def sync_margin_daily(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    import akshare as ak  # noqa: F401
    df = _safe_ak_call("stock_margin_underlying_info_szse", date=day.strftime("%Y%m%d"))
    # 显式判断 func_not_found 后走 sse fallback(与 northbound 同因修复).
    if df is None or _safe_ak_call.last_status == "func_not_found":
        df = _safe_ak_call("stock_margin_underlying_info_sse", date=day.strftime("%Y%m%d"))
    if df is None or df.empty:
        return False, 0, f"融资融券接口返回空 (last={_safe_ak_call.last_status})"
    pdf = df.copy()
    pdf["trade_date"] = day
    # DuckDB 表上 symbol 是 NOT NULL, AKShare 数据缺失时填默认空字符串避免整批 FAIL
    if "symbol" not in pdf.columns:
        pdf["symbol"] = ""
    else:
        pdf["symbol"] = pdf["symbol"].fillna("").astype(str)
    n = _upsert(con, "margin_daily", pdf)
    return True, n, None


def sync_dzjy_daily(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    import akshare as ak  # noqa: F401
    ymd = day.strftime("%Y%m%d")
    # stock_dzjy_sctj 仅含市场汇总(无逐股), 改用 stock_dzjy_mrmx 拉当日大宗分笔并映射到 dzjy_daily.
    df = _safe_ak_call("stock_dzjy_mrmx", start_date=ymd, end_date=ymd)
    if df is None or df.empty:
        return False, 0, "大宗交易返回空"
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("证券代码") or r.get("代码") or "").strip()
        if not code:
            continue
        try:
            rows.append({
                "symbol": code,
                "trade_date": day,
                "name": str(r.get("证券简称") or r.get("名称") or "")[:64],
                "deal_price": float(r["成交价"]) if r.get("成交价") not in (None, "-", "") else None,
                "deal_qty": float(r["成交量"]) if r.get("成交量") not in (None, "-", "") else None,
                "deal_amount": float(r["成交额"]) if r.get("成交额") not in (None, "-", "") else None,
                "premium_rate": None,
                "change_pct": None,
                "close": None,
                "deal_count": 1.0,
                "amount_to_float": None,
                "source": "akshare_dzjy_mrmx",
            })
        except Exception:
            continue
    if not rows:
        return False, 0, "大宗交易行解析后为空"
    pdf = pd.DataFrame(rows)
    n = _upsert(con, "dzjy_daily", pdf,
                conflict_keys=["symbol", "trade_date"])
    return True, n, None


def sync_money_flow_estimate(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    import akshare as ak  # noqa: F401
    try:
        df = _safe_ak_call("stock_market_fund_flow", symbol="全部A股")
    except Exception as e:
        return False, 0, str(e)
    if df is None or df.empty:
        return False, 0, "主力资金流返回空"
    pdf = df.copy()
    pdf["trade_date"] = day
    n = _upsert(con, "money_flow_estimate", pdf)
    return True, n, None


def sync_events(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    # events 表通常由 selector / scheduler_entry 写入, 不在此处覆盖
    return True, 0, "events 由 selector 维护"


def sync_lhb(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    import akshare as ak  # noqa: F401
    ymd = day.strftime("%Y%m%d")
    # current akshare: stock_lhb_stock_statistic_em(symbol=...) 无 start/end 参数,
    # 改用 stock_lhb_detail_em(start_date, end_date) 拉当日龙虎榜并映射到 lhb 表.
    df = _safe_ak_call("stock_lhb_detail_em", start_date=ymd, end_date=ymd)
    if df is None or df.empty:
        return False, 0, "龙虎榜返回空"
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码") or "").strip()
        if not code or len(code) != 6:
            continue
        rows.append({
            "code": code,
            "name": str(r.get("名称") or "")[:64],
            "trade_date": day,
            "buy_amount": _num(r.get("龙虎榜买入额")),
            "sell_amount": _num(r.get("龙虎榜卖出额")),
            "net_amount": _num(r.get("龙虎榜净买额")),
            "reason": str(r.get("上榜原因") or "")[:255],
        })
    if not rows:
        return False, 0, "龙虎榜行解析后为空"
    pdf = pd.DataFrame(rows)
    n = _upsert(con, "lhb", pdf, conflict_keys=["code", "trade_date"])
    return True, n, None


def sync_lhb_detail(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    # lhb_detail 由 lhb 按股票单独补全, 避免一次性拉大量接口
    return True, 0, "lhb_detail 按需补全"


def sync_orderbook_snapshot(con, day: dt.date, start_symbol: str | None = None,
                            end_symbol: str | None = None, workers: int = 12,
                            **_) -> tuple[bool, int, str | None]:
    """使用 mootdx 抓取指定日五档盘口，并通过当前连接串行写入。

    旧实现无条件返回成功但不采集，导致数据长期滞后。这里复用项目根目录
    fetch_orderbook_daily.py 的单股抓取函数，避免采集器另开 DuckDB 写连接产生锁冲突。
    """
    if day != _today():
        return False, 0, f"mootdx 仅能采集实时盘口，不能补采历史日期 {day}"

    try:
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
        from fetch_orderbook_daily import fetch_one
    except Exception as e:
        return False, 0, f"盘口采集器不可用: {type(e).__name__}: {e}"

    syms = [s for s, _ in _list_active_symbols(con, start_symbol, end_symbol)]
    existing = {
        str(r[0]).zfill(6) for r in con.execute(
            "SELECT symbol FROM orderbook_snapshot WHERE snapshot_date=?", [day]
        ).fetchall()
    }
    todo = [s for s in syms if s not in existing]
    if not todo:
        return True, 0, f"当日盘口已覆盖 {len(existing)} 只"

    records: list[dict] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 20))) as exe:
        futs = {exe.submit(fetch_one, s): s for s in todo}
        for idx, fut in enumerate(as_completed(futs), 1):
            try:
                rec = fut.result(timeout=20)
                if rec:
                    rec["snapshot_date"] = day
                    rec["ingested_at"] = dt.datetime.now()
                    records.append(rec)
                else:
                    failed += 1
            except Exception:
                failed += 1
            if len(records) >= 200:
                _upsert(con, "orderbook_snapshot", pd.DataFrame(records),
                        conflict_keys=["symbol", "snapshot_date"])
                records.clear()
            if idx % 500 == 0:
                _log(f"  orderbook progress={idx}/{len(todo)} failed={failed}")

    written = 0
    # _upsert 返回尝试写入数；分批写入后的实际覆盖数用数据库差值核验。
    if records:
        _upsert(con, "orderbook_snapshot", pd.DataFrame(records),
                conflict_keys=["symbol", "snapshot_date"])
    after = con.execute(
        "SELECT COUNT(DISTINCT symbol) FROM orderbook_snapshot WHERE snapshot_date=?", [day]
    ).fetchone()[0]
    written = max(0, int(after) - len(existing))
    if written == 0:
        return False, 0, f"盘口源返回 0 行 (todo={len(todo)}, failed={failed})"
    return True, written, f"覆盖={after}/{len(syms)}, failed={failed}"


def sync_block_trade(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "block_trade 由 dzjy_daily 间接覆盖"


def sync_financials(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    # 财报按报告期发布, 收盘后不主动拉取
    return True, 0, "financials 仅财报披露日更新"


def sync_announcements(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "announcements 由 stock_notices 覆盖"


def sync_news(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "news 由 stock_news 覆盖"


def sync_stock_news(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    import akshare as ak  # noqa: F401
    try:
        df = _safe_ak_call("stock_news_em", symbol="全部A股")
    except Exception as e:
        return False, 0, str(e)
    if df is None or df.empty:
        return False, 0, "stock_news 返回空"
    cur = con.execute("PRAGMA table_info('stock_news')").fetchall()
    cols = [c[1] for c in cur]
    pdf = df.copy()
    for c in cols:
        if c not in pdf.columns:
            pdf[c] = None
    pdf = pdf[cols]
    n = _upsert(con, "stock_news", pdf)
    return True, n, None


def sync_stock_notices(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "stock_notices 公告需按股票拉取, 见 daily selectors"


def sync_news_cctv(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "news_cctv 外部信源, 由 cctv 接口独立维护"


def sync_dividends(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "dividends 按报告期更新"


def sync_repurchases(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "repurchases 按公告更新"


def sync_earnings_forecasts(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "earnings_forecasts 按报告期更新"


def sync_economic_events(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "economic_events 按需手动维护"


def sync_share_structure(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "share_structure 按报告期更新"


def sync_shareholder_changes(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "shareholder_changes 按报告期更新"


def sync_corporate_actions(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "corporate_actions 按公告更新"


def sync_locked_shares(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "locked_shares 按报告期更新"


def sync_restricted_releases(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "restricted_releases 按报告期更新"


def sync_minute_bars(con, day: dt.date, **_) -> tuple[bool, int, str | None]:
    return True, 0, "minute_bars 由盘中引擎维护"


def _is_lock_error(e: Exception) -> bool:
    """识别 DuckDB 文件被外部进程(收盘管道/数据更新.exe)独占持锁. 与
    premarket_healthcheck._duckdb_locked 同规则; 锁冲突属时序性, 应重试而非整步失败."""
    t = str(e).lower()
    if "cannot open file" not in t or ".duckdb" not in t:
        return False
    return any(m in t for m in ("正在使用", "another program", "in use",
                                "locked", "already open", "busy",
                                "sharing violation", "process cannot access"))


def _connect_write_retry(max_tries: int = 6, base_wait: float = 20.0):
    """打开 DuckDB 写连接; 遇文件锁(外部进程写库)按指数退避重试.

    [m4] DuckDB 已退役/文件缺失时直接抛 FileNotFoundError (严禁 duckdb.connect
    自动重建空库文件), 由 update_all 捕获并优雅降级到 h5i 主写.
    """
    import time as _t
    if not os.path.exists(DUCKDB_PATH):
        raise FileNotFoundError(f"DuckDB 已退役/不存在: {DUCKDB_PATH}")
    last: Exception | None = None
    for k in range(max_tries):
        try:
            return duckdb.connect(DUCKDB_PATH)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_lock_error(e) or k == max_tries - 1:
                break
            _log(f"  DuckDB 文件被外部进程持锁(第{k + 1}次), {base_wait * (k + 1):.0f}s 后重试...")
            _t.sleep(base_wait * (k + 1))
    raise last  # type: ignore[misc]


# ============================================================
# 主流程: 按 DB_UPDATE_TARGETS 顺序执行, 每表独立异常处理
# ============================================================
def update_all(day: dt.date | None = None, only: list[str] | None = None,
               start_symbol: str | None = None,
               end_symbol: str | None = None) -> dict:
    """返回 {table: {ok, rows, error}}. start_symbol/end_symbol 用于分段续传.

    [m4] DuckDB 退役后 update_all 优雅降级: duck 不可用时逐表标记降级 (不抛错,
    不阻断主流程); daily_bars 的新增整日数据改由 free_stockdb_sync ->
    h5i_sync.append_daily_bars (h5i 主写) 负责, 无需 duck.
    """
    day = day or _today()
    result: dict[str, dict] = {}
    try:
        con = _connect_write_retry()
    except Exception as e:  # noqa: BLE001
        _log(f"DuckDB 不可用 ({type(e).__name__}), update_all 降级跳过写库: {str(e)[:200]}")
        targets = list(DB_UPDATE_TARGETS.items())
        if only:
            targets = [(k, v) for k, v in targets if k in only]
        for table, _meta in targets:
            result[table] = {"ok": False, "rows": 0,
                             "error": "DuckDB 已退役/不可用, 表级写入降级跳过 "
                                      "(daily_bars 由 free_stockdb_sync -> h5i 主写)"}
        result["__akshare_stats__"] = _safe_ak_status()
        return result
    try:
        targets = list(DB_UPDATE_TARGETS.items())
        if only:
            targets = [(k, v) for k, v in targets if k in only]
        _log(f"update_db 启动 day={day} tables={len(targets)} "
             f"range=[{start_symbol or '-'} ~ {end_symbol or '-'}]")
        for table, _meta in targets:
            fn_name = DB_UPDATE_TARGETS[table][2]
            fn = globals().get(fn_name)
            if fn is None:
                result[table] = {"ok": False, "rows": 0, "error": f"{fn_name} 未实现"}
                continue
            t0 = time.time()
            try:
                ok_, rows, err = fn(con, day,
                                    start_symbol=start_symbol,
                                    end_symbol=end_symbol)
                _write_sync_status(con, table, ok_, rows, day, err)
                result[table] = {"ok": bool(ok_), "rows": int(rows), "error": err}
                _log(f"  [{table}] ok={ok_} rows={rows} elapsed={time.time() - t0:.1f}s "
                     f"err={(err or '')[:80]}")
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc(limit=2)
                _write_sync_status(con, table, False, 0, day, str(e))
                result[table] = {"ok": False, "rows": 0, "error": str(e)[:300]}
                _log(f"  [{table}] EXC {type(e).__name__}: {e}\n{tb}")
    finally:
        con.close()
    # 顶层汇总 AKShare 调用统计 (供 run_daily.db_update 步骤输出)
    result["__akshare_stats__"] = _safe_ak_status()
    return result


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="同步日期 YYYY-MM-DD, 默认今天")
    ap.add_argument("--days", type=int, default=1, help="回溯天数 (含 --day)")
    ap.add_argument("--only", nargs="*", help="仅同步指定表")
    ap.add_argument("--start-symbol", help="起始 symbol6, 含 (含续传)")
    ap.add_argument("--end-symbol", help="结束 symbol6, 含 (分段)")
    args = ap.parse_args()
    end_day = dt.datetime.strptime(args.day, "%Y-%m-%d").date() if args.day else _today()
    overall: dict[str, dict] = {}
    for offset in range(args.days - 1, -1, -1):
        d = end_day - dt.timedelta(days=offset)
        result = update_all(day=d, only=args.only,
                            start_symbol=args.start_symbol,
                            end_symbol=args.end_symbol)
        for t, r in result.items():
            overall.setdefault(t, []).append({"date": d.isoformat(), **r})
    print(json.dumps(overall, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()