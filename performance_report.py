# -*- coding: utf-8 -*-
# ============================================================
# performance_report.py -- [D层] 绩效归因 / 反馈评估
#
# 架构定位: 反馈与评估层的第一块落地。把盘中/收盘模拟盘的回执
#   (data/daily/<day>/daily_summary.json) 汇总成可读的绩效报告:
#   * 组合净值曲线与关键指标(累计/年化收益、最大回撤、Calmar、夏普近似)
#   * 相对基准(全A等权)的每日超额
#   * 持仓归因(每只持仓的贡献: 持仓权重 x 个涨跌幅)
#   * IC 监控联动: 读取 data/ic/ic_curve_*.csv 汇总因子时效
#
# 输出:
#   * data/performance_report.json  (结构化, 供 dashboard/复盘消费)
#   * CLI 终端报告
#
# 用法:
#   python performance_report.py                  # 全量回执
#   python performance_report.py --days 20        # 仅最近20个交易日
#   python performance_report.py --no-bench       # 不计算基准
# ============================================================

import os
import sys
import json
import glob
import argparse
import datetime

import numpy as np
import pandas as pd
import duckdb

from config import DUCKDB_PATH, DATA_DIR, DAILY_DIR, INIT_CAPITAL

PERF_FILE = os.path.join(DATA_DIR, "performance_report.json")
IC_DIR = os.path.join(DATA_DIR, "ic")
BACKTEST_FILE = os.path.join(DATA_DIR, "backtest_latest.json")


