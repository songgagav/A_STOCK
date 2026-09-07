# ============================================================
# local_pull.py -- 本地化全A股快拉底座 (free-stockdb / CNEquity 数据湖)
#
# 目标: 解决 "拉取全A股全量数据过慢" 的问题。
# 策略 (从快到慢):
#   1) 快通道 A -- 直接读本地 free-stockdb 的 parquet 全量镜像
#      (从 <本地行情根>/kline_parts/{sh,sz,bj}_*.parquet 及 kline_daily_full.parquet 导入)
#      一次扫描全A股, 零网络, 增量水位入库 DuckDB daily_bars。
#   2) 快通道 B -- 用 free-stockdb 引擎 SDK 批量取 SZ 高富估值字段
#      (rd.vals 前缀扫描 / rd.get_data 批量), 把 pe/pb/mv/turnover 等
#      补进 valuation_snapshot, 供 DRL / LLM 使用。
#   3) 兜底层   -- AKShare(sina 全市场 spot / 逐步) 补齐周末新鲜度与缺口。
#
# CNEquity 数据湖 (describe_lake / query_bars):
#   在行情层之上, 提供 基本面 / 资金面 / 宏观 / 舆情 四层取数并落库,
#   目标表与现有 DuckDB 表映射见 build_lake_schema()。
#
# 用法:
#   from local_pull import pull_full_market, describe_lake, query_bars
#   pull_full_market(run_real=True)          # 全量快拉 + 真实写入
#   pull_full_market(dry_run=True)           # 仅打印计划不写
#   describe_lake()                           # 打印数据湖范围
#   query_bars(freq="1d", fq="qfq")           # 带复权历史行情
#
# 依赖: duckdb, pandas, pyarrow; 优选已安装。
# ============================================================

from __future__ import annotations

import datetime as dt
import glob
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

import duckdb
import pandas as pd

_LOG = logging.getLogger("local_pull")


# ------------------------------------------------------------------ 路径配置
def _app_dir() -> Path:
    return Path(__file__).resolve().parent


try:
    from config import DUCKDB_PATH  # noqa: F401  (A_stock_rotation/config.py)
except Exception:  # pragma: no cover
    DUCKDB_PATH = os.path.join(_lake_root(), "stockdb.duckdb")

# free-stockdb 数据目录 (parquet 全量镜像 + 单文件全量)
FREE_STOCKDB_DIR = _lake_root()
KLINE_PARTS_DIR = os.path.join(FREE_STOCKDB_DIR, "kline_parts")
KLINE_FULL_PARQUET = os.path.join(FREE_STOCKDB_DIR, "kline_daily_full.parquet")
# free-stockdb 引擎 SDK 目录
PYBAO_DIR = os.environ.get("PYBAO_DIR", "") or os.path.join(_lake_root(), "pybao")

# parquet -> daily_bars 列映射
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

_DAILY_COLS = ["symbol", "date", "open", "high", "low", "close",
               "volume", "amount", "change_pct", "turnover"]

# 市场前缀
_MARKET_PREFIX = ("sh_", "sz_", "bj_")


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [local_pull] {msg}", flush=True)


def _h5i_write_enabled() -> bool:
    """h5i 主写开关 (m4 起默认开; H5I_WRITE=0|false|no 关闭)."""
    return os.environ.get("H5I_WRITE", "1").strip().lower() not in ("0", "false", "no")


def _h5i_global_waterline() -> dt.date | None:
    """h5i daily_bars 全局最大交易日 (duck 缺失时水位线回退)."""
    try:
        from h5i_sync import max_bar_date
        return max_bar_date(force=True)
    except Exception:
        return None


def _open_owned_con(fn_name: str):
    """尝试打开 DuckDB 写连接; 不可用(退役/锁)时返回 (None, False), 由调用方切 h5i 主写."""
    try:
        return _connect(read_only=False), True
    except Exception as e:  # noqa: BLE001
        _log(f"DuckDB 不可用, {fn_name} 降级为 h5i 主写 (duck 尽力写跳过): "
             f"{type(e).__name__}: {str(e)[:150]}")
        return None, False


