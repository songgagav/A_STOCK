# -*- coding: utf-8 -*-
"""h5i_sync: DuckDB daily_bars 写入链 -> h5i daily_bars 双写辅助 (m2 每日写入链)

由 free_stockdb_sync / local_pull / update_db 的 DuckDB daily_bars 写入点调用:
在 DuckDB 成功写入后, 把同一批新行按 (ts, symbol) 升序分块 append 到 h5i.
[m4] h5i 已成为 daily_bars 主写入目标: 无论 DuckDB 是否存在/可写, 都会把
"新增整日"数据 append 到 h5i; DuckDB 存在时仍尽力写 (对照保留).

开关: 环境变量 H5I_WRITE 默认 1 (开启); 设 H5I_WRITE=0 可临时关闭 h5i 写.

h5i 单调追加约束:
  - h5i_db.append 只允许追加 ts 晚于(严格大于) 库内现有最大 ts 的数据;
  - 对回填/历史日期(<= 现有最大 ts) 一律跳过并告警, 不破坏 h5i 单调性;
  - mirror_duck_to_h5i 从 DuckDB 读取 date > h5i 现有最大日期 的新行做追平.
"""
from __future__ import annotations

import datetime as dt
import os
import threading

import pandas as pd

_BASE = os.path.dirname(os.path.abspath(__file__))
H5I_PATH = os.path.join(_BASE, "data", "h5i", "market.db")
from config import DATA_DIR, DUCKDB_PATH  # noqa: E402

_DAILY_COLS = ["symbol", "date", "open", "high", "low", "close",
               "volume", "amount", "change_pct", "turnover"]
# h5i daily_bars 表列序 (含 ts; 由 h5i_ingest.SCHEMA 定义)
_H5I_ORDER = ["ts", "symbol", "open", "high", "low", "close",
              "volume", "amount", "change_pct", "turnover"]
_CHUNK_ROWS = 300_000
_LOCK = threading.Lock()
# 进程级缓存: h5i 当前最大 bar 日期 (仅在自身 append 成功后刷新; 其它进程写入由异常兜底)
_MAX_CACHE: dict = {"date": None, "queried": False}
_WARNED_DATES: set = set()


def h5i_write_enabled() -> bool:
    """是否启用 h5i 双写/主写 (H5I_WRITE, 默认开启; =0|false|no 关闭)."""
    return os.environ.get("H5I_WRITE", "1").strip().lower() not in ("0", "false", "no")


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [h5i_sync] {msg}", flush=True)


def _open_h5i():
    import h5i_db
    if not os.path.isdir(H5I_PATH):
        return None
    return h5i_db.Database(H5I_PATH)


def max_bar_date(force: bool = False) -> dt.date | None:
    """h5i daily_bars 现有最大 ts 的日期 (进程内缓存; force=True 强制重查)."""
    if not force and _MAX_CACHE["queried"]:
        return _MAX_CACHE["date"]
    db = _open_h5i()
    if db is None:
        _MAX_CACHE.update({"date": None, "queried": True})
        return None
    try:
        df = db.sql("SELECT MAX(CAST(ts AS DATE)) m FROM daily_bars").to_pandas()
        v = df.iloc[0, 0] if df is not None and len(df) else None
        d = None if v is None else (
            v.date() if isinstance(v, dt.datetime) else
            (v if isinstance(v, dt.date) else pd.Timestamp(v).date()))
        _MAX_CACHE.update({"date": d, "queried": True})
        return d
    except Exception as e:
        _log(f"读 h5i 最大日期失败: {e}")
        _MAX_CACHE.update({"date": None, "queried": True})
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


def _reset_cache() -> None:
    _MAX_CACHE.update({"date": None, "queried": False})


def _df_to_h5i_table(df: pd.DataFrame):
    """把 duck 风格 df(symbol/date/open...) 转成 h5i daily_bars 顺序的 arrow 表."""
    import numpy as np
    import pyarrow as pa
    part = df.copy()
    part["ts"] = pd.to_datetime(part["date"]).dt.normalize().values.astype("datetime64[us]")
    part["symbol"] = part["symbol"].astype(str).str.zfill(6)
    for c in ["open", "high", "low", "close", "volume", "amount",
              "change_pct", "turnover"]:
        if c in part.columns:
            part[c] = pd.to_numeric(part[c], errors="coerce")
        else:
            # 缺列补 float64 NaN (不是 None), 避免 pyarrow 推断成 Null 类型与
            # 建表 schema(Float64) 冲突导致 append 失败
            part[c] = np.nan
    part = part[_H5I_ORDER]
    table = pa.Table.from_pandas(part, preserve_index=False)
    # 确保 ts 为 timestamp(us), 与建表 schema 一致
    try:
        idx = table.schema.get_field_index("ts")
        arr = pa.compute.cast(table["ts"], pa.timestamp("us"))
        table = table.set_column(idx, "ts", arr)
    except Exception:
        pass
    return table


