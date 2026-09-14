# -*- coding: utf-8 -*-
"""修复 valuation 表中 `float_shares` 整列为空的历史日期 (2026-09-14 观察期首日发现)。

问题
    上游 2026-09-08 段的 `valuation_snapshot` 里 `float_shares` / `total_shares`
    整列 NULL (5551/5551), 但 `float_mv` / `price` 完整。并入 valuation 后当日
    `float_shares` 全空 ⇒ `factor_fusion._assemble_snapshot` 的
    `ln_size = ln(close * float_shares)` 全 NaN ⇒ 规模中性化**静默退化为仅行业中性**。

修法
    仅对 `float_shares IS NULL 且 free_cap IS NOT NULL` 的行, 用
    `float_shares = free_cap / price` 复原 (price 取同源 valuation_snapshot)。
    精度: 在 09-04 段 (两者齐全, n=5181) 上校验, 相对误差 中位 0.0032% /
    p95 0.019% / 最大 0.13%。

安全
    - 默认 **dry-run**, 只看会改多少行; 加 --apply 才写库。
    - 写库前先把受影响行导出 parquet 备份 (data/backup/valuation_<day>_pre_floatfix.parquet)。
    - 使用 h5i `plan_replace_range` 做**行区间定点替换**(不改动其它行)。
      仅当该日在表中是连续区块时才允许执行。

用法
    python scripts/repair_float_shares.py --day 2026-09-08            # dry-run
    python scripts/repair_float_shares.py --day 2026-09-08 --apply    # 执行
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

BASE = r"D:\狗屁通のA大奇妙冒险\A_stock_rotation"
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))
os.chdir(BASE)

from factor_fusion import _sql  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="目标交易日 YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="真正写库(默认 dry-run)")
    ap.add_argument("--backup-dir", default=os.path.join(BASE, "data", "backup"))
    a = ap.parse_args()
    D = str(a.day)[:10]

    # 1) 受影响行
    tgt = _sql(f"""
        SELECT ts, symbol, pe_ttm, pb, ps_ttm, pcf_ncf_ttm, is_st,
               market_cap, free_cap, float_shares, source
        FROM valuation
        WHERE CAST(ts AS DATE) = DATE '{D}'
        ORDER BY symbol
    """)
    if tgt.empty:
        print(f"[repair] {D} 在 valuation 中无行, 退出")
        return 1
    n_all = len(tgt)
    need = tgt["float_shares"].isna() & tgt["free_cap"].notna()
    print(f"[repair] {D}: 当日 {n_all:,} 行, 其中 float_shares 缺失且 free_cap 可用 "
          f"{int(need.sum()):,} 行")
    if not need.any():
        print("[repair] 无需修复, 退出")
        return 0

    # 2) price 取自同源 valuation_snapshot
    px = _sql(f"SELECT symbol, price FROM valuation_snapshot "
              f"WHERE CAST(ts AS DATE) = DATE '{D}'")
    px = px.copy()
    px["symbol"] = px["symbol"].astype(str).str.zfill(6)
    px = px.drop_duplicates("symbol", keep="last").set_index("symbol")["price"]
    tgt = tgt.copy()
    tgt["symbol"] = tgt["symbol"].astype(str).str.zfill(6)
    p = pd.to_numeric(tgt["symbol"].map(px), errors="coerce")
    fc = pd.to_numeric(tgt["free_cap"], errors="coerce")
    ok = need & p.notna() & (p > 0) & fc.notna() & (fc > 0)
    print(f"[repair] 可换算行 {int(ok.sum()):,} / 目标 {int(need.sum()):,}"
          f"  (缺 price 无法换算 {int((need & ~ok).sum()):,})")
    if not ok.any():
        print("[repair] 无可换算行, 退出")
        return 1
    new_fs = fc.where(~ok, fc / p.where(p > 0))
    if not a.apply:
        smp = pd.DataFrame({"symbol": tgt["symbol"][ok][:5],
                            "free_cap": fc[ok][:5],
                            "price": p[ok][:5],
                            "new_float_shares": new_fs[ok][:5]})
        print("\n[dry-run] 样例(前 5 行):")
        print(smp.to_string(index=False))
        print(f"\n[dry-run] 未写库。加 --apply 执行(先自动备份)。")
        return 0

    # 3) 备份受影响行
    os.makedirs(a.backup_dir, exist_ok=True)
    bpath = os.path.join(a.backup_dir,
                         f"valuation_{D.replace('-', '')}_pre_floatfix.parquet")
    tgt.to_parquet(bpath, index=False)
    print(f"[repair] 已备份当日 {n_all:,} 行 -> {os.path.relpath(bpath, BASE)}")

    # 4) 定位时间区间 (h5i plan_replace_range 的 start/end 是 **µs 时间戳**, 不是行号；
    #    首次尝试误传行号被引擎拒绝: "replacement row at time ... falls outside [...]")
    d0 = pd.Timestamp(D)
    d1 = d0 + pd.Timedelta(days=1)
    start = int(d0.value // 1000)
    end = int(d1.value // 1000)
    in_range = int(_sql(f"SELECT COUNT(*) n FROM valuation "
                        f"WHERE ts >= TIMESTAMP '{d0}' AND ts < TIMESTAMP '{d1}'")["n"][0])
    if in_range != n_all:
        print(f"[repair] *** 中止: 时间区间 [{start}, {end}) 内行数 {in_range:,} "
              f"!= 当日行数 {n_all:,} ***")
        return 1
    print(f"[repair] 时间区间 [{start}, {end}) 覆盖 {in_range:,} 行")

    # 5) 定点替换
    import pyarrow as pa

    from valuation_backfill import VAL_SCHEMA, _open_db
    out = tgt.copy()
    out["float_shares"] = new_fs.to_numpy()
    out["ts"] = pd.to_datetime(out["ts"]).dt.as_unit("us")
    out["is_st"] = pd.array(out["is_st"].astype("boolean"), dtype="boolean")
    tbl = pa.Table.from_pandas(out, schema=VAL_SCHEMA, preserve_index=False)
    db = _open_db()
    try:
        plan = db.plan_replace_range("valuation", start, end, data=tbl,
                                     note=f"float_shares 复原 {D} (观察期首日)")
        plan.apply()
    finally:
        db.close()
    print(f"[repair] 已替换 valuation 时间区间 [{start}, {end})")

    # 6) 验证
    chk = _sql(f"SELECT COUNT(*) n, COUNT(float_shares) n_fs FROM valuation "
               f"WHERE CAST(ts AS DATE) = DATE '{D}'")
    print(f"[repair] 验证: 当日 {int(chk['n'][0]):,} 行, float_shares 非空 "
          f"{int(chk['n_fs'][0]):,} 行")
    tot = int(_sql("SELECT COUNT(*) n FROM valuation")["n"][0])
    print(f"[repair] 全表行数 {tot:,} (修复前应为 15,416,967)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
