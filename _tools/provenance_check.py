# -*- coding: utf-8 -*-
"""判别 `data/_backup_before_dryrun/state.json` 的 provenance, 并重算持有期样本构成。

## 为什么要做这个(前几轮的一个潜在错误)

我在 `_tools/holding_period.py` 里把 `data/state.json`(3 天) 与
`data/_backup_before_dryrun/state.json`(9 天) 合并, 得出「生产实际持有期中位 1 天」。
但 `data/preflight_dryrun.json` 显示:

    tmp_data_dir = C:\\Users\\...\\Temp\\_interp_parity_B_blufu271
    prod_state_untouched = True

=> 该备份目录是**解释器一致性预检**在**隔离临时目录**里跑出来的产物,
**不是生产实盘的增量**。若它被算进"生产实际持有期", 那个结论的样本就**不纯**。

本脚本: ① 报两个来源各自的规模; ② 只用 `data/state.json`(明确的生产盘) 重算;
③ 明确"能用/不能用"的边界。
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_provenance.txt")
lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


LIVE = os.path.join(BASE, "data", "state.json")
BAK = os.path.join(BASE, "data", "_backup_before_dryrun", "state.json")
PRE = os.path.join(BASE, "data", "preflight_dryrun.json")

live = json.load(open(LIVE, encoding="utf-8"))
bak = json.load(open(BAK, encoding="utf-8"))

p("=" * 86)
p("1) 预检记录(判 provenance 的直接证据)")
p("=" * 86)
if os.path.exists(PRE):
    pr = json.load(open(PRE, encoding="utf-8"))
    p(f"  generated_at        = {pr.get('generated_at')}")
    p(f"  tmp_data_dir        = {pr.get('tmp_data_dir')}")
    p(f"  prod_state_untouched= {pr.get('prod_state_untouched')}")
    mb = pr.get("prod_state_md5_before") or {}
    ma = pr.get("prod_state_md5_after") or {}
    p(f"  md5 before/after 一致 = {mb.get('state.json') == ma.get('state.json')}")
    p("  => 预检在**临时目录**里跑, 并断言生产 state **未被触碰**。")
else:
    p("  (无 preflight_dryrun.json)")

p()
p("=" * 86)
p("2) 两个来源的规模")
p("=" * 86)


def stat(d, name):
    th = d.get("trades_history") or {}
    days = sorted(th)
    n = sum(len(v) for v in th.values())
    b = sum(1 for v in th.values() for t in v if t.get("type") == "buy")
    s = sum(1 for v in th.values() for t in v if t.get("type") == "sell")
    p(f"  {name:34} day={d.get('day')}  天数={len(days):>2}  笔数={n:>3} (买{b}/卖{s})")
    p(f"  {'':34} 覆盖 {days[0] if days else '-'} .. {days[-1] if days else '-'}")
    p(f"  {'':34} fees_paid={d.get('fees_paid')}  realized={d.get('realized')}  "
      f"positions={len(d.get('positions') or {})}")
    return set(days)


dl = stat(live, "live( data/state.json )")
db = stat(bak, "bak( _backup_before_dryrun )")
p()
p(f"  天数重叠: {sorted(dl & db) or '无'}")
p()

p("=" * 86)
p("3) 只用 live(明确的生产盘) 能观察到什么")
p("=" * 86)
th = live["trades_history"]
for k in sorted(th):
    n = len(th[k])
    b = sum(1 for t in th[k] if t.get("type") == "buy")
    p(f"  {k}: {n} 笔 (买{b}/卖{n-b})")
sells = [t for k in th for t in th[k] if t.get("type") == "sell"]
p()
p(f"  live 的卖出明细({len(sells)} 笔):")
for k in sorted(th):
    for t in th[k]:
        if t.get("type") == "sell":
            p(f"    {k}  {t.get('canon'):11} qty={t.get('qty'):>6} "
              f"px={t.get('price')} pnl={t.get('pnl')}")
p()
p(f"  => live 卖出仅 **{len(sells)} 笔** ⇒ **不足以做持有期分布或卖出条件归因**。")
p()

p("=" * 86)
p("4) 因此: 前几轮那个「中位持有期 1 天」的样本构成")
p("=" * 86)
bs = [t for k in db for t in bak["trades_history"][k] if t.get("type") == "sell"]
ls = len(sells)
p(f"  bak(预检回放) 卖出: {len(bs)} 笔")
p(f"  live(生产盘) 卖出: {ls} 笔")
p(f"  合计: {len(bs) + ls} 笔")
if len(bs) + ls:
    p(f"  => bak 占 **{100*len(bs)/(len(bs)+ls):.0f}%**")
p()
p("  **结论: 那个「中位持有期 1 天」主要来自预检隔离产物, 不是生产实盘。**")
p("  必须更正: 它**不能**作为「生产实际持有期」的证据。")
p()
p("  正确表述应为:")
p("    · **代码可证**: `min_hold_days` 只约束「离开目标池」一条路径;")
p("      止损与风控在它之外(`realtime_engine.py` L951-981 vs L983-1049),")
p("      且注释明写「止损/风控除外」;")
p("    · **实测可证(待补)**: 生产实盘的持有期分布 —— 需累积更长的 `state.json` 台账")
p("      (当前仅 3 天 / 3 笔卖出)。")

p()
p("=" * 86)
p("5) 对已登记结论的影响(诚实清单)")
p("=" * 86)
p("  · `FINDING-BACKTEST-VS-PRODUCTION-HOLDING-GAP`: 其「生产实际中位 1 天」**证据不纯**,")
p("    需降级为「待重测」; 但「回测无 min_hold」这一半仍然成立(代码可证);")
p("  · `docs/hist-window-protocol.md` §3b 里的「中位 1 天」需加限定;")
p("  · `_tools/holding_period.py` 的合并逻辑需标注数据来源与纯度。")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)