def append_daily_bars(df: pd.DataFrame) -> dict:
    """把一批 duck 风格 daily_bars 行按 h5i 单调约束 append.

    只追加 date > h5i 现有最大日期 的新行; 其余(<= max) 为回填/历史日期, 跳过并告警.
    返回 {enabled, h5i_max_date, appended, skipped_rows, dates}.
    """
    base = {"enabled": h5i_write_enabled(), "appended": 0, "skipped_rows": 0,
            "dates": [], "ok": True}
    if not base["enabled"]:
        return base
    if df is None or df.empty:
        return base
    if "date" not in df.columns:
        base.update({"ok": False, "error": "df 缺 date 列"})
        return base
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])
    if work.empty:
        return base
    work["_d"] = work["date"].dt.date
    m = max_bar_date()
    if m is None:
        # h5i 库/表不可用或为空: 无法满足单调追加(建表/历史重建走 h5i_ingest/h5i_rebuild)
        base.update({"ok": False, "error": "h5i daily_bars 不存在或为空, 跳过双写 "
                     "(请先用 h5i_ingest/h5i_rebuild 建库)"})
        return base
    old = work[work["_d"] <= m]
    new = work[work["_d"] > m]
    skipped = int(len(old))
    if skipped:
        for d in sorted(old["_d"].unique()):
            if d in _WARNED_DATES:
                continue
            _WARNED_DATES.add(d)
            _log(f"跳过回填日期 {d} (<= h5i 现有最大 {m}), 不破坏 h5i 单调性")
    base["skipped_rows"] = skipped
    if new.empty:
        return base
    new = new.sort_values(["_d", "symbol"]).reset_index(drop=True)
    with _LOCK:
        db = _open_h5i()
        if db is None:
            base.update({"ok": False, "error": "h5i 库不可用"})
            return base
        try:
            appended = 0
            for i in range(0, len(new), _CHUNK_ROWS):
                chunk = new.iloc[i:i + _CHUNK_ROWS]
                table = _df_to_h5i_table(chunk)
                db.append("daily_bars", table)
                appended += len(chunk)
            base["appended"] = appended
            base["dates"] = sorted(str(d) for d in new["_d"].unique())
        except Exception as e:
            base.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
            _log(f"append h5i daily_bars 失败: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass
    if base["ok"] and appended:
        _reset_cache()  # 下次 max_bar_date 自动重查新水位
    return base


def mirror_duck_to_h5i() -> dict:
    """追平: 读 DuckDB daily_bars 中 date > h5i 现有最大日期 的新行并分块 append.

    调用前提: DuckDB daily_bars 写入已成功提交. 仅在 H5I_WRITE=1 时执行.
    """
    base = {"enabled": h5i_write_enabled(), "appended": 0, "read_rows": 0,
            "skipped_rows": 0, "dates": [], "ok": True}
    if not base["enabled"]:
        return base
    m = max_bar_date()
    if m is None:
        base.update({"ok": False, "error": "h5i daily_bars 不可用, 跳过镜像"})
        return base
    if not os.path.exists(DUCKDB_PATH):
        base.update({"ok": False, "error": f"DuckDB 不存在: {DUCKDB_PATH}"})
        return base
    try:
        import duckdb
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            df = con.execute(
                "SELECT symbol, date, open, high, low, close, volume, amount, "
                "change_pct, turnover FROM daily_bars WHERE date > ? "
                "ORDER BY date, symbol", [m]
            ).fetchdf()
        finally:
            try:
                con.close()
            except Exception:
                pass
    except Exception as e:
        base.update({"ok": False, "error": f"DuckDB 读取失败: {e}"})
        return base
    base["read_rows"] = int(len(df)) if df is not None else 0
    r = append_daily_bars(df)
    base.update({k: r.get(k) for k in ("appended", "skipped_rows", "dates", "ok", "error")})
    if r.get("error"):
        base.setdefault("error", r["error"])
    if base["read_rows"] == 0:
        _log("追平: DuckDB 无晚于 h5i 最大日期的新行 (回填/同日已跳过)")
    elif r.get("appended", 0) > 0:
        _log(f"追平: 读 {base['read_rows']} 行, append {r.get('appended')} 行")
    return base


if __name__ == "__main__":
    import json
    print(json.dumps({"enabled": h5i_write_enabled(),
                      "h5i_path": H5I_PATH,
                      "duckdb": DUCKDB_PATH,
                      "max_bar_date": str(max_bar_date(force=True))},
                     ensure_ascii=False, indent=2))