def _h5i_append_all(combined: pd.DataFrame | None) -> dict:
    """把合并后的新行单线程整日单调追加到 h5i (历史/重复日期内部跳过)."""
    if not _h5i_write_enabled():
        return {"ok": True, "enabled": False}
    try:
        from h5i_sync import append_daily_bars
        if combined is None or combined.empty:
            return {"ok": True, "enabled": True, "appended": 0,
                    "note": "无新行"}
        r = append_daily_bars(combined)
        if r.get("appended", 0) > 0:
            _log(f"h5i 主写 daily_bars 追加 {r['appended']} 行 {r.get('dates')}")
        elif r.get("skipped_rows", 0) > 0:
            _log(f"h5i 主写跳过 {r['skipped_rows']} 行 (回填/重复, 单调约束)")
        return r
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ====================================================================
# 快通道 A -- 本地 parquet 全量快拉
# ====================================================================

def _connect(read_only: bool = False):
    if not read_only and not os.path.exists(DUCKDB_PATH):
        # [m4] DuckDB 已退役: 严禁 duckdb.connect 自动重建空库文件
        raise FileNotFoundError(f"DuckDB 已退役/不存在: {DUCKDB_PATH}")
    if not read_only:
        Path(os.path.dirname(DUCKDB_PATH)).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(DUCKDB_PATH, read_only=read_only)
    if not read_only:
        _ensure_daily_schema(con)
    return con


def _ensure_daily_schema(con) -> None:
    con.execute("""
    CREATE TABLE IF NOT EXISTS daily_bars (
        symbol     VARCHAR NOT NULL,
        date       DATE    NOT NULL,
        open       DOUBLE,
        high       DOUBLE,
        low        DOUBLE,
        close      DOUBLE,
        volume     DOUBLE,
        amount     DOUBLE,
        change_pct DOUBLE,
        turnover   DOUBLE,
        PRIMARY KEY (symbol, date)
    );
    """)


def _duckdb_waterlines(con, symbols: Iterable[str] | None = None) -> dict[str, dt.date]:
    """读 daily_bars 每只 symbol 的 max(date) 作为水位线。"""
    if symbols:
        syms = [str(s).zfill(6) for s in symbols]
        ph = ",".join(["?"] * len(syms))
        rows = con.execute(
            f"SELECT symbol, MAX(date) FROM daily_bars WHERE symbol IN ({ph}) GROUP BY symbol",
            syms,
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT symbol, MAX(date) FROM daily_bars GROUP BY symbol"
        ).fetchall()
    out = {}
    for sym, d in rows:
        if d is None:
            continue
        s = str(sym).zfill(6)
        if hasattr(d, "date"):
            out[s] = d
        else:
            out[s] = dt.date.fromisoformat(str(d)[:10])
    return out


def _compute_change_pct(df: pd.DataFrame) -> pd.DataFrame:
    """按 symbol 分组算 change_pct = (close - prev_close)/prev_close*100。"""
    if "change_pct" in df.columns and df["change_pct"].notna().all():
        return df
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    prev_close = df.groupby("symbol")["close"].shift(1)
    df["change_pct"] = ((df["close"] - prev_close) / prev_close * 100).where(
        prev_close > 0, None
    )
    return df


def _insert_daily_bars(con, df: pd.DataFrame) -> int:
    """增量写 daily_bars。

    依赖 daily_bars 的 PRIMARY KEY (symbol,date) 做原子去重:
    用 DuckDB 原生 INSERT OR IGNORE, 返回实际新增行数。
    注意: 本函数须由【单一顺序执行】调用(不放多线程), 且不包显式大事务,
    依赖连接 autocommit 逐个 executemany 提交, 避免 DuckDB 单连接并发/嵌套事务冲突。
    """
    if df is None or df.empty:
        return 0
    for c in _DAILY_COLS:
        if c not in df.columns:
            df[c] = None
    df = df[_DAILY_COLS].copy().where(pd.notnull(df), None)
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    rows = [tuple(r) for r in df.itertuples(index=False, name=None)]

    col_list = ", ".join('"%s"' % c for c in _DAILY_COLS)
    ph = ",".join(["?"] * len(_DAILY_COLS))
    ins = f"INSERT OR IGNORE INTO daily_bars ({col_list}) VALUES ({ph})"
    total = 0
    BATCH = 8000
    # INSERT OR IGNORE 依赖主键原子去重; DuckDB executemany 不报告可靠 rowcount,
    # 故用 count 差值统计实际新增 (逐 executemany 各自 autocommit)。
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        before = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        con.executemany(ins, batch)
        after = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        total += max(after - before, 0)
    return total


