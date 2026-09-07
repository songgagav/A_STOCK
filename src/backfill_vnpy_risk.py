# -*- coding: utf-8 -*-
"""回填 vnpy summary: 补 Calmar (精确) 与 Sortino (等权主链路近似).

2026-09-07: 旧 summary 由 vnpy 引擎写入时不含 Calmar/Sortino. 本脚本对缺失
文件补写:
  - stats.calmar            : annual_return(%) / |max_ddpercent(%)| (精确)
  - stats.sortino_ratio_approx : 用 121 日等权主链路日收益序列计算的下行风险口径
    (检测层会读取并在 meta 标注 sortino_approx=True, 属理论近似口径)

用法: python backfill_vnpy_risk.py [--all]
"""
from __future__ import annotations

import glob
import json
import os
import sys

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)

DATA_DIR = os.path.join(_BASE, "data")
VNPY_DIR = os.path.join(DATA_DIR, "vnpy_backtest")


def _calmar_from_stats(s: dict):
    try:
        a = float(s.get("annual_return"))
        d = float(s.get("max_ddpercent"))
    except (TypeError, ValueError):
        return None
    return round(a / abs(d), 4) if d and abs(d) > 1e-9 else None


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="处理全部 vnpy 目录(默认仅缺失)")
    args = ap.parse_args()

    # 等权主链路基准日收益 (用于 sortino 近似)
    from backtest_with_gate import load_dataset
    ds = load_dataset("2026-03-12", "2026-09-04")
    R = ds["R"] if ds.get("ok") else None
    eq_base = None
    if R is not None and len(R) > 0:
        nav = np.ones(len(R))
        for i in range(1, len(R)):
            nav[i] = nav[i - 1] * (1.0 + R[i])
        eq_base = nav
    sortino_approx = None
    if eq_base is not None and len(eq_base) >= 5:
        r = np.diff(eq_base) / eq_base[:-1]
        down = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2)))
        if down > 1e-12:
            sortino_approx = round(float(np.mean(r)) / down * np.sqrt(252.0), 4)

    files = sorted(glob.glob(os.path.join(VNPY_DIR, "*", "summary.json")))
    touched = 0
    for p in files:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        s = data.get("stats") or {}
        if not (args.all or "calmar" not in s or "sortino_ratio_approx" not in s):
            continue
        changed = False
        if "calmar" not in s:
            c = _calmar_from_stats(s)
            if c is not None:
                s["calmar"] = c
                changed = True
        if "sortino_ratio_approx" not in s and sortino_approx is not None:
            s["sortino_ratio_approx"] = sortino_approx
            s["sortino_note"] = "等权主链路日收益近似口径(非引擎逐日净值)"
            changed = True
        if changed:
            data["stats"] = s
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            touched += 1
            print(f"回填 {os.path.relpath(p, _BASE)} calmar={s.get('calmar')} "
                  f"sortino_approx={s.get('sortino_ratio_approx')}")
    print(f"完成: 共处理 {touched} 个 summary 文件")
    sys.exit(0)


if __name__ == "__main__":
    main()
