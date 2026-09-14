# -*- coding: utf-8 -*-
"""端到端验证池快照: 从快照复算的 Top-N 必须与 selector 实时计算**逐位一致**.

用法:
    python scripts/verify_pool_snapshot.py --day 2018-06-29

对每个口径各跑一次 `_select_hist`(约 1~2 分钟/次), 再离线从快照复算并比对:
  ① RANK_BY_FUSION=0, TRIM=0        -> 快照 rank_by='score'
  ② RANK_BY_FUSION=1, ALPHA=1.0     -> 快照 rank_by='fusion', alpha=1.0
  ③ RANK_BY_FUSION=1, ALPHA=1.0, TRIM=0.05 -> 快照 rank_by='fusion', alpha=1.0, trim_q=0.05
"""
from __future__ import annotations

import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))
os.chdir(BASE)

CASES = [
    ("RB=0 TRIM=0",        dict(RANK_BY_FUSION="0"), dict(rank_by="score")),
    ("RB=1 ALPHA=1.0",     dict(RANK_BY_FUSION="1", FUSION_RANK_ALPHA="1.0"),
     dict(rank_by="fusion", alpha=1.0)),
    ("RB=1 ALPHA=1.0 T05", dict(RANK_BY_FUSION="1", FUSION_RANK_ALPHA="1.0",
                                FUSION_TRIM_Q="0.05"),
     dict(rank_by="fusion", alpha=1.0, trim_q=0.05)),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2018-06-29")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--cases", default="", help="只跑名字含该子串的口径")
    a = ap.parse_args()

    from db import StockDB
    import pool_snapshot as ps
    from selector import RotationSelector

    db = StockDB()
    sel = RotationSelector(db, n=a.n)
    allpass = True
    for label, env, snap_kw in CASES:
        if a.cases and a.cases not in label:
            continue
        for k in ("RANK_BY_FUSION", "FUSION_RANK_ALPHA", "FUSION_TRIM_Q"):
            os.environ.pop(k, None)
        os.environ.update(env)
        res = sel._select_hist(a.day)
        live = [x["canon"] for x in res["top_n"]]
        df = ps.load(a.day, a.n)
        if df is None:
            print(f"[{label}] 快照缺失, FAIL")
            allpass = False
            continue
        snap = [x["canon"] for x in ps.top_from_snapshot(df, n=a.n, **snap_kw)]
        same = live == snap
        allpass &= same
        print(f"[{label}] live==snapshot: {same}")
        if not same:
            print("   live:", live)
            print("   snap:", snap)
        else:
            print("   top(5):", [c.split(".")[0] for c in live[:5]])
    print("\n结论:", "PASS 全部一致" if allpass else "FAIL 存在不一致")
    sys.exit(0 if allpass else 1)


if __name__ == "__main__":
    main()