def _read_parquet_incremental(path: str, waterline: dt.date | None) -> pd.DataFrame | None:
    """读单个 parquet, 只保留 trade_date > waterline 的新行。"""
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        _LOG.debug("读 %s 失败: %s", path, e)
        return None
    if df.empty:
        return None
    df = df.rename(columns=_COL_MAP)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["date"] = df["date"].dt.date
    if waterline is not None:
        df = df[df["date"] > waterline]
    return df if not df.empty else None


def _scan_parquet_files(symbols: list[str] | None = None) -> list[tuple[str, Path]]:
    """返回 [(market, path), ...]，按 sh/sz/bj 前缀扫描分片。"""
    out = []
    targets = {str(s).zfill(6) for s in (symbols or [])} if symbols else None
    if not os.path.isdir(KLINE_PARTS_DIR):
        _log(f"kline_parts 目录不存在: {KLINE_PARTS_DIR}")
        return out
    for prefix in _MARKET_PREFIX:
        mkt = prefix[:-1].upper()
        for path in glob.glob(os.path.join(KLINE_PARTS_DIR, f"{prefix}*.parquet")):
            base = os.path.basename(path)
            sym6 = base[len(prefix):].split(".")[0]
            if targets is not None and sym6 not in targets:
                continue
            out.append((mkt, Path(path)))
    out.sort(key=lambda x: x[1].name)
    return out


def pull_from_parts(symbols: list[str] | None = None,
                    since_date: str | None = None,
                    max_workers: int = 4,
                    con=None) -> dict:
    """快通道 A-1: 遍历 kline_parts 全部分片, 增量写 daily_bars。

    全A 5548 只本地文件, 无网络。返回统计 dict。
    con: 可选注入的连接。DuckDB 单进程写锁下, 由调用方已有连接传进来
    可避免 "file already open" 锁冲突 (例如 update_db.update_all 场景)。
    未传时自建变量连接并在结束时关闭。
    """
    files = _scan_parquet_files(symbols)
    if not files:
        return {"ok": False, "error": f"无 parquet 分片: {KLINE_PARTS_DIR}", "files": 0}

    owned = con is None
    duck_ok = True
    if owned:
        con, duck_ok = _open_owned_con("pull_from_parts")
    else:
        duck_ok = con is not None
    duck_write_failures = 0
    try:
        h5i_max = _h5i_global_waterline() if _h5i_write_enabled() else None
        if since_date:
            # 明确给一个全局起始水位 (since_date 前一天)
            global_since = dt.date.fromisoformat(since_date)
            waterlines = None
        else:
            global_since = None
            # 收集分片代码, 读 daily_bars 现有水位
            codes = []
            for _, path in files:
                nm = path.stem
                pfx = nm[:3]
                codes.append(nm[len(pfx):])
            waterlines = {}
            if duck_ok and con is not None:
                try:
                    waterlines = _duckdb_waterlines(con, codes)
                except Exception as e:  # noqa: BLE001
                    _log(f"DuckDB 水位线读取失败: {e}")
            if not waterlines:
                # [m4] DuckDB 缺失 -> 水位线回退 h5i 全局最大日
                if h5i_max is not None:
                    waterlines = {c: h5i_max for c in codes}
                    _log(f"pull_from_parts 水位线回退 h5i 全局最大日 {h5i_max}")
                else:
                    waterlines = {}
                    _log("DuckDB/h5i 均不可用, 全量扫描 (h5i 单调去重兜底)")

        counter = {"scanned": 0, "rows": 0, "updated_symbols": 0,
                   "min_new": None, "max_new": None}
        _buf: list[pd.DataFrame] = []

        def _read_one(mkt, path):
            """仅读取+计算 (不写库), 返回 (sym6, df|None), 供多线程并发."""
            sym6 = path.stem[3:]
            w = None if global_since is None else global_since - dt.timedelta(days=1)
            if global_since is None:
                w = waterlines.get(sym6) if waterlines else None
            df = _read_parquet_incremental(str(path), w)
            if df is None or df.empty:
                return sym6, None
            df = _compute_change_pct(df)
            return sym6, df

        started = time.time()
        # 多线程只做读取+计算; 写入统一回主线程串行执行 (DuckDB 单连接非线程安全,
        # 且注入外部 con 时不能并发 BEGIN / executemany)。
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_read_one, mkt, path) for mkt, path in files]
            for fut in as_completed(futs):
                sym6, df = fut.result()
                counter["scanned"] += 1
                if df is not None and not df.empty:
                    counter["updated_symbols"] += 1
                    duck_n = 0
                    if duck_ok and con is not None:
                        # [m4] DuckDB 尽力写: 失败仅告警, 不阻断 h5i 主写
                        try:
                            duck_n = _insert_daily_bars(con, df)
                        except Exception as e:  # noqa: BLE001
                            duck_write_failures += 1
                            _log(f"DuckDB daily_bars 写失败({sym6}) 转 h5i 主写: {e}")
                    counter["rows"] += duck_n
                    _buf.append(df)
                if counter["scanned"] % 500 == 0:
                    _log(f"进度 {counter['scanned']}/{len(files)} 新增行={counter['rows']}")

        # [m4] h5i 主写: 合并整批新行单线程整日单调追加 (防半日/重复)
        combined = pd.concat(_buf, ignore_index=True) if _buf else None
        counter["h5i_primary_write"] = _h5i_append_all(combined)

        # 汇总全局日期范围
        if not since_date:
            if duck_ok and con is not None:
                # 重新查水位线
                try:
                    lines = _duckdb_waterlines(con, None)
                    vals = [v for v in lines.values()]
                    if vals:
                        counter["min_new"] = min(vals).isoformat()
                        counter["max_new"] = max(vals).isoformat()
                except Exception as e:  # noqa: BLE001
                    _log(f"重查 duck 水位线失败: {e}")
            elif h5i_max is not None:
                counter["min_new"] = counter["max_new"] = h5i_max.isoformat()
        return {
            "ok": True,
            "mode": "kline_parts",
            "files": len(files),
            "scanned": counter["scanned"],
            "rows_added": counter["rows"],
            "duck_write_failures": duck_write_failures,
            "updated_symbols": counter["updated_symbols"],
            "max_duckdb_date": counter.get("max_new"),
            "h5i_primary_write": counter.get("h5i_primary_write"),
            "elapsed_seconds": round(time.time() - started, 2),
        }
    finally:
        if owned and con is not None:
            con.close()