def load_backtest_baseline():
    """读取 data/backtest_latest.json 作为策略回测基线(供与模拟盘实绩对照).
    返回 {ok, ...} 或 {ok:False}. """
    if not os.path.exists(BACKTEST_FILE):
        return {"ok": False, "reason": "无 backtest_latest.json"}
    try:
        with open(BACKTEST_FILE, encoding="utf-8") as f:
            b = json.load(f)
        return {
            "ok": True,
            "tag": b.get("tag"),
            "start": b.get("start"),
            "end": b.get("end"),
            "init_capital": b.get("init_capital"),
            "final_equity": _r(b.get("final_equity"), 2),
            "total_return_pct": _r(b.get("total_return"), 3),
            "max_drawdown_pct": _r(b.get("max_drawdown_pct"), 3),
            "trade_days": b.get("trade_days"),
            "total_trades": b.get("total_trades"),
            "note": b.get("note"),
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ---------------- 工具 ----------------
def _norm_day(d) -> str:
    """任意日期 -> YYYYMMDD。"""
    s = str(d).strip().replace("-", "")
    return s[:8]


def _r(x, nd=4):
    return round(float(x), nd) if x is not None and not (
        isinstance(x, float) and not np.isfinite(x)) else None


def _parse_day(day):
    return datetime.date(int(day[:4]), int(day[4:6]), int(day[6:8]))


def _year_span(d0, d1):
    return (_parse_day(d1) - _parse_day(d0)).days / 365.0


# ---------------- 回执读取 ----------------
def load_daily_equities(n: int = None):
    """读取 data/daily/*/daily_summary.json 的 {day, equity, cash, positions}。
    按日升序返回列表; n 若给定只保留最后 n 天。"""
    rows = []
    dirs = sorted(glob.glob(os.path.join(DAILY_DIR, "*")))
    for d in dirs:
        if not os.path.isdir(d):
            continue
        day = os.path.basename(d)
        if not (len(day) == 8 and day.isdigit()):
            continue
        sp = os.path.join(d, "daily_summary.json")
        if not os.path.exists(sp):
            continue
        try:
            with open(sp, encoding="utf-8") as f:
                s = json.load(f)
            paper = (s.get("steps") or {}).get("paper") or {}
            equity = paper.get("equity")
            if equity is None:
                continue
            rows.append({
                "day": _norm_day(s.get("day", day)),
                "equity": float(equity),
                "cash": float(paper.get("cash") or 0.0),
                "positions": paper.get("positions") or {},
                # 每份回执自带的初始资金(若存在); 无则 None 由调用方回退 config
                "init_capital": float(paper["init_capital"]) if paper.get("init_capital") not in (None, 0) else None,
            })
        except Exception:
            continue
    rows.sort(key=lambda r: r["day"])
    if n:
        rows = rows[-n:]
    return rows


# ---------------- 基准: 全A等权 ----------------
def load_bench_returns(start: str, end: str) -> dict:
    """全A等权日收益: daily_bars 在 [start,end] 内逐日对 change_pct 取均值。
    start/end 为 YYYYMMDD。返回 {day(YYYYMMDD): 小数收益}。

    [2026-09-07 修复] DuckDB 已退役(daily_bars 迁入 h5i): 原直连 DuckDB 路径
    在 daily_bars 表缺失时会整页报错; 现改为 duckdb 成功则用之, 失败自动降级
    走 h5i (factor_fusion._sql), 保证 dashboard /api/perf 现算路径可用.
    """
    s = _norm_day(start)
    e = _norm_day(end)
    # DuckDB date 需要 YYYY-MM-DD
    s_iso = "{}-{}-{}".format(s[:4], s[4:6], s[6:8])
    e_iso = "{}-{}-{}".format(e[:4], e[4:6], e[6:8])

    def _to_map(df: pd.DataFrame) -> dict:
        if df is None or df.empty:
            return {}
        df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
        return dict(zip(df["date"], df["ret"].astype(float)))

    # 1) 首选 DuckDB (兼容旧环境)
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            df = con.execute("""
                SELECT date, avg(CASE WHEN isnan(change_pct) THEN NULL ELSE change_pct END) / 100.0 AS ret
                FROM daily_bars
                WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
                      AND change_pct IS NOT NULL
                GROUP BY date
            """, [s_iso, e_iso]).fetchdf()
            return _to_map(df)
        finally:
            con.close()
    except Exception:
        pass

    # 2) h5i 降级 (daily_bars 现存放于 data/h5i/market.db)
    try:
        from factor_fusion import _sql
        df = _sql(
            f"SELECT CAST(ts AS DATE) AS date, change_pct FROM daily_bars "
            f"WHERE CAST(ts AS DATE) >= DATE '{s_iso}' "
            f"AND CAST(ts AS DATE) <= DATE '{e_iso}' "
            f"AND change_pct IS NOT NULL")
        if df is None or df.empty:
            return {}
        df = df[df["change_pct"].notna()]
        g = df.groupby("date")["change_pct"].mean() / 100.0
        out = pd.DataFrame({"date": g.index, "ret": g.to_numpy()})
        return _to_map(out)
    except Exception:
        return {}


# ---------------- 主报告 ----------------
def compute_report(rows, bench=True) -> dict:
    if not rows:
        return {"ok": False, "error": "无任何 daily_summary 回执"}

    days = [r["day"] for r in rows]
    equities = np.array([r["equity"] for r in rows], dtype=float)
    n_days = len(days)

    # 起始资金: 优先取首份回执的实值 init_capital, 回退 config 默认
    base = None
    init_src = "config"
    for rw in rows:
        if rw.get("init_capital"):
            base = rw["init_capital"]
            init_src = "report({})".format(rw["day"])
            break
    if base is None:
        base = INIT_CAPITAL
    total_ret = equities[-1] / base - 1.0
    # 逐日收益(净值变化)
    daily_ret = np.diff(equities) / equities[:-1]
    # 年化(自然日跨度)
    span_years = max(1e-9, _year_span(days[0], days[-1]))
    annual_ret = (1.0 + total_ret) ** (1.0 / span_years) - 1.0 if total_ret > -1 else -1.0

    # 最大回撤 / Calmar / Sortino (2026-09-07 补 Sortino 下行偏差口径)
    peak = np.maximum.accumulate(equities)
    dd = equities / peak - 1.0
    max_dd = float(dd.min())
    calmar = (annual_ret / abs(max_dd)) if max_dd < 0 else None
    downside = None
    sortino_annual = None
    if len(daily_ret) > 1:
        down_ret = np.minimum(daily_ret, 0.0)
        downside = float(np.sqrt(np.mean(down_ret ** 2)))
        mean_r = float(np.mean(daily_ret))
        if downside > 1e-12:
            sortino_annual = float(mean_r / downside * np.sqrt(252.0))

    # 日收益统计
    if len(daily_ret) > 1:
        rstd = float(np.std(daily_ret, ddof=1))
        sharpe = float(daily_ret.mean() / rstd * np.sqrt(252)) if rstd > 0 else None
    else:
        rstd, sharpe = 0.0, None

    # ---- 相对基准(全A等权) ----
    bench_rows = []
    bench_cum_total = None
    excess_total = None
    if bench and n_days > 0:
        bd = load_bench_returns(days[0], days[-1])
        cum_b = 1.0
        for i, day in enumerate(days):
            port_cum = equities[i] / base
            br = bd.get(day)
            if br is not None and np.isfinite(br):
                cum_b *= (1.0 + br)
                bench_cum_total = cum_b - 1.0
                exc = (port_cum - 1.0) - (cum_b - 1.0)
                bench_rows.append({
                    "day": day,
                    "port_ret": _r(daily_ret[i - 1]) if i > 0 else None,
                    "bench_ret": _r(br),
                    "port_cum": _r(port_cum - 1.0),
                    "bench_cum": _r(cum_b - 1.0),
                    "cum_excess": _r(exc),
                })
        if bench_rows:
            excess_total = bench_rows[-1]["cum_excess"]

    # ---- 持仓归因(最新一天) ----
    last = rows[-1]
    positions = last.get("positions") or {}
    attribution = []
    for canon, p in positions.items():
        qty = p.get("qty") or 0
        avg = p.get("avg_cost") or 0
        lp = p.get("last_price") or 0
        mv = qty * lp
        w = mv / equities[-1] if equities[-1] else 0
        pnl = (lp - avg) * qty if avg else 0
        attribution.append({
            "canon": canon,
            "qty": int(qty),
            "avg_cost": _r(avg, 3),
            "last_price": _r(lp, 3),
            "market_value": _r(mv, 2),
            "weight": _r(w, 4),
            "unrealized_pnl": _r(pnl, 2),
            "unrealized_pct": _r((lp / avg - 1) * 100, 2) if avg else 0,
        })
    attribution.sort(key=lambda x: -(x["unrealized_pnl"] or 0))

    # ---- IC 监控汇总 ----
    ic_summary = {}
    for f in ("vol", "mom_20", "reversal"):
        p = os.path.join(IC_DIR, "ic_curve_{}_k20.csv".format(f))
        if os.path.exists(p):
            try:
                df = pd.read_csv(p)
                if "ic_h20" in df.columns:
                    s = df["ic_h20"].dropna()
                    if len(s) > 1 and s.std(ddof=1) > 0:
                        icir = _r(s.mean() / s.std(ddof=1), 3)
                    else:
                        icir = None
                    ic_summary[f] = {
                        "n": int(len(s)),
                        "ic_mean": _r(s.mean(), 4),
                        "icir": icir,
                        "recent_mean20": _r(s.tail(20).mean(), 4),
                    }
            except Exception:
                pass

    return {
        "ok": True,
        "source": "data/daily/*/daily_summary.json",
        "period": {"start": days[0], "end": days[-1], "n_days": n_days},
        "metrics": {
            "init_capital": base,
            "init_capital_src": init_src,
            "final_equity": _r(equities[-1], 2),
            "total_return": _r(total_ret * 100, 3),
            "annual_return": _r(annual_ret * 100, 3),
            "max_drawdown": _r(max_dd * 100, 3),
            "daily_ret_std": _r(rstd * 100, 3),
            "sharpe_annual": sharpe,
            "sortino_annual": _r(sortino_annual, 4),
            "calmar": calmar,
        },
        "benchmark": {
            "bench_total_ret": _r(bench_cum_total * 100, 3) if bench_cum_total is not None else None,
            "excess_total": excess_total,
            "daily": bench_rows,
        },
        "backtest_baseline": load_backtest_baseline(),
        "attribution": attribution,
        "ic_summary": ic_summary,
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _print_report(r):
    print("=" * 52)
    print("全A轮动 · 绩效归因报告  {} ~ {}".format(
        r["period"]["start"], r["period"]["end"]))
    print("-" * 52)
    m = r["metrics"]
    print("累计收益 {:+.3f}% | 年化 {:+.3f}% | 最大回撤 {:+.3f}%".format(
        m["total_return"], m["annual_return"], m["max_drawdown"]))
    print("夏普(年化) {} | Calmar {} | 日波动 {:.3f}%".format(
        m["sharpe_annual"] if m["sharpe_annual"] is not None else "—",
        m["calmar"] if m["calmar"] is not None else "—",
        m["daily_ret_std"]))
    b = r["benchmark"]
    if b.get("bench_total_ret") is not None:
        print("基准(全A等权) {:+.3f}% | 超额 {:+.3f}%".format(
            b["bench_total_ret"],
            b["excess_total"] if b["excess_total"] is not None else 0.0))
    bb = r.get("backtest_baseline") or {}
    if bb.get("ok"):
        print("回测基线(策略回放 {}~{}): 累计 {:+.3f}% | 最大回撤 {:.3f}%".format(
            bb.get("start"), bb.get("end"),
            bb.get("total_return_pct") or 0.0, bb.get("max_drawdown_pct") or 0.0))
    ic = r.get("ic_summary") or {}
    if ic:
        print("-" * 52)
        print("因子IC监控 (H20, 最近20日):")
        for f, v in ic.items():
            print("  {:8s} IC均值 {:+.4f} | ICIR {:+.3f} | 近20日均值 {:+.4f}".format(
                f, v["ic_mean"], v["icir"] or 0.0, v["recent_mean20"]))
    att = r.get("attribution") or []
    if att:
        print("-" * 52)
        print("持仓归因 (最新日, 按浮盈排序):")
        for x in att:
            print("  {} 权重{:5.2f}% 浮盈 {:+.2f} ({:+.2f}%)".format(
                x["canon"], (x["weight"] or 0) * 100,
                x["unrealized_pnl"] or 0.0, x["unrealized_pct"] or 0.0))


def main():
    ap = argparse.ArgumentParser(description="全A轮动绩效归因")
    ap.add_argument("--days", type=int, help="只用最近 N 个交易日回执")
    ap.add_argument("--no-bench", action="store_true", help="不计算基准超额")
    a = ap.parse_args()

    rows = load_daily_equities(n=a.days)
    r = compute_report(rows, bench=not a.no_bench)
    if not r.get("ok"):
        print("无回执: {}".format(r.get("error")))
        return 1
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PERF_FILE, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2, default=str)
    # ===== ArcticDB 持久化 (供 SPC / 退化检测历史查询) =====
    try:
        from arctic_store import get_store
        store = get_store()
        period = r.get("period") or {}
        end_day = period.get("end") or ""
        if end_day and len(end_day) == 8:
            end_iso = f"{end_day[:4]}-{end_day[4:6]}-{end_day[6:8]}"
            if store.write_perf_report(end_iso, r):
                print(f"[ArcticDB] perf_report 已写: {end_iso}")
    except Exception as e:
        print(f"[ArcticDB] perf_report 写盘失败 (容错): {e}")
    _print_report(r)
    print("\n已写: {}".format(PERF_FILE))
    return 0


if __name__ == "__main__":
    sys.exit(main())