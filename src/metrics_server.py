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

# ---- 策略绩效指标 (来源: state.json + dashboard.read_riskops + performance_report) ----
_g_eq = Gauge("astock_strategy_equity", "paper account equity")
_g_cash = Gauge("astock_strategy_cash", "paper account cash")
_g_pos = Gauge("astock_strategy_positions", "open position count")
_g_pnl = Gauge("astock_strategy_daily_pnl_pct", "daily pnl percent")
_g_sharpe = Gauge("astock_strategy_sharpe", "rolling sharpe (60d)")
_g_sortino = Gauge("astock_strategy_sortino", "sortino ratio")
_g_maxdd = Gauge("astock_strategy_max_dd", "max drawdown pct (negative)")
_g_var95 = Gauge("astock_strategy_var95", "daily var95 pct")
_g_win = Gauge("astock_strategy_win_rate", "win rate pct")
_g_totret = Gauge("astock_strategy_total_return", "total return pct since init")


def _to_f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _refresh_strategy() -> None:
    try:
        import json as _json
        import os as _os
        # state.json 实时持仓/权益
        sp = _os.path.join(_BASE, "data", "state.json")
        eq = ca = ps = None
        if _os.path.exists(sp):
            st = _json.load(open(sp, encoding="utf-8"))
            eq = _to_f(st.get("equity"))
            ca = _to_f(st.get("cash"))
            ps = sum(1 for v in (st.get("positions") or {}).values()
                     if v.get("qty", 0) > 0)
        _g_eq.set(eq if eq is not None else -1)
        _g_cash.set(ca if ca is not None else -1)
        _g_pos.set(ps if ps is not None else -1)
        # 绩效序列指标 (read_riskops)
        import dashboard as _dash
        rk = _dash.read_riskops()
        m = rk.get("metrics") or {}
        _g_pnl.set(_to_f(m.get("daily_pnl_pct")) if m.get("daily_pnl_pct") is not None else -1)
        _g_sharpe.set(_to_f(m.get("sharpe_w60")) if m.get("sharpe_w60") is not None else -1)
        _g_sortino.set(_to_f(m.get("sortino")) if m.get("sortino") is not None else -1)
        _g_maxdd.set(_to_f(m.get("max_dd")) if m.get("max_dd") is not None else -1)
        _g_var95.set(_to_f(m.get("var95")) if m.get("var95") is not None else -1)
        _g_win.set(_to_f(m.get("win_rate")) if m.get("win_rate") is not None else -1)
        # performance_report total_return
        pp = _os.path.join(_BASE, "data", "performance_report.json")
        tr = None
        if _os.path.exists(pp):
            pr = _json.load(open(pp, encoding="utf-8"))
            tr = _to_f((pr.get("metrics") or {}).get("total_return"))
        _g_totret.set(tr if tr is not None else -1)
    except Exception as e:  # noqa: BLE001
        print("strategy metrics err:", str(e)[:160], flush=True)


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
        _refresh_strategy()
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