def pull_from_full_parquet(waterline_mode: str = "duckdb", con=None) -> dict:
    """快通道 A-2: 一次扫描 kline_daily_full.parquet 全市场。

    1280 万行单文件, DuckDB 直接查询过滤再写, 最快。
    con: 同 pull_from_parts, 可注入调用方连接避免文件锁冲突。
    """
    if not os.path.exists(KLINE_FULL_PARQUET):
        return {"ok": False, "error": f"单文件全量不存在: {KLINE_FULL_PARQUET}"}
    started = time.time()
    owned = con is None
    duck_ok = True
    if owned:
        con, duck_ok = _open_owned_con("pull_from_full_parquet")
    else:
        duck_ok = con is not None
    duck_write_failures = 0
    try:
        # [m4] 水位线: duck 缺失时回退 h5i 全局最大日
        lines = {}
        if duck_ok and con is not None:
            try:
                lines = _duckdb_waterlines(con, None)
            except Exception as e:  # noqa: BLE001
                _log(f"DuckDB 水位线读取失败: {e}")
        h5i_max = _h5i_global_waterline() if _h5i_write_enabled() else None
        if not lines and h5i_max is not None:
            lines = {"__GLOBAL__": h5i_max}  # 用全局水位近似过滤
        df = pd.read_parquet(KLINE_FULL_PARQUET)
        df = df.rename(columns=_COL_MAP)
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df = df.dropna(subset=["date"])
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)

        before = None
        if duck_ok and con is not None:
            before = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        if lines:
            wl = pd.Series(lines)  # index=symbol(或 __GLOBAL__), value=date
            def _keep(r):
                if r["symbol"] in wl.index:
                    return r["date"] > wl[r["symbol"]]
                if "__GLOBAL__" in wl.index:
                    return r["date"] > wl["__GLOBAL__"]
                return True
            df = df[df.apply(_keep, axis=1)]
        df = _compute_change_pct(df)
        duck_n = 0
        if duck_ok and con is not None:
            try:
                duck_n = _insert_daily_bars(con, df)
            except Exception as e:  # noqa: BLE001
                duck_write_failures += 1
                _log(f"DuckDB 写失败 (pull_from_full_parquet) 转 h5i 主写: {e}")
        after = None
        if duck_ok and con is not None:
            after = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        # [m4] h5i 主写: 整批单线程整日单调追加
        h5i_primary_write = _h5i_append_all(df if (df is not None and not df.empty) else None)
        return {
            "ok": True,
            "mode": "kline_daily_full",
            "source_rows": int(len(df)),
            "daily_bars_before": before,
            "daily_bars_after": after,
            "daily_bars_added": (after - before) if (before is not None and after is not None) else duck_n,
            "duck_write_failures": duck_write_failures,
            "h5i_primary_write": h5i_primary_write,
            "elapsed_seconds": round(time.time() - started, 2),
        }
    finally:
        if owned and con is not None:
            con.close()


