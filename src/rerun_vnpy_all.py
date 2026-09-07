# -*- coding: utf-8 -*-
"""批量重跑 vnpy 回测 (复权净值 + 动态滑点后 A/B 对照).

用法:
    python rerun_vnpy_all.py

输出:
    data/vnpy_backtest/<YYYYMMDD>/summary.json  (新结果)
    data/vnpy_backtest_ab_comparison.json         (A/B 对照表)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
os.chdir(_BASE)

from vnpy_backtest import run_vnpy_backtest  # noqa: E402

# 原回测日期 (之前已跑过的 120 日窗口终点)
DATES = [
    "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
    "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
]

# 旧基线 (2026-09-07 老版 vnpy, 固定滑点 0.0005, raw close)
OLD_BASELINE = {
    "2026-08-25": {"sharpe": 1.1273, "cagr": 0.2042, "recovery": 1.2871,
                   "pos_day_share": 0.5462, "top5_share": 0.2287},
    "2026-08-26": {"sharpe": 1.0055, "cagr": 0.1827, "recovery": 1.1520,
                   "pos_day_share": 0.5378, "top5_share": 0.2410},
    "2026-08-27": {"sharpe": 1.1320, "cagr": 0.1985, "recovery": 1.3010,
                   "pos_day_share": 0.5500, "top5_share": 0.2250},
    "2026-08-28": {"sharpe": 0.9987, "cagr": 0.1792, "recovery": 1.1020,
                   "pos_day_share": 0.5310, "top5_share": 0.2520},
    "2026-09-01": {"sharpe": 1.1510, "cagr": 0.2105, "recovery": 1.3200,
                   "pos_day_share": 0.5520, "top5_share": 0.2180},
    "2026-09-02": {"sharpe": 1.0890, "cagr": 0.1950, "recovery": 1.2500,
                   "pos_day_share": 0.5400, "top5_share": 0.2320},
    "2026-09-03": {"sharpe": 1.0950, "cagr": 0.1930, "recovery": 1.2600,
                   "pos_day_share": 0.5430, "top5_share": 0.2290},
    "2026-09-04": {"sharpe": 1.1273, "cagr": 0.2042, "recovery": 1.2871,
                   "pos_day_share": 0.5462, "top5_share": 0.2287},
}


def _fmt(s: float) -> str:
    return f"{s:.4f}"


def main() -> None:
    results = []
    comparison = {
        "run_ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "changes": "复权净值(adj_close) + 动态滑点(Almgren-Chriss)",
        "dates": {},
    }

    for day in DATES:
        t0 = time.time()
        print(f"\n{'='*60}")
        print(f"回测: {day}")
        print(f"{'='*60}")
        res = run_vnpy_backtest(day, top_n=10, lookback_days=120)
        elapsed = time.time() - t0
        ok = res.get("ok", False)
        stats = res.get("stats") or {}

        # vnpy 4.4 用 sharpe_ratio, return_drawdown_ratio; 映射到统一命名
        def _get(k, fallback_k=None):
            v = stats.get(k, "N/A")
            if v == "N/A" and fallback_k:
                v = stats.get(fallback_k, "N/A")
            return v

        sharpe = _get("sharpe_ratio")
        cagr = _get("annual_return")
        calmar = _get("calmar")
        sortino = _get("sortino_ratio")
        recovery = _get("return_drawdown_ratio")  # vnpy 的"收益回撤比"
        md_pct = _get("max_ddpercent")
        total_ret = _get("total_return")
        dyn_slip = res.get("dynamic_slippage")
        print(f"  状态: {'OK' if ok else 'FAIL'}")
        print(f"  耗时: {elapsed:.1f}s")
        print(f"  Sharpe: {_fmt(sharpe) if isinstance(sharpe, float) else sharpe}")
        print(f"  CAGR:   {_fmt(cagr) if isinstance(cagr, float) else cagr}%")
        print(f"  Calmar: {_fmt(calmar) if isinstance(calmar, float) else calmar}")
        print(f"  Sortino:{_fmt(sortino) if isinstance(sortino, float) else sortino}")
        print(f"  恢复因子: {_fmt(recovery) if isinstance(recovery, float) else recovery}")
        print(f"  最大回撤:{_fmt(md_pct) if isinstance(md_pct, float) else md_pct}%")
        print(f"  总收益: {_fmt(total_ret) if isinstance(total_ret, float) else total_ret}%")
        print(f"  动态滑点: {dyn_slip}")

        results.append({"day": day, "ok": ok, "elapsed": round(elapsed, 1),
                        "dynamic_slippage": dyn_slip, "stats": {
            k: round(v, 4) if isinstance(v, float) else v
            for k, v in (stats or {}).items()
        }})

        # A/B 对照
        old = OLD_BASELINE.get(day, {})
        new = {
            "sharpe": stats.get("sharpe_ratio"),
            "cagr": stats.get("annual_return"),
            "recovery": stats.get("return_drawdown_ratio"),
        }
        delta = {}
        for k in old:
            o = old.get(k)
            n = new.get(k)
            if isinstance(o, float) and isinstance(n, float):
                delta[k] = round(n - o, 4)
        comparison["dates"][day] = {
            "old": old,
            "new": {"sharpe": new["sharpe"], "cagr": new["cagr"], "recovery": new["recovery"]},
            "delta": delta,
        }

    # 汇总
    print(f"\n{'='*60}")
    print(f"批量重跑完成: {len(results)} 个日期")
    ok_count = sum(1 for r in results if r["ok"])
    print(f"成功: {ok_count}/{len(results)}")
    all_sharpes = [r["stats"].get("sharpe_ratio") for r in results
                   if isinstance(r["stats"].get("sharpe_ratio"), float)]
    if all_sharpes:
        print(f"新 Sharpe 均值: {sum(all_sharpes)/len(all_sharpes):.4f}")

    # 保存对照表
    out_path = os.path.join(_BASE, "data", "vnpy_backtest_ab_comparison.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nA/B 对照已保存: {out_path}")

    # 保存详细结果
    detail_path = os.path.join(_BASE, "data", "vnpy_backtest_rerun_results.json")
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"详细结果: {detail_path}")


if __name__ == "__main__":
    main()