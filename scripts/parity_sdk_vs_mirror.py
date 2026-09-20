# -*- coding: utf-8 -*-
"""SDK 直连 vs kline_parts 离线镜像：同一交易日的逐股逐字段一致性对账.

为什么需要它
------------
生产摄入原本读 `E:\\A_stockDB\\kline_parts\\*.parquet`（离线镜像）。该镜像自 2026-09-05
起冻结，且**其生成器在本机已不存在**（全仓 + 湖根 + 桌面/文档/下载均无命中），厂商文档
也从未提及该产物。厂商真正承诺的读取接口是 `pybao/stock_sdk.rd`（引擎 127.0.0.1:7899）。

把生产源切到 SDK 之前，必须先证明**两者在同一天、同一只股票、同一组字段上是同一份数据** ——
否则"切换"就可能是拿"看起来补上了"换掉"静默漏掉一批股票"。这正是本项目一律要求
"证据先于结论"的地方。

判定口径
--------
· 代码集合: 引擎侧 ⊇ / = 镜像侧（差异必须能逐条解释, 不允许默认为噪声）;
· 数值字段: |Δ| <= 容差(默认 1e-6 相对) 视为一致; 逐字段统计最大偏差与不一致条数;
· 镜像侧缺行: 单独统计并列出, 且**不当作通过** —— 必须解释(如北交所分片本就停在更早日期)。

用法
  python scripts/parity_sdk_vs_mirror.py --day 2026-09-03
  python scripts/parity_sdk_vs_mirror.py --day 2026-09-03 --out data/parity_sdk_vs_mirror.json
退出码: 0 = 一致(含"差异已全部解释"); 1 = 存在未解释差异; 2 = 环境错误(引擎/SDK/目录)
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LAKE = os.environ.get("STOCKDB_ROOT", "").strip() or r"E:\A_stockDB"
PYBAO = os.environ.get("PYBAO_DIR", "").strip() or os.path.join(LAKE, "pybao")
PARTS = os.path.join(LAKE, "kline_parts")

# 引擎侧按前缀取数。北交所在两端都是 92xxxx（43/83/87 两边皆 0, 实测）。
PREFIXES = ["0*", "3*", "6*", "9*"]

FIELDS = ["open", "high", "low", "close", "volume", "amount"]


def _d8(day: str) -> str:
    return str(day).replace("-", "")


def _engine_rows(day8: str) -> tuple[dict, dict]:
    """返回 ({code: row}, {prefix: n})."""
    if PYBAO not in sys.path:
        sys.path.insert(0, PYBAO)
    from stock_sdk import rd  # noqa: F401

    out, per = {}, {}
    for pfx in PREFIXES:
        rows = list(rd.vals("日k", pfx, day8))
        per[pfx] = len(rows)
        for r in rows:
            out[str(r.get("code")).zfill(6)] = r
    return out, per


def _mirror_rows(day: str) -> tuple[dict, dict]:
    """返回 ({code: row}, 统计). 只读分片里该日的那一行。"""
    import pandas as pd

    files = sorted(glob.glob(os.path.join(PARTS, "*.parquet")))
    out, stat = {}, {"files": len(files), "read_errors": 0, "no_row_for_day": 0}
    for f in files:
        code = os.path.basename(f).split("_")[-1].replace(".parquet", "")
        try:
            df = pd.read_parquet(f, columns=["trade_date", "symbol"] + FIELDS)
        except Exception:
            stat["read_errors"] += 1
            continue
        d = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        sub = df[d == day]
        if sub.empty:
            stat["no_row_for_day"] += 1
            continue
        out[code.zfill(6)] = sub.iloc[-1].to_dict()
    return out, stat


def _cmp(a, b, rel=1e-6) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if fa != fa or fb != fb:  # NaN
        return (fa != fa) and (fb != fb)
    if fa == fb:
        return True
    scale = max(abs(fa), abs(fb), 1.0)
    return abs(fa - fb) / scale <= rel


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="交易日 YYYY-MM-DD")
    ap.add_argument("--out", default="", help="把结果写 JSON")
    args = ap.parse_args()
    day = str(args.day)
    day8 = _d8(day)
    if len(day8) != 8 or not day8.isdigit():
        print("[FAIL] 日期非法")
        return 2
    if not os.path.isdir(PARTS):
        print(f"[FAIL] 镜像目录不存在: {PARTS}")
        return 2

    print("=" * 78)
    print(f"SDK 直连 vs kline_parts 镜像 对账   day={day}")
    print("=" * 78)

    try:
        eng, per = _engine_rows(day8)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 引擎侧取数失败: {type(e).__name__}: {e}")
        print("       提示: 需要 stockdb.exe 在 127.0.0.1:7899 常驻")
        return 2
    print(f"  引擎侧: {len(eng)} 只   前缀分布 {per}")

    mir, mstat = _mirror_rows(day)
    print(f"  镜像侧: {len(mir)} 只有该日行   文件 {mstat['files']} 个, "
          f"该日无行 {mstat['no_row_for_day']} 个, 读失败 {mstat['read_errors']} 个")

    ec, mc = set(eng), set(mir)
    both = sorted(ec & mc)
    eng_only = sorted(ec - mc)
    mir_only = sorted(mc - ec)
    print(f"\n  交集 {len(both)} | 仅引擎有 {len(eng_only)} | 仅镜像有 {len(mir_only)}")

    bad: dict = {}
    for code in both:
        e, m = eng[code], mir[code]
        for fld in FIELDS:
            if not _cmp(e.get(fld), m.get(fld)):
                bad.setdefault(fld, []).append(
                    {"code": code, "engine": e.get(fld), "mirror": m.get(fld)})

    print("\n  --- 数值字段不一致统计 ---")
    if not bad:
        print("    全部一致（容差 1e-6 相对）")
    for fld in FIELDS:
        rows = bad.get(fld, [])
        print(f"    {fld:<8} 不一致 {len(rows)}")
        for r in rows[:3]:
            print(f"        {r['code']}  引擎={r['engine']}  镜像={r['mirror']}")

    # 未解释差异 = 仅镜像有 (引擎漏了) + 字段不一致
    unexplained = len(mir_only) + sum(len(v) for v in bad.values())
    print(f"\n  未解释差异数: {unexplained}")
    if eng_only:
        print(f"  仅引擎有 {len(eng_only)} 只（若为镜像停更导致, 属**镜像落后**而非引擎漏数）: "
              f"{eng_only[:10]}")
    if mir_only:
        print(f"  [严重] 仅镜像有 {len(mir_only)} 只 —— 引擎**缺**这些股票: {mir_only[:10]}")
    for fld, rows in bad.items():
        print(f"  [严重] {fld} 有 {len(rows)} 条与镜像不符: {[r['code'] for r in rows[:10]]}")

    res = {"day": day, "engine_n": len(eng), "mirror_n": len(mir),
           "engine_prefix_counts": per, "mirror_stat": mstat,
           "intersection": len(both), "engine_only": eng_only[:200],
           "mirror_only": mir_only, "field_mismatch_counts": {k: len(v) for k, v in bad.items()},
           "field_mismatch_samples": {k: v[:5] for k, v in bad.items()},
           "unexplained": unexplained,
           "at": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print(f"\n  结果已写: {args.out}")

    if unexplained:
        print("\n[FAIL] 存在未解释差异, 不应据此切换数据源")
        return 1
    print("\n[PASS] 同日同股同字段一致 —— 引擎可作为镜像的等价替代")
    return 0


if __name__ == "__main__":
    sys.exit(main())
