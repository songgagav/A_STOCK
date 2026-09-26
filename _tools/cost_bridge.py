# -*- coding: utf-8 -*-
"""P0 验收: 补上 min_hold_days 后重跑, 并做**精确成本桥接**(注入式 book_factory)。

## 三件事

1. **验收 min_hold 修复**: 同一窗口重跑, 看 `turnover_pct` 是否从 484% 降下来,
   以及收益是否改善 —— 这验证"484% 是**回测口径**问题, 不是生产问题"。
2. **精确成本桥接**: 用 `book_factory` 注入一个把 `PAPER` 费用/滑点归零的账本,
   重跑同窗口。`total_return` 的差就是**精确**的成本贡献(不再用估算上界)。
3. **持有期规则对比**: min_hold ∈ {2, 10, 20} 各跑一次(靠覆盖 `PAPER`),
   看换手与收益怎么变 —— 为"持有期应匹配 IC 达峰周期"提供数据。

## 为什么用注入 book_factory 而不是改 config

`paper_book` 是 `from config import (... PAPER ...)`, 即**模块级绑定**。
故 patch `paper_book.PAPER`(而非 `config.PAPER`)才会生效 —— 这是本次的关键细节,
改错对象会"看起来 patch 了但没生效"(本仓 ⑤ 形态)。

不改仓库里任何持久配置: 每次运行结束恢复原值。
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import portfolio_backtest as PB  # noqa: E402
import portfolio_live as PL  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cost_bridge.txt")
lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


bt = json.load(open("data/portfolio_backtest_latest.json", encoding="utf-8"))
WIN = list(bt["days"])
p(f"窗口: {WIN[0]} .. {WIN[-1]}  ({len(WIN)} 天)")
p(f"改造前产物: turnover={bt['turnover_pct']}%  return={bt['total_return_pct']}%  "
  f"trades={bt['trades']}  max_dd={bt['max_drawdown_pct']}%")
p()


@contextlib.contextmanager
def paper_override(**kw):
    """临时改 `paper_book.PAPER` 的若干键(模块级绑定, 必须 patch 这里)。"""
    import paper_book
    old = copy.deepcopy(paper_book.PAPER)
    try:
        paper_book.PAPER.update(kw)
        yield paper_book.PAPER
    finally:
        paper_book.PAPER.clear()
        paper_book.PAPER.update(old)


def run(label: str, **paper_kw) -> dict:
    with paper_override(**paper_kw):
        r = PL.run_live_portfolio_backtest(WIN, init_capital=bt.get("init_capital"))
    if not r.get("ok"):
        p(f"  [{label}] FAIL: {r.get('error')}")
        return {}
    p(f"  [{label}] return={r['total_return_pct']:+.4f}%  "
      f"turnover={r['turnover_pct']:.2f}%  trades={r['trades']}  "
      f"max_dd={r['max_drawdown_pct']:.4f}%")
    return r


p("=== A) 补 min_hold_days 之后(生产口径: min_hold=2) ===")
base = run("min_hold=2 (生产)")
p()

p("=== B) 精确成本桥接: 费用与滑点归零 ===")
zero = run("零成本", commission=0.0, stamp_tax=0.0, transfer_fee=0.0,
           slippage=0.0, impact_cost=0.0)
p()

p("=== C) 持有期规则对比(覆盖 min_hold_days) ===")
rules = {}
for mh in (0, 2, 5, 10, 20):
    rules[mh] = run(f"min_hold={mh}", min_hold_days=mh)
p()

p("=== 桥接表(生产口径 min_hold=2) ===")
if base and zero:
    p(f"  {'项':<28}{'数值':>12}")
    p(f"  {'-'*40}")
    p(f"  {'零成本(毛)收益':<28}{zero['total_return_pct']:>+11.4f}%")
    p(f"  {'生产口径(净)收益':<28}{base['total_return_pct']:>+11.4f}%")
    p(f"  {'=> 成本贡献':<28}"
      f"{base['total_return_pct'] - zero['total_return_pct']:>+11.4f}pp")
    p(f"  {'换手(生产口径)':<28}{base['turnover_pct']:>11.2f}%")
    if base["turnover_pct"] > 0:
        per = (base['total_return_pct'] - zero['total_return_pct']) / base['turnover_pct'] * 100
        p(f"  {'=> 每 1% 换手的成本(bp)':<28}{per*100:>11.2f}bp")
    p()
    p(f"  {'改造前(无 min_hold)':<28}{bt['total_return_pct']:>+11.4f}%   "
      f"turnover={bt['turnover_pct']:.2f}%")
    p(f"  {'=> min_hold 修复带来的改善':<28}"
      f"{base['total_return_pct'] - bt['total_return_pct']:>+11.4f}pp")

p()
p("=== 持有期规则汇总 ===")
p(f"  {'min_hold':>9}{'换手%':>12}{'收益%':>12}{'交易数':>9}{'最大回撤%':>12}")
for mh, r in rules.items():
    if r:
        p(f"  {mh:>9}{r['turnover_pct']:>12.2f}{r['total_return_pct']:>+12.4f}"
          f"{r['trades']:>9}{r['max_drawdown_pct']:>12.4f}")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)
