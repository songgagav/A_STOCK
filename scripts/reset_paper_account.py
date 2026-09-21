# -*- coding: utf-8 -*-
"""重置虚拟盘账户：初始资金 10 万、零持仓（最小差异改写，保留 schema 每一个键）。

为什么是"最小差异"而不是整文件替换
----------------------------------
实测 `PaperBook.snapshot()` 与 `data/state.json` **不是同一套 schema**:

| 现有 state.json | snapshot() |
|---|---|
| `day, cash, positions, equity, realized, peak_equity,` `top_targets, trades_history, applied_corp_actions, risk_state …` | `date, init_capital, market_value, open_positions,` `drawdown_pct, risk_log, trades_today …` |

`snapshot()` 缺 `day`(它给的是 `date`)/`peak_equity`/`top_targets`/`trades_history`/
`applied_corp_actions` —— **直接覆写会丢字段**, 明天引擎可能读不到。
故本脚本**只改账户字段, 其余键一律原样保留**。

两处**刻意不动**的字段(动了反而是引入风险):
· `applied_corp_actions` —— 它是"已入账除权/分红"的去重凭据; 清空会**移除一道防重复入账的闸**。
  且持仓已清空, 留着它无副作用。
· 其余任何未知键 —— 一律原样保留(不猜语义)。

安全措施
--------
1. 落盘前**自动备份**到 `E:\\A_stockDB_backup_20260920_2026\\paper_reset_<时间戳>\\`;
2. 落盘后用 `PaperBook.restore()` **回读校验**(确认引擎真能读: 资金=10万、零持仓);
3. 默认预演, 需显式 `--apply` 才写。

用法
  python scripts/reset_paper_account.py            # 预演(打印差异)
  python scripts/reset_paper_account.py --apply    # 落盘 + 回读校验
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

BACKUP_ROOT = r"E:\A_stockDB_backup_20260920_2026"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    import config
    import paper_book as PB

    fp = config.STATE_FILE
    init = float(config.INIT_CAPITAL)
    print("=" * 74)
    print(f"重置虚拟盘账户  {fp}")
    print("=" * 74)
    if not os.path.isfile(fp):
        print(f"  [FAIL] 不存在: {fp}")
        return 2
    with open(fp, encoding="utf-8-sig") as f:
        cur = json.load(f)
    new = dict(cur)          # ★ 先整体复制 —— 未列出的键一律原样保留

    want = {
        "cash": init, "equity": init, "realized": 0.0, "unrealized": 0.0,
        "fees_paid": 0.0, "buy_fees": 0.0, "sell_fees": 0.0, "attributed_pnl": 0.0,
        "peak_equity": init,          # 峰值重置, 否则回撤熔断会按旧峰值判定
        "risk_state": "NORMAL",       # 与新峰值/新权益一致
        "positions": {}, "trades_history": {}, "top_targets": [],
    }
    print("\n  --- 差异（只动表内字段；未列出的键原样保留）---")
    for k, v in want.items():
        if k in cur:
            ov = cur[k]
            ov = ("%d 项" % len(ov)) if isinstance(ov, (list, dict)) else ov
            nv = ("%d 项" % len(v)) if isinstance(v, (list, dict)) else v
            print(f"    {k:<20} {ov}  ->  {nv}")
            new[k] = v
        else:
            print(f"    {k:<20} (原文件无此键 -> 不新增, 避免改 schema)")
    kept = sorted(set(cur) - set(want))
    print(f"\n  原样保留的键 ({len(kept)}): {kept}")

    if not a.apply:
        print("\n(--预演: 未落盘; 加 --apply 执行)")
        return 0

    # ---- 落盘前备份 ----
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bk = os.path.join(BACKUP_ROOT, f"paper_reset_{stamp}")
    os.makedirs(bk, exist_ok=True)
    for name in ("state.json", "live_state.json"):
        src = os.path.join(config.DATA_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(bk, name))
    print(f"\n  [备份] {bk}")
    for name in sorted(os.listdir(bk)):
        p = os.path.join(bk, name)
        print(f"    {name}  {os.path.getsize(p):,} B")

    with open(fp, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
    print(f"  [已写入] {fp}")

    # ---- 回读校验: 用引擎自己的 restore 路径 ----
    with open(fp, encoding="utf-8-sig") as f:
        back = json.load(f)
    pb = PB.PaperBook(init_capital=init)
    pb.restore(back)
    snap = pb.snapshot()
    checks = [
        ("键集合与原文件一致(未改 schema)", sorted(back) == sorted(cur)),
        ("cash == 初始资金", abs(float(back.get("cash", -1)) - init) < 1e-6),
        ("零持仓", len(back.get("positions") or {}) == 0),
        ("PaperBook.restore 后可读", True),
        ("restore 后 open_positions == 0", int(snap.get("open_positions", -1)) == 0),
        ("restore 后 equity == 初始资金", abs(float(snap.get("equity", -1)) - init) < 1e-6),
        ("峰值已重置", abs(float(back.get("peak_equity", -1)) - init) < 1e-6),
    ]
    print("\n  --- 回读校验（用 PaperBook.restore 这条真实路径）---")
    bad = 0
    for name, ok in checks:
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n  snapshot: cash={snap.get('cash')} equity={snap.get('equity')} "
          f"open_positions={snap.get('open_positions')} risk_state={snap.get('risk_state')}")
    if bad:
        print(f"\n[FAIL] {bad} 项校验未过 —— 请用 {bk} 还原")
        return 1
    print("\n[PASS] 重置完成且引擎可读")
    return 0


if __name__ == "__main__":
    sys.exit(main())
