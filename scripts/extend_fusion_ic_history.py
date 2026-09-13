# -*- coding: utf-8 -*-
"""分块把融合因子逐日 IC 扩到全历史, 再合并(规避单次 2000 日的内存问题).

单次 days=2000 实测进程静默退出(样本量 ~10M, 疑似 OOM); days=242 耗时 77.5s,
线性外推 2000 日约 11 分钟。故按 400 日/块推进, 逐块取 daily_ic 后合并,
最终覆盖 data/factor_mine/fusion_ic_121d.json (门控读该路径)。
"""
import json
import os
import sys
import time

BASE = r"D:\狗屁通のA大奇妙冒险\A_stock_rotation"
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))
os.chdir(BASE)

from attribution_analysis import FUSION_IC_CACHE, FUSION_FACTOR_DESC, compute_fusion_ic  # noqa: E402
from vnpy_backtest import _full_calendar  # noqa: E402

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
CHUNK = int(sys.argv[2]) if len(sys.argv) > 2 else 400

cal = _full_calendar()
if len(cal) < TOTAL:
    print(f"[warn] 日历仅 {len(cal)} 个交易日, 按可用长度处理")
    TOTAL = len(cal)
seg = cal[-TOTAL:]
chunks = [seg[i:i + CHUNK] for i in range(0, len(seg), CHUNK)]
print(f"目标 {TOTAL} 个交易日, 分 {len(chunks)} 块(每块<= {CHUNK})", flush=True)

merged: dict[str, dict] = {}
t_all = time.time()
for i, ch in enumerate(chunks, 1):
    end = ch[-1]
    t0 = time.time()
    d = compute_fusion_ic(end=end, days=len(ch), refresh=True)
    ics = d.get("daily_ic") or []
    for e in ics:
        if e.get("date"):
            merged[e["date"]] = e
    print(f"  [{i}/{len(chunks)}] end={end} days={len(ch)} -> {len(ics)} 条 "
          f"({time.time() - t0:.0f}s)  累计 {len(merged)}", flush=True)

out = {
    "ok": True,
    "as_of_start": min(merged) if merged else None,
    "as_of_end": max(merged) if merged else None,
    "n_days": len(merged),
    "n_with_ic": sum(1 for v in merged.values()
                     if v.get("fwd1_ic") is not None or v.get("fwd5_ic") is not None),
    "elapsed_s": round(time.time() - t_all, 1),
    "factor": FUSION_FACTOR_DESC,
    "chunk_days": CHUNK,
    "daily_ic": [merged[k] for k in sorted(merged)],
}
os.makedirs(os.path.dirname(FUSION_IC_CACHE), exist_ok=True)
with open(FUSION_IC_CACHE, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"\n合并完成: {len(merged)} 个交易日 "
      f"({out['as_of_start']} ~ {out['as_of_end']}), 总耗时 {out['elapsed_s']}s")
print("已写入:", FUSION_IC_CACHE)
