# -*- coding: utf-8 -*-
"""非重叠窗口回测 (nonoverlap_rerun.py).

目的: 为过拟合检测提供"非重叠、可作时间序列"的窗口样本, 提升 PBO/置换检验
统计功效 (窗口数需 >=12 或至少不相互 99% 重叠).

用法 (建议在收盘后/停止守护进程、且 data/h5i/market.db 空闲时运行):
    python scripts/nonoverlap_rerun.py                       # 默认 12 个非重叠窗口(增量合并)
    python scripts/nonoverlap_rerun.py --only-missing         # 只补跑缺失/失败的窗口
    python scripts/nonoverlap_rerun.py --ends 2020-01-03 2024-07-03
    python scripts/nonoverlap_rerun.py --fresh                # 忽略既有结果, 全量重跑

窗口集 (2026-09-13 扩到 12): 每个窗口 120 个交易日, 窗口之间互不重叠,
按起点排序后相邻窗口首尾相接或留有空隙, 覆盖 2017-12-29 ~ 2026-09-04.

输出:
    data/vnpy_backtest_nonoverlap_results.json  (结构同 vnpy_backtest_rerun_results.json)
    默认按 day 合并既有结果: 已成功的窗口不会被重复回测/覆盖, 便于增量扩容.

过拟合检测读取:
    $env:OVERFIT_RESULTS_FILE="data/vnpy_backtest_nonoverlap_results.json"
    python overfitting_test.py --html
"""
from __future__ import annotations

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

# 默认: 12 个互不重叠的 120 交易日窗口终点 (起点见下一行注释).
#   起点        终点           起点        终点
#   2017-12-29  2018-06-29     2022-07-08  2022-12-30
#   2018-12-27  2019-06-28     2023-07-07  2023-12-29
#   2019-07-11  2020-01-03     2024-01-02  2024-07-03
#   2020-07-09  2020-12-31     2024-12-27  2025-06-30
#   2021-01-04  2021-07-02     2025-09-01  2026-03-05
#   2021-12-29  2022-06-30     2026-03-16  2026-09-04
DEFAULT_ENDS = [
    "2018-06-29", "2019-06-28", "2020-01-03", "2020-12-31",
    "2021-07-02", "2022-06-30", "2022-12-30", "2023-12-29",
    "2024-07-03", "2025-06-30", "2026-03-05", "2026-09-04",
]


def _load_existing() -> list:
    if not os.path.exists(OUT):
        return []
    try:
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 既有结果读取失败, 将按空处理: {type(e).__name__}: {e}")
        return []


def main() -> None:
    args = sys.argv[1:]
    if "--ends" in args:
        ends = args[args.index("--ends") + 1:]
        ends = [a for a in ends if not a.startswith("--")]
    else:
        ends = list(DEFAULT_ENDS)
    only_missing = "--only-missing" in args
    fresh = "--fresh" in args

    prev = [] if fresh else _load_existing()
    done = {r.get("day"): r for r in prev if r.get("ok")}
    if prev:
        n_ok = len(done)
        print(f"既有结果: {len(prev)} 条, 其中成功 {n_ok} 条"
              f"{' (fresh: 忽略)' if fresh else ' (成功项将跳过)'}")

    todo = [d for d in ends if not (only_missing and done.get(d))]
    if len(todo) < len(ends):
        print(f"--only-missing: 跳过已完成 {len(ends) - len(todo)} 个, "
              f"待跑 {len(todo)} 个: {todo}")
    if not todo:
        print("无待跑窗口, 直接合并既有结果.")

    for day in todo:
        t0 = time.time()
        print(f"\n{'=' * 60}\n回测窗口(终点): {day}\n{'=' * 60}")
        res = run_vnpy_backtest(day, top_n=10, lookback_days=120)
        stats = res.get("stats") or {}
        ok = res.get("ok", False)
        print(f"  状态: {'OK' if ok else 'FAIL'}  耗时 {time.time() - t0:.1f}s")
        if not ok:
            # 常见原因: 该历史终点无当日 selection.json/target_plan(需当时落盘产物),
            # PIT 现场选股回退也失败(行情/估值/财务缺口) -> 无法凭空重建.
            print(f"  失败原因: {res.get('error') or res.get('reason') or '未知'}")
        else:
            print(f"  Sharpe={stats.get('sharpe_ratio')}  CAGR={stats.get('annual_return')}%  "
                  f"回撤={stats.get('max_ddpercent')}%")
        done[day] = {"day": day, "ok": ok, "elapsed": round(time.time() - t0, 1),
                     "dynamic_slippage": res.get("dynamic_slippage"),
                     "stats": {k: (round(v, 4) if isinstance(v, float) else v)
                               for k, v in (stats or {}).items()}}

    merged = sorted(done.values(), key=lambda r: r.get("day") or "")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, default=str)
    n_ok = sum(1 for r in merged if r.get("ok"))
    print(f"\n完成: 窗口合计 {len(merged)} 个 (成功 {n_ok} / 失败 {len(merged) - n_ok}) -> {OUT}")
    if n_ok < 12:
        print(f"[注意] 成功窗口 {n_ok} < 12, 过拟合检测的 PBO/置换检验仍会按功效不足处理.")
    print("用其跑过拟合: 设置 OVERFIT_RESULTS_FILE 后执行 python overfitting_test.py --html")


if __name__ == "__main__":
    main()
