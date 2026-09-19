# -*- coding: utf-8 -*-
"""上线前复验 · 数据层只读检查 (preflight_data_checks.py).

对应复验清单"一、数据层"中的三项可自动化的检查:
  ① 口径边界   —— 三段 PB 口径边界日抽检(统一 / 上游 / 统一), 看 PB 跳变是否可解释;
  ② 财务前视   —— **全部** _avail_date 实现交叉比对(清单只要求 governance vs fusion,
                  但仓库里有 5 份独立实现, 任一份漂移都是一个前视/滞后源);
  ③ 并发读     —— 两个进程同时读 valuation/financials, 与单进程基线比对行数。

用法
    python scripts/preflight_data_checks.py            # 全部
    python scripts/preflight_data_checks.py --only b   # 只跑某一项 (a/b/c)
输出
    data/preflight_data_checks.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import pandas as pd  # noqa: E402

OUT = os.path.join(_BASE, "data", "preflight_data_checks.json")
PY = sys.executable

#: 口径边界两侧各取一日 + 段内基准 (见 pit-valuation §⑩ 口径时间边界)
BOUNDARY_DAYS = ["2020-10-21", "2020-10-22", "2025-08-01", "2025-08-04", "2023-06-30"]
RESULT: dict = {}


def _sql(q: str) -> pd.DataFrame:
    import h5i_db
    db = h5i_db.Database(os.path.join(_BASE, "data", "h5i", "market.db"),
                         read_only=True)
    try:
        return db.sql(q).to_pandas()
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def check_boundary() -> None:
    """① 三段 PB 口径边界: source 分布 + PB 覆盖率 + 中位 PB."""
    print("=== ① 口径边界抽检 ===")
    rows = []
    for d in BOUNDARY_DAYS:
        df = _sql(
            "SELECT source, COUNT(*) AS n, "
            "SUM(CASE WHEN pb IS NOT NULL THEN 1 ELSE 0 END) AS n_pb, "
            "MEDIAN(pb) AS med_pb "
            f"FROM valuation WHERE CAST(ts AS DATE)=DATE '{d}' GROUP BY source")
        if df.empty:
            print(f"  {d}: 无数据")
            rows.append({"day": d, "empty": True})
            continue
        tot = int(df["n"].sum())
        npb = int(df["n_pb"].sum())
        cov = npb / tot if tot else 0.0
        med = float((df["med_pb"] * df["n"]).sum() / max(npb, 1))
        src = {r["source"]: int(r["n"]) for r in df.to_dict("records")}
        ok = cov >= 0.95
        print(f"  {d}: 行={tot:>7} PB非空={cov:6.2%} 中位PB={med:6.3f} "
              f"source={src}  {'OK' if ok else '**覆盖率偏低**'}")
        rows.append({"day": d, "rows": tot, "pb_cov": round(cov, 4),
                     "median_pb": round(med, 4), "sources": src, "cov_ok": ok})
    RESULT["boundary"] = rows


def check_pit_consistency() -> None:
    """② 全部 _avail_date 实现交叉比对(2010-2026 全部报告期 + 非季末月)."""
    print("\n=== ② 财务前视: _avail_date 实现一致性 ===")
    impls = {}
    try:
        import db as _db
        impls["db"] = _db._avail_date
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] db._avail_date 不可用: {type(e).__name__}")
    try:
        import factor_fusion as _ff
        impls["factor_fusion"] = _ff._avail_date
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] factor_fusion._avail_date 不可用: {type(e).__name__}")
    try:
        import factor_library as _fl
        impls["factor_library"] = _fl._avail_date
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] factor_library._avail_date 不可用: {type(e).__name__}")
    try:
        import valuation_backfill as _vb
        impls["valuation_backfill"] = _vb.avail_date
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] valuation_backfill.avail_date 不可用: {type(e).__name__}")
    try:
        from factor_mine import m5_rebuild as _m5
        impls["m5_rebuild"] = _m5.avail_date
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] m5_rebuild.avail_date 不可用: {type(e).__name__}")

    print(f"  发现实现 {len(impls)} 份: {list(impls)}")
    periods = [pd.Timestamp(f"{y}-{m:02d}-{dd}")
               for y in range(2010, 2027) for m, dd in
               ((3, 31), (6, 30), (9, 30), (12, 31), (1, 31), (7, 15))]
    mismatch = []
    for ts in periods:
        vals = {}
        for nm, fn in impls.items():
            try:
                vals[nm] = str(pd.Timestamp(fn(ts)).date())
            except Exception as e:  # noqa: BLE001
                vals[nm] = f"ERR:{type(e).__name__}"
        if len(set(vals.values())) > 1:
            mismatch.append({"period": str(ts.date()), "values": vals})
    total = len(periods)
    print(f"  比对 {total} 个报告期 × {len(impls)} 份实现 -> 不一致 {len(mismatch)} 处")
    for m in mismatch[:6]:
        print(f"    {m['period']}: {m['values']}")
    RESULT["pit_consistency"] = {"n_impls": len(impls), "impls": list(impls),
                                 "n_periods": total, "n_mismatch": len(mismatch),
                                 "mismatches": mismatch[:20]}
    if len(impls) > 1:
        print(f"  ⚠️ 同一规则存在 {len(impls)} 份拷贝 —— 本次一致, 但任一处单独修改即产生"
              f"前视/滞后分歧(建议收敛为单一来源)")


_CHILD = r'''
import sys, json, os
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
import h5i_db
db = h5i_db.Database(os.path.join("data", "h5i", "market.db"), read_only=True)
out = {}
for t, q in (("valuation", "SELECT COUNT(*) AS n FROM valuation"),
             ("financials", "SELECT COUNT(*) AS n FROM financials")):
    out[t] = int(db.sql(q).to_pandas()["n"].iloc[0])
print(json.dumps(out))
'''


def check_concurrent_reads() -> None:
    """③ 并发读: 两个进程同时读 valuation/financials, 与单进程基线比对."""
    print("\n=== ③ 并发读一致性 ===")
    baseline = _child_counts()
    print(f"  单进程基线: {baseline}")
    procs = [subprocess.Popen([PY, "-c", _CHILD], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              cwd=_BASE) for _ in range(2)]
    outs, errs = [], []
    for p in procs:
        o, e = p.communicate(timeout=900)
        outs.append(o)
        errs.append(e)
    got = []
    for o in outs:
        try:
            got.append(json.loads(o.strip().splitlines()[-1]))
        except Exception:  # noqa: BLE001
            got.append(None)
    ok = all(g == baseline for g in got)
    print(f"  并发两进程: {got}")
    for e in errs:
        if e.strip():
            print(f"    stderr: {e.strip()[:160]}")
    print(f"  判定: {'OK 两进程行数与基线一致, 无空表/锁冲突' if ok else '**不一致 -> 需排查**'}")
    RESULT["concurrent_reads"] = {"baseline": baseline, "concurrent": got, "ok": ok}


def _child_counts() -> dict:
    r = subprocess.run([PY, "-c", _CHILD], capture_output=True, text=True,
                       encoding="utf-8", cwd=_BASE, timeout=900)
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="a=口径边界 b=前视一致性 c=并发读")
    a = ap.parse_args()
    todo = a.only or "abc"
    if "a" in todo:
        check_boundary()
    if "b" in todo:
        check_pit_consistency()
    if "c" in todo:
        check_concurrent_reads()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(RESULT, f, ensure_ascii=False, indent=2, default=str)
    print("\n已保存:", OUT)


if __name__ == "__main__":
    main()
