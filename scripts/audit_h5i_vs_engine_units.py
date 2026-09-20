# -*- coding: utf-8 -*-
"""审计 h5i daily_bars 与厂商引擎的同日截面：单位/口径是否一致、错在哪些股票哪几天.

为什么必须做
------------
在把生产源切到 SDK 直连之前，实测已发现**同一序列内单位不统一**的迹象：
`000001` 在 2026-09-04 的 h5i `volume=814,373`，而引擎同日晚 `81,437,300`
—— 恰好 100 倍（「手」vs「股」）；而该股票 09-07/09-08 的 volume 又是「股」口径。
同时 `turnover` 在 h5i 存**小数**(0.0042)，引擎给**百分数**(0.42)，且 09-07/09-08 为 NaN。

单一标的的观察**不足以**推断全局（这正是本项目 METHOD-1 的要求：结论要基于
"该值发生时的下游表现"而非一个样本）。故本脚本对**整个截面**做逐股对账，量化：
  · volume 比值分布（是否成簇地出现 ~0.01）
  · amount 比值分布
  · change_pct 与引擎 pct_chg 的一致性
  · turnover 的"空值率"与量纲比

用法
  python scripts/audit_h5i_vs_engine_units.py --days 2026-09-04 2026-09-07 2026-09-08
  python scripts/audit_h5i_vs_engine_units.py --days 2026-09-04 --out data/audit_units.json
退出码: 0 = 全部一致; 1 = 发现不一致; 2 = 环境错误
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

LAKE = os.environ.get("STOCKDB_ROOT", "").strip() or r"E:\A_stockDB"
PYBAO = os.environ.get("PYBAO_DIR", "").strip() or os.path.join(LAKE, "pybao")
PREFIXES = ["0*", "3*", "6*", "9*"]

# 比值分桶：以 2 倍为界判定"同量纲"，1/100 视为典型的手/股错。
def _bucket(r):
    if r is None or r != r:
        return "nan"
    if r == 0:
        return "zero"
    if 0.5 <= r <= 2.0:
        return "~1 (同量纲)"
    if 0.005 <= r <= 0.02:
        return "~0.01 (疑 手/股 100倍)"
    if 50 <= r <= 200:
        return "~100 (疑 股/手 100倍)"
    return f"其他({r:.4g})"


def _engine_day(day8: str) -> dict:
    if PYBAO not in sys.path:
        sys.path.insert(0, PYBAO)
    from stock_sdk import rd
    out = {}
    for pfx in PREFIXES:
        for r in rd.vals("日k", pfx, day8):
            out[str(r.get("code")).zfill(6)] = r
    return out


def _h5i_day(day: str):
    import h5i_sync as H
    db = H._open_h5i()
    if db is None:
        raise RuntimeError("h5i 库不可用")
    try:
        return db.sql(
            "SELECT symbol, open, high, low, close, volume, amount, change_pct, turnover "
            f"FROM daily_bars WHERE CAST(ts AS DATE) = CAST('{day}' AS DATE)"
        ).to_pandas()
    finally:
        try:
            db.close()
        except Exception:
            pass


def _ratio(a, b):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if b == 0 or b != b or a != a:
        return None
    return a / b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    report, bad_total = {}, 0
    for day in args.days:
        day8 = day.replace("-", "")
        print("=" * 78)
        print(f"审计 {day}")
        print("=" * 78)
        eng = _engine_day(day8)
        df = _h5i_day(day)
        print(f"  引擎 {len(eng)} 只 | h5i {len(df)} 行")

        h5 = {str(r["symbol"]).zfill(6): r for _, r in df.iterrows()}
        both = sorted(set(eng) & set(h5))
        print(f"  交集 {len(both)} | 仅引擎 {len(set(eng) - set(h5))} | 仅 h5i {len(set(h5) - set(eng))}")

        import collections
        vol_b = collections.Counter()
        amt_b = collections.Counter()
        to_b = collections.Counter()
        to_nan = 0
        pct_bad = []
        vol_bad = []
        for c in both:
            e, h = eng[c], h5[c]
            rv = _ratio(h.get("volume"), e.get("volume"))
            ra = _ratio(h.get("amount"), e.get("amount"))
            vol_b[_bucket(rv)] += 1
            amt_b[_bucket(ra)] += 1
            if _bucket(rv) != "~1 (同量纲)":
                vol_bad.append({"code": c, "h5i": h.get("volume"), "engine": e.get("volume"), "ratio": rv})
            tv = h.get("turnover")
            if tv is None or tv != tv:
                to_nan += 1
            else:
                to_b[_bucket(_ratio(tv, e.get("turnover")))] += 1
            hp, ep = h.get("change_pct"), e.get("pct_chg")
            try:
                if abs(float(hp) - float(ep)) > 0.02:
                    pct_bad.append({"code": c, "h5i": hp, "engine": ep})
            except (TypeError, ValueError):
                pct_bad.append({"code": c, "h5i": hp, "engine": ep})

        print(f"\n  volume 比值分布 : {dict(vol_b)}")
        print(f"  amount 比值分布 : {dict(amt_b)}")
        print(f"  turnover        : NaN {to_nan}/{len(both)}  非空比值分布 {dict(to_b)}")
        print(f"  change_pct 偏差>0.02 的条数: {len(pct_bad)}")

        vol_ok = vol_b.get("~1 (同量纲)", 0) == len(both)
        if not vol_ok:
            bad_total += 1
            print(f"\n  [不一致] volume 有 {len(vol_bad)} 只不同量纲, 样本:")
            for r in vol_bad[:5]:
                print(f"      {r['code']}  h5i={r['h5i']:.0f}  引擎={r['engine']:.0f}  比值={r['ratio']:.4g}")
        else:
            print("\n  [OK] volume 全截面同量纲")
        if to_nan:
            print(f"  [注意] turnover 有 {to_nan} 只为空 —— 该字段在存量数据里基本未回填")

        report[day] = {"engine_n": len(eng), "h5i_n": len(df), "both": len(both),
                       "volume_buckets": dict(vol_b), "amount_buckets": dict(amt_b),
                       "turnover_nan": to_nan, "turnover_buckets": dict(to_b),
                       "change_pct_mismatch": len(pct_bad),
                       "volume_bad_sample": vol_bad[:20]}

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"at": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                       "days": report}, fh, ensure_ascii=False, indent=2)
        print(f"\n结果已写: {args.out}")

    print("\n" + "=" * 78)
    if bad_total:
        print(f"[FAIL] {bad_total}/{len(args.days)} 天存在 volume 量纲不一致")
        return 1
    print("[PASS] 所有审计日 volume 全截面同量纲")
    return 0


if __name__ == "__main__":
    sys.exit(main())
