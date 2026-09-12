# -*- coding: utf-8 -*-
"""非重叠窗口回测 (nonoverlap_rerun.py).

目的: 为过拟合检测提供"非重叠、可作时间序列"的窗口样本, 提升 PBO/置换检验
统计功效 (窗口数需 >=12 或至少不相互 99% 重叠).

用法 (建议在收盘后/停止守护进程、且 data/h5i/market.db 空闲时运行):
    python scripts/nonoverlap_rerun.py                       # 默认两个 120 交易日窗口
    python scripts/nonoverlap_rerun.py --ends 2026-03-05 2026-09-04

输出:
    data/vnpy_backtest_nonoverlap_results.json  (结构同 vnpy_backtest_rerun_results.json)

过拟合检测读取:
    $env:OVERFIT_RESULTS_FILE="data/vnpy_backtest_nonoverlap_results.json"
    python overfitting_test.py --html
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))  # src/ 布局(2026-09 迁移), 模块位于 src/
os.chdir(_BASE)

from vnpy_backtest import run_vnpy_backtest  # noqa: E402

OUT = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_results.json")
# 默认: 两个"互不重叠"的 120 交易日窗口终点 (间隔 >= 约 120 交易日).
DEFAULT_ENDS = ["2026-03-05", "2026-09-04"]


def main() -> None:
    ends = [a for a in sys.argv[sys.argv.index("--ends") + 1:]] if "--ends" in sys.argv else DEFAULT_ENDS
    results = []
    for day in ends:
        t0 = time.time()
        print(f"\n{'=' * 60}\n回测窗口(终点): {day}\n{'=' * 60}")
        res = run_vnpy_backtest(day, top_n=10, lookback_days=120)
        stats = res.get("stats") or {}
        ok = res.get("ok", False)
        print(f"  状态: {'OK' if ok else 'FAIL'}  耗时 {time.time() - t0:.1f}s")
        if not ok:
            # 常见原因: 该历史终点无当日 selection.json/target_plan(需当时落盘产物),
            # 无法凭空重建 -> 提示改在真实运行过的日期上取窗口.
            print(f"  失败原因: {res.get('error') or res.get('reason') or '未知'}")
            print("  提示: vnpy 回测依赖当日的 selection/target_plan; 历史非重叠窗口"
                  "仅在数据源保留了当日选股产物时可重建.")
        if ok:
            print(f"  Sharpe={stats.get('sharpe_ratio')}  CAGR={stats.get('annual_return')}%  "
                  f"回撤={stats.get('max_ddpercent')}%")
        results.append({"day": day, "ok": ok, "elapsed": round(time.time() - t0, 1),
                        "dynamic_slippage": res.get("dynamic_slippage"),
                        "stats": {k: (round(v, 4) if isinstance(v, float) else v)
                                  for k, v in (stats or {}).items()}})
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完成: {len(results)} 个非重叠窗口 -> {OUT}")
    print("用其跑过拟合: 设置 OVERFIT_RESULTS_FILE 后执行 python overfitting_test.py --html")


if __name__ == "__main__":
    main()
