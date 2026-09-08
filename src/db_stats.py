# -*- coding: utf-8 -*-
"""db_stats.py -- 数据库表统计 / 质量指标 / 历史趋势快照 / 全量更新触发.

服务对象: Prometheus 指标端点(/metrics), Celery 后台任务, dashboard(8000) 数据板块.

统计范围:
  - h5i market.db 的 7 张表: 行数 / 最后交易日 / 关键列空值率 / (ts,symbol) 唯一性
  - 关键数据文件: state.json / weights.json / factor_gate_state.json 等 (mtime/size)
采集结果追加到 data/db_stats_history.jsonl 作为趋势历史快照 (供趋势图).

更新: update_all_tables() 封装 update_db.update_all 全量数据同步,
      由 Celery worker 异步执行并回报进度 (见 tasks_db.py).

用法:
  python -m db_stats --once        # 采集一次并写历史快照 (stdout JSON)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
SRC = os.path.join(_BASE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.chdir(_BASE)

HISTORY_FILE = os.path.join(_BASE, "data", "db_stats_history.jsonl")
DATA_DIR = os.path.join(_BASE, "data")

# h5i 表: 时间列统一为 ts; 质量检查关注的关键数值列
TABLE_SPEC = {
    "daily_bars":          {"na_cols": ("close", "change_pct", "turnover")},
    "valuation":           {"na_cols": ("pe_ttm", "pb")},
    "valuation_snapshot":  {"na_cols": ("price", "pe_ttm", "pb", "float_mv")},
    "margin_daily":        {"na_cols": (None,)},
    "money_flow_estimate": {"na_cols": (None,)},
    "northbound_money":    {"na_cols": (None,)},
    "financials":          {"na_cols": ("revenue", "net_profit")},
}

# 关键数据文件资产 (补全非 h5i 的运行状态源)
FILE_ASSETS = ("state.json", "weights.json", "factor_gate_state.json",
               "performance_report.json", "backtest_latest.json")


def _sql(q: str):
    """factor_fusion._sql 包装 (读路径与巡检脚本一致)."""
    import factor_fusion as ff
    return ff._sql(q)


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def _table_stats(name: str, na_cols) -> dict:
    out = {"asset": "table", "name": name, "rows": None, "last_day": None,
           "na": {}, "dup_pairs": None, "ok": True, "err": None}
    try:
        r = _sql(f'SELECT COUNT(*) n, MAX(CAST(ts AS DATE)) d FROM "{name}"')
        n, d = int(r.iloc[0]["n"]), r.iloc[0]["d"]
        out["rows"] = n
        out["last_day"] = str(d) if d is not None else None
        for col in na_cols:
            if not col:
                continue
            q = ("SELECT COUNT(*) n, COUNT(\"%s\") nn, "
                 "COUNT(DISTINCT ts || '|' || symbol) u FROM \"%s\"") % (col, name)
            r2 = _sql(q)
            tot = int(r2.iloc[0]["n"])
            nn = int(r2.iloc[0]["nn"])
            u = int(r2.iloc[0]["u"])
            out["na"][col] = round((tot - nn) / tot * 100, 2) if tot else 0.0
            out["dup_pairs"] = round((1 - u / tot) * 100, 3) if tot else 0.0
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["err"] = str(e)[:160]
    return out


def _file_stats(fn: str) -> dict:
    p = os.path.join(DATA_DIR, fn)
    out = {"asset": "file", "name": fn, "rows": None, "last_day": None,
           "na": {}, "dup_pairs": None, "ok": False, "err": "missing"}
    if os.path.exists(p):
        st = os.stat(p)
        out["ok"] = True
        out["err"] = None
        out["last_day"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        out["size"] = st.st_size
        try:
            with open(p, encoding="utf-8") as f:
                out["rows"] = len(json.load(f)) if isinstance(json.load(f), list) else None
        except Exception:
            out["rows"] = None
    return out


def collect_once(force_all: bool = False) -> list[dict]:
    """采集全部表/文件统计."""
    rows = []
    for name, spec in TABLE_SPEC.items():
        rows.append(_table_stats(name, spec.get("na_cols") or ()))
    for fn in FILE_ASSETS:
        rows.append(_file_stats(fn))
    return rows


# ---------------------------------------------------------------------------
# 历史趋势快照
# ---------------------------------------------------------------------------
def append_history(stats: list[dict] | None = None) -> dict:
    stats = stats if stats is not None else collect_once()
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "stats": stats}
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def history_frame(limit: int | None = None) -> dict:
    """读历史快照 -> {name: [{t, rows, last_day}...]} (供趋势图)."""
    series: dict[str, list] = {}
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    if limit:
        lines = lines[-limit:]
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        t = rec.get("ts", "")
        for s in rec.get("stats", []):
            nm = s.get("name")
            if not nm:
                continue
            series.setdefault(nm, []).append({
                "t": t, "rows": s.get("rows"), "last_day": s.get("last_day"),
            })
    return series


# ---------------------------------------------------------------------------
# 全量数据更新 (供 Celery 异步任务 / 手动按钮调用)
# ---------------------------------------------------------------------------
def update_all_tables(day: str | None = None, only: list[str] | None = None,
                      progress: callable | None = None) -> dict:
    """调用 update_db.update_all 执行全量同步 (可能耗时较长, 应在后台任务中执行).

    progress: 可选回调, 每张表完成后以 (表名, 结果 dict) 调用.
    """
    from datetime import date, datetime as _dt
    from update_db import update_all
    day = day or _dt.now().strftime("%Y-%m-%d")
    if progress:
        progress("start", {"day": day})
    upd = update_all(day=datetime.strptime(day, "%Y-%m-%d").date(), only=only)
    if progress:
        progress("done", {"ok": True})
    ak = upd.pop("__akshare_stats__", {})
    return {"ok": any(v.get("ok") for v in upd.values()),
            "day": day, "tables": {k: {"ok": v.get("ok"), "rows": v.get("rows")}
                                   for k, v in upd.items()},
            "akshare_stats": ak}


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="采集一次并写历史快照")
    args = ap.parse_args()
    if args.once:
        t0 = time.time()
        rec = append_history()
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        print("elapsed %.1fs history=%s" % (time.time() - t0, HISTORY_FILE))
