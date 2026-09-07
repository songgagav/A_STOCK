# -*- coding: utf-8 -*-
"""数据库完整性巡检 (对照 docs 规范表).

对照《数据库表头》21 张目标表, 检查当前存储层:
  - h5i public 表 (daily_bars/valuation/financials/valuation_snapshot/northbound/
    money_flow_estimate/margin_daily)
  - data/h5i/views/*.parquet (因子视图)
  - data/*.json / data/factor_mine/*.json (注册表/门控/权重/日历等)

用法:
    python scripts/check_data_completeness.py             # 文本报告
    python scripts/check_data_completeness.py --json      # 写 data/check_data_report.json

输出结构: {run_ts, checks: [{name, status: ok|stale|missing|partial, detail}], summary}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import factor_fusion as ff  # noqa: E402

REPORT_PATH = os.path.join(_BASE, "data", "check_data_report.json")

# 每个检查: name / kind / spec: {sql 或 file} / expect: 说明
CHECKS = [
    # 1.x 行情
    {"name": "1.1 daily_bars", "kind": "h5i", "table": "daily_bars",
     "expect": "span>=1991 且最新=上一交易日"},
    {"name": "1.2 minute_bars", "kind": "missing",
     "expect": "无分钟级持久化(引擎内存/AKShare), 规范表未落地"},
    {"name": "1.3 tick_data", "kind": "missing", "expect": "无持久化"},
    {"name": "1.4 index_daily", "kind": "missing",
     "expect": "无独立指数日表(指数价未持久化)"},
    {"name": "1.5 stock_basic", "kind": "partial",
     "detail": "static/symbols.parquet + valuation(is_st) + industry_map/concept_map.json; 无统一表"},
    {"name": "1.6 trading_calendar", "kind": "json", "file": "data/trade_calendar.json",
     "expect": "交易日历 JSON"},
    # 2.x 因子
    {"name": "2.1 factor_raw", "kind": "view", "view": "v_factor_scores_daily",
     "expect": "6 因子日分视图"},
    {"name": "2.2 factor_neu", "kind": "file", "file": "data/h5i/factors/monthly_panel_neutral.parquet",
     "expect": "中性化月面板"},
    {"name": "2.3 factor_fusion", "kind": "file", "file": "data/factor_mine/fusion_ic_121d.json",
     "expect": "融合 IC 缓存(121日)"},
    {"name": "2.4 factor_ic_daily", "kind": "dir", "dir": "data/ic",
     "expect": "data/ic/ic_curve_*.csv (vol/mom_20/reversal)"},
    # 3.x 交易持仓(JSON/账本)
    {"name": "3.1 account", "kind": "json", "file": "data/state.json", "expect": "模拟盘账户状态"},
    {"name": "3.2 position", "kind": "json", "file": "data/state.json", "expect": "持仓在 state/live_state"},
    {"name": "3.3 order", "kind": "dir", "dir": "data/daily", "expect": "trades/paper_book 落盘 data/daily/*"},
    {"name": "3.4 trade", "kind": "same_as_3.3", "expect": "逐笔在 arctic trade_records / daily trades.json"},
    # 4.x 策略
    {"name": "4.1 daily_signal", "kind": "json", "file": "data/daily/", "expect": "selection.json 等每日产物"},
    {"name": "4.2 drl_state", "kind": "json", "file": "data/weights.json", "expect": "DRL/ICIR 权重"},
    {"name": "4.3 gate_state", "kind": "json", "file": "data/factor_gate_state.json",
     "expect": "regime/hyst/ic/reason"},
    # 5.x 风控
    {"name": "5.1 risk_events", "kind": "missing", "expect": "无显式事件表(仅 reasons 文本)"},
    {"name": "5.2 daily_risk", "kind": "json", "file": "data/performance_report.json",
     "expect": "绩效/回撤汇总"},
    # 6.x 系统
    {"name": "6.1 sync_log", "kind": "missing", "expect": "无统一同步日志(零散 mark)"},
    {"name": "6.2 factor_registry", "kind": "json", "file": "data/factor_mine/factor_registry.json",
     "expect": "因子注册表"},
    {"name": "6.3 model_versions", "kind": "dir", "dir": "data/drl_factor_value",
     "expect": "DRL 训练版本目录 train_meta"},
]

# 健康度: 各 h5i 表 "交易日滞后"(max_date 之后到基准的交易日数) 超容忍判 stale
HEALTH = [
    ("daily_bars", 0),        # 应=基准交易日
    ("valuation", 0),
    ("valuation_snapshot", 0),
    ("financials", 999),      # 按财报报告期, 不要求日更
    ("northbound_money", 0),
    ("margin_daily", 0),
    ("money_flow_estimate", 3),
]


def _last_trade_day_ahead() -> str:
    try:
        df = ff._sql("SELECT MAX(CAST(ts AS DATE)) d FROM daily_bars")
        return str(df.iloc[0, 0])
    except Exception:
        return "?"


def _h5i_table_ok(table: str, max_lag_trading: int) -> dict:
    """按交易日滞后判定: lag_trading = max_date 之后到基准的 daily_bars 交易日数."""
    base = _last_trade_day_ahead()
    out = {"table": table, "base_max": base}
    try:
        mx = ff._sql(f"SELECT MAX(CAST(ts AS DATE)) d FROM {table}").iloc[0, 0]
        out["max_date"] = str(mx)
    except Exception as e:
        out.update({"exists": False, "error": str(e)[:100], "ok": False})
        return out
    out["exists"] = True
    if base != "?" and str(mx) is not None:
        try:
            lag = int(ff._sql(
                f"SELECT COUNT(DISTINCT CAST(ts AS DATE)) FROM daily_bars "
                f"WHERE CAST(ts AS DATE) > DATE '{mx}' AND CAST(ts AS DATE) <= DATE '{base}'"
            ).iloc[0, 0])
        except Exception:
            lag = 0
        out["lag_trading"] = lag
        out["ok"] = lag <= max_lag_trading
    else:
        out["ok"] = True
    return out


def run() -> dict:
    checks = []
    base = _last_trade_day_ahead()
    for c in CHECKS:
        status, detail = "ok", c.get("expect", "")
        kind = c.get("kind", "")
        if kind == "h5i":
            r = _h5i_table_ok(c["table"], 2)
            status = "ok" if r.get("ok") else "stale"
            detail = f"exists={r.get('exists')} max={r.get('max_date')} base={base}"
        elif kind == "json":
            p = os.path.join(_BASE, c["file"])
            if not os.path.exists(p):
                status = "missing"
            detail = f"{c['file']} ({(os.path.getsize(p) if os.path.exists(p) else 0)}B)"
        elif kind == "dir":
            p = os.path.join(_BASE, c.get("dir", ""))
            if not os.path.isdir(p) or not os.listdir(p):
                status = "missing"
            detail = c.get("dir", "")
        elif kind == "view":
            p = os.path.join(_BASE, "data", "h5i", "views", c["view"] + ".parquet")
            if not os.path.exists(p):
                status = "missing"
            detail = c["view"] + ".parquet"
        elif kind == "file":
            p = os.path.join(_BASE, c["file"])
            if not os.path.exists(p):
                status = "missing"
            detail = c["file"]
        elif kind in ("missing", "partial", "same_as_3.3"):
            status = "missing" if kind == "missing" else kind
            detail = c.get("detail", c.get("expect", ""))
        checks.append({"name": c["name"], "status": status, "detail": detail})

    # 健康度专项 (h5i 表最新日期 vs 基期, 按交易日滞后)
    health = []
    for table, maxlag in HEALTH:
        r = _h5i_table_ok(table, maxlag)
        health.append({"table": table, **r})

    return {"run_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "base_trade_day": base, "checks": checks, "health": health}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rep = run()
    ok_n = sum(1 for c in rep["checks"] if c["status"] == "ok")
    print(f"巡检时间: {rep['run_ts']} | 基准交易日: {rep['base_trade_day']}")
    print(f"规范表 21 项: ok={ok_n} / 其它: "
          f"{[c['status'] for c in rep['checks'] if c['status'] != 'ok']}")
    print("\n-- h5i 数据健康度 (最新日期 vs 基准) --")
    for h in rep["health"]:
        flag = "OK " if h.get("ok") else ("MISS" if not h.get("exists") else "STALE")
        print(f"  [{flag}] {h['table']:24s} max={h.get('max_date')} "
              f"(lag={h.get('lag_days', '?')}d)")
    print("\n-- 21 表对照明细 --")
    for c in rep["checks"]:
        print(f"  {c['name']:24s} {c['status']:8s} {c['detail'][:80]}")
    if args.json:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写: {REPORT_PATH}")


if __name__ == "__main__":
    main()