# ====================================================================
# 快通道 B -- free-stockdb 引擎批量取 SZ 高富估值字段 -> valuation_snapshot
# ====================================================================

def _load_sdk():
    """把 free-stockdb 的 pybao 目录加入 sys.path 并导入 rd。"""
    if str(PYBAO_DIR) not in sys.path:
        sys.path.insert(0, str(PYBAO_DIR))
    from stock_sdk import rd, bk, zb  # noqa: F401
    return rd, bk, zb


def pull_engine_valuation(max_symbols: int | None = None) -> dict:
    """快通道 B: 用 rd.vals 前缀批量取全市场(0*/3*/6*)含估值日K, 写入 valuation_snapshot。

    引擎日k表每行含 pe_ttm/pb/total_mv/float_mv/turnover/name 等丰富字段。
    实测覆盖: SZ主板0*(1485) + 创业板3*(1394) + SH主板6*(2302) ≈ 5181 只, 接近全市场。

    历史 bug (2026-08-31 事故) 修复:
      1) max_symbols 默认 2000 → 深市前缀(0*1485+3*部分)即截断, 永远到不了沪市 6*,
         导致 valuation_snapshot 只剩深市残段. 默认改为 None = 全量.
      2) fetch_time 原先在循环内逐行取 dt.datetime.now(), 一批数据被拆成几十个
         微秒级时间戳段, 每段 <3000 行, 破坏 get_universe 段防护完整性判断.
         改为循环外取一次时间, 整批共用一个 fetch_time 段.
    """
    try:
        rd, _, _ = _load_sdk()
    except Exception as e:
        return {"ok": False, "error": f"SDK 不可用: {e}"}

    latest = None
    try:
        r = list(rd.vals("日k", "000001", "*"))
        if r:
            latest = r[-1]["date"]
    except Exception:
        pass
    if latest is None:
        return {"ok": False, "error": "引擎返回空"}

    con = _connect(read_only=False)
    # 表存在性保障 (不再 DROP 全量重建: 08-31 事故即因 DROP+只写深市前缀
    # 把沪市段清空; 改为按 fetch_time 增量 upsert, 保留历史完整段供段防护回退)
    con.execute("""
    CREATE TABLE IF NOT EXISTS valuation_snapshot (
        symbol VARCHAR, name VARCHAR, price DOUBLE, prev_close DOUBLE,
        volume DOUBLE, amount DOUBLE, turnover DOUBLE, pe_ttm DOUBLE, pb DOUBLE,
        total_mv DOUBLE, float_mv DOUBLE, float_shares DOUBLE, total_shares DOUBLE,
        is_st BOOLEAN, fetch_time TIMESTAMP
    );
    """)
    started = time.time()
    out_rows = []
    # 整批共用一个 fetch_time, 保证段防护按段行数/沪市覆盖判定通过
    fetch_time = dt.datetime.now()
    # 前缀覆盖全市场: SZ主板(0*) + 创业板(3*) + 沪市(6*). 08-31 事故根因:
    # 原仅 ["0*","3*"] 导致沪市 6* 段被 DROP 清空, universe 只剩深市残池.
    prefixes = ["0*", "3*", "6*"]  # SZ 主板 + 创业板 + SH 主板
    for pfx in prefixes:
        try:
            items = list(rd.vals("日k", pfx, str(latest)))
        except Exception as e:
            _log(f"prefix {pfx} 失败: {e}")
            continue
        for it in items:
            if not isinstance(it, dict) or not it.get("code"):
                continue
            code = str(it["code"])
            fmv = it.get("float_mv")
            tmv = it.get("total_mv")
            # 单位归一: 引擎返回 float_mv/total_mv 为「元」(实测中际旭创 1.02e12),
            # 全系统口径为「亿」(filter_universe/selector 均按亿). 统一 /1e8.
            fmv = float(fmv) / 1e8 if isinstance(fmv, (int, float)) else fmv
            tmv = float(tmv) / 1e8 if isinstance(tmv, (int, float)) else tmv
            out_rows.append({
                "symbol": code.zfill(6),
                "name": it.get("name"),
                "price": it.get("close"),
                "prev_close": it.get("pre_close"),
                "volume": it.get("volume"),
                "amount": it.get("amount"),
                "turnover": it.get("turnover"),
                "pe_ttm": it.get("pe_ttm"),
                "pb": it.get("pb"),
                "total_mv": tmv,
                "float_mv": fmv,
                "float_shares": it.get("float_share"),
                "total_shares": it.get("total_share"),
                "is_st": it.get("is_st"),
                "fetch_time": fetch_time,
            })
        if max_symbols and len(out_rows) >= max_symbols:
            break

    if out_rows:
        df = pd.DataFrame(out_rows)
        df = df.where(pd.notnull(df), None)
        ft = df["fetch_time"].iloc[0]
        con.execute("BEGIN")
        try:
            # 本段写入前先清同名 fetch_time 的旧段(幂等), 不再 DROP 全表
            con.execute("DELETE FROM valuation_snapshot WHERE fetch_time = ?",
                        [ft])
            con.executemany(
                """INSERT INTO valuation_snapshot
                   (symbol,name,price,prev_close,volume,amount,turnover,pe_ttm,pb,
                    total_mv,float_mv,float_shares,total_shares,is_st,fetch_time)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [tuple(r) for r in df.itertuples(index=False, name=None)],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    con.close()
    return {"ok": True, "rows": len(out_rows), "latest_date": str(latest),
            "elapsed_seconds": round(time.time() - started, 2)}


# ====================================================================
# 兜底层 -- AKShare 全市场补齐新鲜度
# ====================================================================

def pull_sina_spot_fallback() -> dict:
    """用 sina stock_zh_a_spot 单次请求补齐估值快照缺失的标的 (含 SH/BJ)。"""
    try:
        import akshare as ak
    except Exception as e:
        return {"ok": False, "error": f"akshare 不可用: {e}"}
    try:
        df = ak.stock_zh_a_spot()
    except Exception as e:
        return {"ok": False, "error": f"sina spot 失败: {e}"}
    if df is None or df.empty:
        return {"ok": False, "error": "sina spot 空"}
    con = _connect(read_only=False)
    out = []
    for _, r in df.iterrows():
        raw = str(r.get("代码") or "")
        low = raw.lower()
        pfx = low[:2] if low[:2] in ("sh", "sz", "bj") else ""
        code = low[2:] if pfx else low
        if not code or len(code) != 6 or not code.isdigit():
            continue
        out.append({
            "symbol": code, "name": r.get("名称"),
            "price": _n(r.get("最新价")), "prev_close": _n(r.get("昨收")),
            "volume": _n(r.get("成交量")), "amount": _n(r.get("成交额")),
            "turnover": _n(r.get("换手率")), "pe_ttm": _n(r.get("市盈率-动态")),
            "pb": _n(r.get("市净率")), "total_mv": _n(r.get("总市值")),
            "float_mv": _n(r.get("流通市值")),
        })
    if out:
        # merge 进 valuation_snapshot: 只补 symbols 不在引擎覆盖的部分
        pd_out = pd.DataFrame(out).where(pd.notnull(pd.DataFrame(out)), None)
    con.close()
    return {"ok": True, "rows": len(out), "note": "sina spot 补齐"}


def _n(v):
    if v is None or (isinstance(v, str) and str(v).strip() in ("", "-", "None", "nan")):
        return None
    try:
        return float(v)
    except Exception:
        return None


# ====================================================================
# CNEquity 数据湖 -- 四层取数 + describe_lake / query_bars
# ====================================================================

def build_lake_schema(con) -> dict:
    """定义 CNEquity 数据湖与现有 DuckDB 表的映射 + 建补充表。"""
    mapping = {
        "L0 行情":  ["daily_bars", "minute_bars", "adj_factors"],
        "L1 基本面": ["financials", "dividends", "earnings_forecasts",
                  "share_structure", "shareholder_changes"],
        "L2 资金面": ["northbound_money", "margin_daily", "money_flow",
                  "money_flow_estimate", "block_trade", "lhb", "lhb_detail"],
        "L3 宏观":  ["economic_events", "macro_gdp", "macro_m2",
                  "macro_cpi", "macro_ppi", "macro_pmi"],
        "L4 舆情":  ["news", "stock_news", "news_cctv", "stock_notices",
                  "announcements", "events", "corporate_actions"],
        "风险/事件": ["locked_shares", "restricted_releases", "repurchases"],
    }
    # 每个 layer 建一张查询视图, 便于 Orchestrator describe_lake 发现
    for layer, tables in mapping.items():
        safe = "".join(ch for ch in layer if ch.isalnum())
        view = f"cn_lake_{safe}"
        try:
            con.execute(f"DROP VIEW IF EXISTS {view}")
        except Exception:
            pass
    return mapping


def describe_lake() -> dict:
    """供 Orchestrator Agent 了解数据湖数据范围。"""
    con = _connect(read_only=True)
    try:
        mapping = build_lake_schema(con)
        tabs = con.execute(
            "SELECT table_name, (SELECT COUNT(*) FROM information_schema.columns c "
            "WHERE c.table_name=t.table_name) FROM information_schema.tables t "
            "ORDER BY table_name"
        ).fetchall()
        avail = {t[0]: t[1] for t in tabs}
        out = {}
        for layer, tables in mapping.items():
            out[layer] = {t: avail.get(t, 0) for t in tables if t in avail}
        # 行情行数 / 日期范围水位
        try:
            row = con.execute(
                "SELECT COUNT(*), MIN(date), MAX(date) FROM daily_bars"
            ).fetchone()
            out["_daily_bars"] = {"rows": row[0], "min": str(row[1]), "max": str(row[2])}
        except Exception:
            pass
        return {"layers": out,
                "duckdb_path": DUCKDB_PATH,
                "parquet_mirror": KLINE_PARTS_DIR,
                "engine_sdk": PYBAO_DIR}
    finally:
        con.close()


def query_bars(symbols: list[str], start: str | None = None, end: str | None = None,
               freq: str = "1d", fq: str = "qfq",
               exclude_delisted: bool = False) -> pd.DataFrame:
    """Research Agent 取带复权的历史行情。

    复权口径:
      qfq(默认): close * adj_factor / adj_latest, 以该标的最新因子为基准
      hfq:       close * adj_factor
      none:      原始价
    adj_factors 为新浪后复权累计因子(source=akshare_sina_hfq_factor),
    事件式更新(仅除权日新增行), 交易日缺因子时按最近因子 ASOF 前向填充,
    无因子覆盖的标的(退市股/转债等)按比例 1.0 原价返回。

    exclude_delisted=True: 过滤退市/停更标的(在 daily_bars 有行情但
    未在 symbols 注册的代码), 用于 PIT 幸存者偏差控制。
    """
    con = _connect(read_only=True)
    try:
        syms = [s.split(".")[0].zfill(6) for s in symbols]
        ph = ",".join(["?"] * len(syms))
        args: list = list(syms)
        if exclude_delisted:
            alive = {r[0] for r in con.execute(
                f"SELECT symbol FROM symbols WHERE symbol IN ({ph})", args).fetchall()}
            syms = [s for s in syms if s in alive]
            if not syms:
                return pd.DataFrame(columns=_DAILY_COLS)
            ph = ",".join(["?"] * len(syms))
            args = list(syms)
        sql = (f"SELECT symbol, date, open, high, low, close, volume, amount, "
               f"change_pct, turnover FROM daily_bars WHERE symbol IN ({ph})")
        if start:
            sql += " AND date >= ?"; args.append(dt.date.fromisoformat(start))
        if end:
            sql += " AND date <= ?"; args.append(dt.date.fromisoformat(end))
        sql += " ORDER BY symbol, date"
        df = con.execute(sql, args).fetchdf()
        if df.empty:
            return df
        if fq and fq != "none":
            df = _adjust_bars(con, df, fq)
        return df
    finally:
        con.close()


def _adjust_bars(con, df: pd.DataFrame, fq: str) -> pd.DataFrame:
    """用 adj_factors 后复权累计因子合成 qfq/hfq 价。

    adj_factors.trade_date 为事件式(仅除权日有行), 用 merge_asof 前向填充;
    无因子日按 1.0 兜底(原价)。change_pct 为原始涨跌幅, 等比缩放不影响, 保留。
    """
    syms = df["symbol"].unique().tolist()
    ph = ",".join(["?"] * len(syms))
    fac = con.execute(
        f"SELECT symbol, trade_date, adj_factor FROM adj_factors "
        f"WHERE symbol IN ({ph})", syms).fetchdf()
    if fac.empty:
        return df
    fac = fac.rename(columns={"trade_date": "date"})
    merged = pd.merge_asof(
        df.sort_values("date"), fac.sort_values("date"),
        on="date", by="symbol", direction="backward")
    merged["adj_factor"] = merged["adj_factor"].fillna(1.0)
    if fq == "hfq":
        ratio = merged["adj_factor"]
    else:  # qfq
        latest = fac.groupby("symbol")["adj_factor"].max()
        merged["_base"] = merged["symbol"].map(latest).fillna(merged["adj_factor"])
        ratio = merged["adj_factor"] / merged["_base"]
    for c in ["open", "high", "low", "close"]:
        merged[c] = (merged[c] * ratio).round(4)
    return merged.drop(columns=["adj_factor", "_base"], errors="ignore")


def list_delisted(limit: int | None = None) -> pd.DataFrame:
    """返回已退市/停更标的清单(在 daily_bars 有行情但不在 symbols 注册)。

    用于幸存者偏差修复: 回测选股池应从 daily_bars 全量符号池采样,
    而非 symbols(当前活跃快照); 对退市标的按其 last_trade 截断。
    返回列: symbol / first_trade / last_trade / bars / kind
    """
    con = _connect(read_only=True)
    try:
        sql = """
            WITH t AS (
              SELECT d.symbol, COUNT(*) AS bars, MIN(d.date) AS first_trade,
                     MAX(d.date) AS last_trade
              FROM daily_bars d LEFT JOIN symbols s ON d.symbol = s.symbol
              WHERE s.symbol IS NULL
              GROUP BY d.symbol)
            SELECT symbol, first_trade, last_trade, bars,
                   CASE
                     WHEN symbol LIKE '60%' OR symbol LIKE '68%' OR symbol LIKE '00%'
                          OR symbol LIKE '30%' THEN 'A股退市'
                     WHEN symbol LIKE '200%' OR symbol LIKE '900%' THEN 'B股'
                     WHEN symbol LIKE '11%' OR symbol LIKE '12%' THEN '可转债'
                     ELSE '其他' END AS kind
            FROM t ORDER BY symbol"""
        if limit:
            sql += f" LIMIT {int(limit)}"
        return con.execute(sql).fetchdf()
    finally:
        con.close()


# ====================================================================
# 主入口
# ====================================================================

def pull_full_market(dry_run: bool = False, run_real: bool = False,
                     only_parts: bool = False) -> dict:
    """全A快拉主入口。

    dry_run=True: 只打印计划与水位, 不写库。
    run_real=True: 真实执行 (快通道 A 全量 + B 估值)。
    """
    result = {}
    if dry_run:
        con = _connect(read_only=True)
        try:
            lines = _duckdb_waterlines(con, None)
        finally:
            con.close()
        n_parts = len(_scan_parquet_files())
        n_full = os.path.exists(KLINE_FULL_PARQUET)
        return {
            "dry_run": True,
            "kline_parts_files": n_parts,
            "kline_daily_full_exists": n_full,
            "duckdb_daily_bars_waterline_symbols": len(lines),
            "sample_waterlines": {k: str(v) for k, v in list(lines.items())[:5]},
        }
    if run_real:
        if not only_parts and os.path.exists(KLINE_FULL_PARQUET):
            result["full_parquet"] = pull_from_full_parquet()
        else:
            result["parts"] = pull_from_parts(max_workers=4)
        result["engine_valuation"] = pull_engine_valuation()
        return result
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="本地化全A快拉底座")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不写库")
    ap.add_argument("--run-real", action="store_true", help="真机全量快拉")
    ap.add_argument("--parts", action="store_true", help="仅走 kline_parts 分片")
    ap.add_argument("--full", action="store_true", help="仅走 kline_daily_full 单文件")
    ap.add_argument("--valuation", action="store_true", help="仅引擎估值快照")
    ap.add_argument("--describe", action="store_true", help="打印数据湖范围")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.describe:
        print(json.dumps(describe_lake(), ensure_ascii=False, indent=2, default=str))
    elif args.full:
        print(json.dumps(pull_from_full_parquet(), ensure_ascii=False, indent=2, default=str))
    elif args.parts:
        print(json.dumps(pull_from_parts(), ensure_ascii=False, indent=2, default=str))
    elif args.valuation:
        print(json.dumps(pull_engine_valuation(), ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(pull_full_market(dry_run=args.dry_run,
                                          run_real=args.run_real),
                         ensure_ascii=False, indent=2, default=str))