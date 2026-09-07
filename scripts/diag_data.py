# ============================================================
# scripts/diag_data.py -- 数据链路健康检查
#
# 用法:
#   python scripts/diag_data.py            # 检查今天
#   python scripts/diag_data.py 2026-08-25 # 检查指定日期
#
# 检查项:
#   1) DuckDB daily_bars 当日 (或指定日) 是否入库
#   2) data/market/<day>/market.json 是否存在且 ok
#   3) data/daily/<day>/daily_summary.json 是否存在
#   4) data/performance_report.json 是否最新 (含当日)
#   5) data/vnpy_backtest/<day>/summary.json 是否存在
#   6) data/drl/<day>/pre_drl_brief.json 是否存在
#
# 退出码: 0 全部 OK; 1 有缺失 (详情 stdout).
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import os
import sys

# 兼容: 直接运行或被 import
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJ_DIR)

from config import DATA_DIR, DUCKDB_PATH
import duckdb


def _check_daily_bars(day: str) -> tuple[bool, str]:
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        rows = con.execute(
            "SELECT COUNT(*) FROM daily_bars WHERE date = ?", [day]
        ).fetchone()[0]
        latest = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0]
        con.close()
        latest_str = latest.isoformat() if latest else "—"
        if rows == 0:
            return False, f"daily_bars [{day}] 0 行 (数据库最新={latest_str})"
        return True, f"daily_bars [{day}] {rows} 行 (数据库最新={latest_str})"
    except Exception as e:
        return False, f"daily_bars 查询异常: {type(e).__name__}: {e}"


def _check_market(day: str) -> tuple[bool, str]:
    p = os.path.join(DATA_DIR, "market", day.replace("-", ""), "market.json")
    if not os.path.exists(p):
        # fallback: 用 DuckDB 最近一日
        try:
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            row = con.execute(
                "SELECT MAX(date) FROM daily_bars WHERE date <= ?", [day]
            ).fetchone()
            con.close()
            if row and row[0]:
                fb = row[0].isoformat()
                fb_p = os.path.join(DATA_DIR, "market", fb.replace("-", ""), "market.json")
                if os.path.exists(fb_p):
                    return False, f"market/{day} 缺失; DuckDB 最近一日={fb} 有 fallback"
        except Exception:
            pass
        return False, f"market/{day}/market.json 不存在"
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        ok = m.get("ok")
        return bool(ok), f"market/{day} ok={ok} sentiment={m.get('sentiment_score')}"
    except Exception as e:
        return False, f"market/{day} 读取失败: {e}"


def _check_daily_summary(day: str) -> tuple[bool, str]:
    p = os.path.join(DATA_DIR, "daily", day.replace("-", ""), "daily_summary.json")
    if not os.path.exists(p):
        return False, f"daily/{day}/daily_summary.json 不存在"
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        st = d.get("status")
        eq = d.get("summary", {}).get("equity")
        return True, f"daily/{day} status={st} equity={eq}"
    except Exception as e:
        return False, f"daily/{day} 读取失败: {e}"


def _check_perf_report(day: str) -> tuple[bool, str]:
    p = os.path.join(DATA_DIR, "performance_report.json")
    if not os.path.exists(p):
        return False, "performance_report.json 不存在"
    try:
        with open(p, encoding="utf-8") as f:
            perf = json.load(f)
        period = perf.get("period") or {}
        last_day = (period.get("end") or "")[:8]
        last_iso = f"{last_day[:4]}-{last_day[4:6]}-{last_day[6:8]}" if len(last_day) == 8 else "?"
        on_target = last_iso == day
        return on_target, (
            f"performance_report end={last_iso} n_days={period.get('n_days')} "
            f"({'当日覆盖' if on_target else '未覆盖当日'})"
        )
    except Exception as e:
        return False, f"performance_report 读取失败: {e}"


def _check_vnpy(day: str) -> tuple[bool, str]:
    p = os.path.join(DATA_DIR, "vnpy_backtest", day.replace("-", ""), "summary.json")
    if not os.path.exists(p):
        return False, f"vnpy_backtest/{day}/summary.json 不存在"
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("fallback"):
            return False, f"vnpy/{day} fallback (无真实 stats)"
        st = s.get("stats") or {}
        return True, (
            f"vnpy/{day} engine={s.get('engine')} "
            f"ret={st.get('total_return')} sharpe={st.get('sharpe_ratio')}"
        )
    except Exception as e:
        return False, f"vnpy/{day} 读取失败: {e}"


def _check_pre_drl_brief(day: str) -> tuple[bool, str]:
    p = os.path.join(DATA_DIR, "drl", day.replace("-", ""), "pre_drl_brief.json")
    if not os.path.exists(p):
        return False, f"drl/{day}/pre_drl_brief.json 不存在 (LLM 调研未跑)"
    try:
        with open(p, encoding="utf-8") as f:
            b = json.load(f)
        ok = b.get("ok")
        eff = (b.get("meta") or {}).get("effective_day") or {}
        any_fb = eff.get("any_fallback") if isinstance(eff, dict) else None
        return bool(ok), f"pre_drl_brief ok={ok} fallback={any_fb}"
    except Exception as e:
        return False, f"pre_drl_brief 读取失败: {e}"


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().strftime("%Y-%m-%d")
    print(f"=== 数据健康检查 / {day} ===")
    print(f"DATA_DIR = {DATA_DIR}")
    print(f"DUCKDB   = {DUCKDB_PATH}")
    print()

    checks = [
        ("1) DuckDB daily_bars", _check_daily_bars(day)),
        ("2) market.json", _check_market(day)),
        ("3) daily_summary.json", _check_daily_summary(day)),
        ("4) performance_report.json", _check_perf_report(day)),
        ("5) vnpy_backtest summary", _check_vnpy(day)),
        ("6) pre_drl_brief.json", _check_pre_drl_brief(day)),
    ]

    fail = 0
    for name, (ok, msg) in checks:
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:35s} {msg}")
        if not ok:
            fail += 1

    print()
    if fail == 0:
        print("ALL GREEN ✓")
        return 0
    print(f"FAIL: {fail}/{len(checks)} 项缺失")
    return 1


if __name__ == "__main__":
    sys.exit(main())