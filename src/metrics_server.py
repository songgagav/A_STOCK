# -*- coding: utf-8 -*-
"""metrics_server.py -- Prometheus /metrics 端点 (表健康 + 更新状态).

端口: 9101 (Prometheus scrape target)
指标:
  astock_db_rows{table}              表/文件行数
  astock_db_lastday_ts{table}         最后更新日的 epoch 秒 (供 24h 滞后告警)
  astock_db_na_pct{table,column}      关键列空值率 %
  astock_db_dup_pct{table}            (ts,symbol) 重复对占比 %
  astock_update_running               Celery/manual 全量更新是否在跑 (0/1)
采集周期: 60s, 每次同步追加 db_stats_history.jsonl (趋势历史).

用法: python src/metrics_server.py [--port 9101]
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
SRC = os.path.join(_BASE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.chdir(_BASE)

import db_stats  # noqa: E402
import tasks_db  # noqa: E402

from prometheus_client import Gauge, start_http_server  # noqa: E402

_g_rows = Gauge("astock_db_rows", "table/file row count", ["table"])
_g_last = Gauge("astock_db_lastday_ts", "last updated day epoch seconds",
                ["table"])
_g_na = Gauge("astock_db_na_pct", "null ratio percent of key column",
              ["table", "column"])
_g_dup = Gauge("astock_db_dup_pct", "dup (ts,symbol) pair percent",
               ["table"])
_g_run = Gauge("astock_update_running", "full db update running flag")


def _epoch(val) -> float | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val)).timestamp()
    except Exception:
        try:
            return datetime.strptime(str(val), "%Y-%m-%d").timestamp()
        except Exception:
            return None


def _refresh() -> None:
    try:
        stats = db_stats.collect_once()
        for s in stats:
            nm = s.get("name", "")
            _g_rows.labels(nm).set(s.get("rows") if s.get("rows") is not None else -1)
            ep = _epoch(s.get("last_day"))
            _g_last.labels(nm).set(ep if ep is not None else -1)
            for col, v in (s.get("na") or {}).items():
                _g_na.labels(nm, col).set(v if v is not None else -1)
            _g_dup.labels(nm).set(s.get("dup_pairs")
                                  if s.get("dup_pairs") is not None else -1)
        st = tasks_db._read_state()
        _g_run.set(1 if st.get("running") else 0)
        db_stats.append_history(stats)  # 60s 一条, 足够趋势分辨率
    except Exception as e:  # noqa: BLE001
        print("refresh err:", str(e)[:200], flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9101)
    ap.add_argument("--interval", type=int, default=60)
    a = ap.parse_args()
    start_http_server(a.port)
    print("metrics endpoint: http://localhost:%d/metrics  (interval=%ds)"
          % (a.port, a.interval), flush=True)
    _refresh()
    while True:
        time.sleep(a.interval)
        _refresh()


if __name__ == "__main__":
    main()
