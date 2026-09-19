# -*- coding: utf-8 -*-
"""非重叠窗口回测 (nonoverlap_rerun.py).

目的: 为过拟合检测提供"非重叠、可作时间序列"的窗口样本, 提升 PBO/置换检验
统计功效 (窗口数需 >=12 或至少不相互 99% 重叠).

用法 (建议在收盘后/停止守护进程、且 data/h5i/market.db 空闲时运行):
    python scripts/nonoverlap_rerun.py                       # 默认 12 个**前向**窗口(增量合并)
    python scripts/nonoverlap_rerun.py --only-missing         # 只补跑缺失/失败的窗口
    python scripts/nonoverlap_rerun.py --ends 2020-01-03 2024-07-03
    python scripts/nonoverlap_rerun.py --fresh                # 忽略既有结果, 全量重跑
    python scripts/nonoverlap_rerun.py --backward             # 回到历史(前视)口径, 仅回溯用

口径 (2026-09-13 修正):
    前向(默认)  元素=决策日, 窗口 = [决策日, +120 交易日] -> 无前视, 用于 OOS。
    回溯(--backward) 元素=窗口终点, 窗口 = [终点-120, 终点]。评估期落在决策日之前,
      选股"知道"了整个评估期的走势(实测回测收益≈所选篮子在该区间的自身涨幅),
      **存在前视**, 仅可用于回溯归因。两种口径写入不同文件, 不混用。

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

OUT_LEGACY = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_results.json")
OUT_FWD = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")

# 默认: 12 个互不重叠的 120 交易日窗口. 2026-09-13 起口径改为**前向**:
#   forward=True  -> 元素是"决策日", 窗口 = [决策日, +120 交易日](无前视)
#   forward=False -> 元素是"窗口终点", 窗口 = [终点-120, 终点](**存在前视**, 仅回溯用)
#   起点        决策日/终点     起点        决策日/终点
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


def _load_existing(out: str, mode: str) -> list:
    """读取既有结果, 只保留同口径(window_mode)的条目 —— 前向/回溯不可混用."""
    if not os.path.exists(out):
        return []
    try:
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 既有结果读取失败, 将按空处理: {type(e).__name__}: {e}")
        return []
    if not isinstance(data, list):
        return []
    keep, drop = [], 0
    for r in data:
        if (r.get("window_mode") or "backward") == mode:
            keep.append(r)
        else:
            drop += 1
    if drop:
        print(f"[warn] 既有结果中有 {drop} 条属其它口径, 已忽略(不混用)")
    return keep


def main() -> None:
    args = sys.argv[1:]
    if "--ends" in args:
        ends = args[args.index("--ends") + 1:]
        ends = [a for a in ends if not a.startswith("--")]
    else:
        ends = list(DEFAULT_ENDS)
    only_missing = "--only-missing" in args
    fresh = "--fresh" in args
    # 口径: 2026-09-13 起默认前向(无前视); --backward 可回到历史(前视)口径
    forward = "--backward" not in args
    mode = "forward" if forward else "backward"
    out = OUT_FWD if forward else OUT_LEGACY
    # 2026-09-13: 排序口径不同(如 RANK_BY_FUSION=1)时结果须另存, 避免覆盖基线
    _suf = os.environ.get("OOS_OUT_SUFFIX", "")
    if _suf:
        out = out.replace(".json", f"_{_suf}.json")
    if not forward:
        print("[warn] --backward: 窗口为 [终点-120, 终点], 评估期落在决策日之前, "
              "存在前视, 结果不可用于样本外评估(仅回溯归因).")

    prev = [] if fresh else _load_existing(out, mode)
    done = {r.get("day"): r for r in prev if r.get("ok")}
    if prev:
        n_ok = len(done)
        print(f"既有结果({mode}): {len(prev)} 条, 其中成功 {n_ok} 条"
              f"{' (fresh: 忽略)' if fresh else ' (成功项将跳过)'}")

    todo = [d for d in ends if not (only_missing and done.get(d))]
    if len(todo) < len(ends):
        print(f"--only-missing: 跳过已完成 {len(ends) - len(todo)} 个, "
              f"待跑 {len(todo)} 个: {todo}")
    if not todo:
        print("无待跑窗口, 直接合并既有结果.")

    for day in todo:
        t0 = time.time()
        label = "决策日" if forward else "窗口终点"
        print(f"\n{'=' * 60}\n回测{label}: {day}  口径={mode}\n{'=' * 60}")
        res = run_vnpy_backtest(day, top_n=10, lookback_days=120, forward=forward)
        stats = res.get("stats") or {}
        ok = res.get("ok", False)
        print(f"  状态: {'OK' if ok else 'FAIL'}  耗时 {time.time() - t0:.1f}s")
        if not ok:
            # 常见原因: 该历史节点无当日 selection.json/target_plan(需当时落盘产物),
            # PIT 现场选股回退也失败(行情/估值/财务缺口) -> 无法凭空重建;
            # 前向模式下还可能是"未来交易日不足".
            print(f"  失败原因: {res.get('error') or res.get('reason') or '未知'}")
        else:
            print(f"  区间 {stats.get('start_date')} ~ {stats.get('end_date')}  "
                  f"Sharpe={stats.get('sharpe_ratio')}  "
                  f"CAGR={stats.get('annual_return')}%  "
                  f"回撤={stats.get('max_ddpercent')}%")
        done[day] = {"day": day, "ok": ok, "window_mode": mode,
                     "elapsed": round(time.time() - t0, 1),
                     "dynamic_slippage": res.get("dynamic_slippage"),
                     # 2026-09-19: 显式落盘引擎口径。此前只保留 stats, 下游要判断
                     # "本窗是否走了自研简化引擎" 只能靠 `"method" in stats` 反推 ——
                     # 隐式约定容易漏(§⑬: 简化引擎数值系统性偏高 +5.50pp, 且不可与
                     # vnpy 窗口同表统计)。fallback=True / engine='fallback_simple'
                     # 的窗口一律排除出矩阵。
                     "fallback": bool(res.get("fallback")),
                     "engine": res.get("engine"),
                     "stats": {k: (round(v, 4) if isinstance(v, float) else v)
                               for k, v in (stats or {}).items()}}

    merged = sorted(done.values(), key=lambda r: r.get("day") or "")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, default=str)
    n_ok = sum(1 for r in merged if r.get("ok"))
    print(f"\n完成({mode}): 窗口合计 {len(merged)} 个 "
          f"(成功 {n_ok} / 失败 {len(merged) - n_ok}) -> {out}")
    if n_ok < 12:
        print(f"[注意] 成功窗口 {n_ok} < 12, 过拟合检测的 PBO/置换检验仍会按功效不足处理.")
    print(f"用其跑过拟合: $env:OVERFIT_RESULTS_FILE=\"{os.path.relpath(out, _BASE)}\" "
          f"后执行 python src/overfitting_test.py --html")


if __name__ == "__main__":
    main()
