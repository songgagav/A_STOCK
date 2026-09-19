# -*- coding: utf-8 -*-
"""保真度重算的新旧版本逐窗口对账 (fidelity_compare.py).

背景
  2026-09-18 估值/行情数据版本变化(e9070faf -> 71e44ede)后, 保真度(11 决策日 x 6 调仓点,
  共 66 个调仓点)必须在**同一版本**上重跑, 才能与 §⑩ 六格矩阵处在同一数据基线。
  旧值(保真度偏差 -0.66pp)已随数据版本失效, 本脚本负责逐窗口给出"变了没有、变在哪"。

判定口径
  - 静态腿: 只依赖决策日的 top10 -> 数据版本不变时应**逐位一致**;
  - 调仓腿: 依赖 5 个中间调仓点的 PIT 选股, 任一点成分变化都会经 20 日 x 6 段复利放大,
    故**逐窗口比对 + 定位到具体调仓点**才算完成归因(见 --explain)。
  - 差异**不是**读取故障的判据: 脚本另行核对两版本在该窗口各调仓点的选股缓存
    (`<day>_n20_<ver>.json`), 只要缓存层能解释差异, 就不属于"静默空表"类故障。

用法
    python scripts/fidelity_compare.py                       # 逐窗口对账 + 汇总
    python scripts/fidelity_compare.py --explain 2021-07-02  # 额外定位该窗口哪个调仓点变了
输出
    data/fidelity_rebalance_compare.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

NEW = os.path.join(_BASE, "data", "fidelity_rebalance.json")
OLD = os.path.join(_BASE, "data", "fidelity_rebalance_pre_valrebuild.json")
OUT = os.path.join(_BASE, "data", "fidelity_rebalance_compare.json")
CACHE = os.path.join(_BASE, "data", "pit", "selection_cache")
VER_OLD = "e9070faf"      # 9/15 重建后的数据版本(旧保真度所用)
VER_NEW = "71e44ede"      # 9/18 重建后的数据版本(新保真度所用)
TOL = 0.005               # 视为"逐位一致"的阈值(pp)


def _load(p: str) -> dict:
    if not os.path.exists(p):
        print(f"[fatal] 缺文件: {p}")
        sys.exit(2)
    return {r["day"]: r for r in json.load(open(p, encoding="utf-8"))}


def _mean(rows: list[dict], key: str):
    v = [r[key] for r in rows if r.get(key) is not None]
    return st.mean(v) if v else None


def explain(day: str) -> None:
    """定位该决策日 6 个调仓点中哪些的选股在新旧版本间发生了变化."""
    import pandas as pd
    from vnpy_backtest import forward_window_days
    cal = forward_window_days(pd.Timestamp(day).date(), 120)
    pts = [str(cal[p])[:10] for p in range(0, len(cal), 20)]
    print(f"    {day} 调仓点: {pts}")
    for d in pts:
        o = os.path.join(CACHE, f"{d}_n20_d{VER_OLD}.json")
        n = os.path.join(CACHE, f"{d}_n20_d{VER_NEW}.json")
        if not (os.path.exists(o) and os.path.exists(n)):
            print(f"      {d}: 缓存不全 (old={os.path.exists(o)} new={os.path.exists(n)})")
            continue
        oc = [t["canon"] for t in json.load(open(o, encoding="utf-8"))["targets"]][:10]
        nc = [t["canon"] for t in json.load(open(n, encoding="utf-8"))["targets"]][:10]
        if oc == nc:
            print(f"      {d}: 逐位一致")
        else:
            only_o = [c for c in oc if c not in nc]
            only_n = [c for c in nc if c not in oc]
            tag = "集合相同, 仅名次变化(等权无影响)" if set(oc) == set(nc) else \
                  f"成分替换 -{only_o} +{only_n} (交集 {len(set(oc) & set(nc))}/10)"
            print(f"      {d}: **{tag}**")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", default=NEW)
    ap.add_argument("--old", default=OLD)
    ap.add_argument("--explain", nargs="*", default=[],
                    help="对指定决策日定位到具体调仓点")
    a = ap.parse_args()

    new, old = _load(a.new), _load(a.old)
    days = [d for d in old if d in new]
    print(f"保真度新旧对账: 共同窗口 {len(days)}/{len(old)} (旧 {os.path.basename(a.old)}"
          f" -> 新 {os.path.basename(a.new)})\n")
    print(f"{'决策日':<12}{'旧静态':>9}{'新静态':>9}{'Δ静态':>8}"
          f"{'旧调仓后':>10}{'新调仓后':>10}{'Δ调仓pp':>10}   判定")
    print("-" * 82)
    recs = []
    for d in sorted(days):
        o, n = old[d], new[d]
        ds = n["static"] - o["static"]
        dn = n["rebal_net"] - o["rebal_net"]
        same = abs(ds) < TOL and abs(dn) < TOL
        recs.append({"day": d, "static_old": o["static"], "static_new": n["static"],
                     "static_delta": ds, "rebal_old": o["rebal_net"],
                     "rebal_new": n["rebal_net"], "rebal_delta_pp": dn,
                     "cost_old": o["cost"], "cost_new": n["cost"],
                     "avg_new_old": o["avg_new"], "avg_new_new": n["avg_new"],
                     "identical": same})
        print(f"{d:<12}{o['static']:>9.2f}{n['static']:>9.2f}{ds:>8.2f}"
              f"{o['rebal_net']:>10.2f}{n['rebal_net']:>10.2f}{dn:>10.2f}   "
              f"{'逐位一致' if same else '**偏离**'}")
    print("-" * 82)

    o_rows = [old[d] for d in days]
    n_rows = [new[d] for d in days]
    os_, on_ = _mean(o_rows, "static"), _mean(o_rows, "rebal_net")
    ns_, nn_ = _mean(n_rows, "static"), _mean(n_rows, "rebal_net")
    print("\n=== 汇总 ===")
    print(f"  静态持有   旧 {os_:+.2f}%  新 {ns_:+.2f}%   Δ {ns_ - os_:+.2f}pp")
    print(f"  定期调仓净 旧 {on_:+.2f}%  新 {nn_:+.2f}%   Δ {nn_ - on_:+.2f}pp")
    print(f"  **保真度偏差(调仓-持有)**  旧 {on_ - os_:+.2f}pp  新 {nn_ - ns_:+.2f}pp"
          f"   Δ {(nn_ - ns_) - (on_ - os_):+.2f}pp")
    print(f"  平均成本   旧 {_mean(o_rows, 'cost'):.2f}%  新 {_mean(n_rows, 'cost'):.2f}%")
    print(f"  平均新名   旧 {_mean(o_rows, 'avg_new'):.1f}/10  新 {_mean(n_rows, 'avg_new'):.1f}/10")
    nd = [r["day"] for r in recs if not r["identical"]]
    print(f"  逐位一致窗口 {len(recs) - len(nd)}/{len(recs)}"
          + (f"; 偏离窗口: {nd}" if nd else ""))

    if a.explain:
        print("\n=== 偏离窗口的调仓点定位 ===")
        for d in a.explain:
            explain(d)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"old_file": os.path.basename(a.old), "new_file": os.path.basename(a.new),
                   "versions": {"old": VER_OLD, "new": VER_NEW}, "windows": recs,
                   "summary": {"static_old": os_, "static_new": ns_,
                               "rebal_old": on_, "rebal_new": nn_,
                               "fidelity_old_pp": on_ - os_,
                               "fidelity_new_pp": nn_ - ns_,
                               "identical_windows": len(recs) - len(nd),
                               "diverged_windows": nd},
                   "note": "数据版本变更后的保真度逐窗口对账(66 调仓点)"},
                  f, ensure_ascii=False, indent=2)
    print("\n已保存:", OUT)


if __name__ == "__main__":
    main()
