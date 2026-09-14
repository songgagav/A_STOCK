# -*- coding: utf-8 -*-
"""OOS 台账: 把一次前向窗口回测的汇总指标追加到 data/oos_ledger.jsonl.

用途: 路径A "积累 OOS 窗口" —— 每次重跑(月度)都追加一行, 便于日后做统计推断
(当前 n=11, t 均不显著; 目标 20+ 窗口).

用法:
    python scripts/oos_ledger.py --results data/vnpy_backtest_nonoverlap_fwd_results_rb.json \\
        --note "重建后数据首测" [--tau RANK_BY_FUSION/RANK_ALPHA 标签]

幂等: 同一 (results 文件, 窗口集合) 不会重复追加; 数据版本/窗口集合变化则新增一行。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics as st
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))

LEDGER = os.path.join(BASE, "data", "oos_ledger.jsonl")


def summarise(path: str) -> dict:
    rows = json.load(open(path, encoding="utf-8"))
    ok = [x for x in rows if x.get("ok") and x.get("stats")]
    days = sorted(x["day"] for x in ok)
    rets = [x["stats"]["total_return"] for x in ok]
    sh = [x["stats"]["sharpe_ratio"] for x in ok]
    mdd = [abs(x["stats"]["max_ddpercent"]) for x in ok]
    return {
        "source_file": os.path.basename(path),
        "n_windows": len(ok),
        "n_failed": len(rows) - len(ok),
        "windows": [days[0], days[-1]],
        "days": days,
        "mean_ret_pct": round(st.mean(rets), 2),
        "median_ret_pct": round(st.median(rets), 2),
        "pos_windows": sum(1 for r in rets if r > 0),
        "pos_ratio": round(sum(1 for r in rets if r > 0) / len(rets), 3),
        "mean_sharpe": round(st.mean(sh), 2),
        "mean_mdd_pct": round(st.mean(mdd), 2),
        "max_mdd_pct": round(max(mdd), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--tags", default="")          # 如 "RB=0 TRIM=0" 或 "RB=1 ALPHA=1.0"
    a = ap.parse_args()

    p = a.results if os.path.isabs(a.results) else os.path.join(BASE, a.results)
    s = summarise(p)
    try:
        import vnpy_backtest as v
        s["data_version"] = v._data_version()
    except Exception as e:  # noqa: BLE001
        s["data_version"] = f"n/a({type(e).__name__})"
    s["tags"] = a.tags
    s["note"] = a.note
    s["run_at"] = dt.datetime.now().isoformat(timespec="seconds")

    # 去重口径 (2026-09-14): 按 (窗口集合, 数据版本, 配置标签) 判重, **不含产物文件名** ——
    # 台账应在"出现新窗口"或"数据版本变化"时增长, 而不是因为换了输出文件名就重复记一行。
    key = hashlib.md5(f"{','.join(s['days'])}|{s['data_version']}|{a.tags}"
                      .encode()).hexdigest()
    s["key"] = key

    seen = set()
    if os.path.exists(LEDGER):
        for line in open(LEDGER, encoding="utf-8"):
            line = line.strip()
            if line:
                seen.add(json.loads(line).get("key"))
    if key in seen:
        print(f"[ledger] 该 (窗口集合/数据版本/标签) 已存在, 跳过: {s['source_file']} "
              f"{s['windows'][0]}~{s['windows'][1]} ver={s['data_version']} tags='{a.tags}'")
        return
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"[ledger] 已追加: {s['source_file']} 窗口 {s['n_windows']} 个 "
          f"({s['windows'][0]}~{s['windows'][1]}) ver={s['data_version']}")
    print(f"         均值 {s['mean_ret_pct']:+.2f}%  中位 {s['median_ret_pct']:+.2f}%  "
          f"正窗口 {s['pos_windows']}/{s['n_windows']}  Sharpe {s['mean_sharpe']:+.2f}  "
          f"平均MDD {s['mean_mdd_pct']:.2f}%")
    n = len(seen) + 1
    print(f"         台账累计 {n} 行 -> {os.path.relpath(LEDGER, BASE)}")


if __name__ == "__main__":
    main()
