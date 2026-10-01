# -*- coding: utf-8 -*-
"""一次性: 登记 `data/daily/` 覆盖诊断的真实结果(并更正先前的"09-25..09-27 缺目录"误判)。

## 本轮问的问题(用户清单)

> 检查 `run_daily` 在 09-09…09-20 有没有跑过;
> 查清 `data/daily/<date>/` 为什么没生成;
> 区分「生成时点漂移」与「校验窗口口径」。

## 实测答案(逐日, 见 `_tools/daily_coverage.py`)

对 2026-08-25 .. 2026-09-28 **每个交易日**逐一核对有没有
`data/daily/<该日>/selection.json`:

* 交易日 **24** 天, 其中 **9** 天**连目录都没有**:
  `20260909 20260910 20260911 20260914 20260915 20260916 20260917 20260918 20260928`
  —— 前 8 天连续, 正是守护进程中断窗口; **09-28 是今天, 属"尚未到 19:10"**, 不是缺口。
* **非交易日却有目录 5 天**: `20260830 20260905 20260925 20260926 20260927`
  —— 但里面**只有** `daily_summary.json` 与 `incremental_learn_heartbeat.json`,
  **没有 `selection.json`** ⇒ 来自非交易日维护分支, **不会污染第 2 档**
  (第 2 档只认 `selection.json`)。

## 更正

先前把「09-25/09-26/09-27 没有 `data/daily/`」当成异常。按日历这三天的
**交易日判定分别是 Fri=False / Sat=False / Sun=False** —— 非交易日不跑收盘选股,
**本来就不该有 `selection.json`**。故该条从缺口清单剔除。

**真正需要归因的只有 09-09..09-18 那 8 个交易日。**
"""
from __future__ import annotations

import json
import os

FP = os.path.join("ops", "acceptance_status.json")
d = json.load(open(FP, encoding="utf-8"))
items = d["items"]
by_id = {it["id"]: it for it in items}
NL = "\n"

DETAIL = NL.join([
    "## 实测(2026-09-28, `_tools/daily_coverage.py`, 只读)",
    "",
    "对 **2026-08-25 .. 2026-09-28** 区间内**每一个交易日**核对",
    "`data/daily/<该日>/selection.json` 是否存在:",
    "",
    "| 指标 | 值 |",
    "|---|---|",
    "| 区间内交易日数 | 24 |",
    "| **缺 selection.json 的交易日** | **9** |",
    "| 其中连目录都没有 | 9 |",
    "| 非交易日却产出目录 | 5 |",
    "",
    "### 缺 selection 的交易日明细",
    "",
    "```text",
    "20260909 20260910 20260911 20260914 20260915 20260916 20260917 20260918  <- 连续 8 天",
    "20260928                                                                <- 今天(尚未到 19:10)",
    "```",
    "",
    "⇒ **真缺口只有 09-09..09-18 那 8 个交易日**, 与守护进程中断窗口**完全重合**。",
    "09-28 未生成属正常(收盘选股 19:10 才跑)。",
    "",
    "### 非交易日却有目录的 5 天 —— 已核查, **无害**",
    "",
    "| 日期 | 星期 | 是交易日? | 目录内文件 |",
    "|---|---|---|---|",
    "| 20260830 | Sun | 否 | `daily_summary.json`, `incremental_learn_heartbeat.json` |",
    "| 20260905 | Sat | 否 | `daily_summary.json`, `incremental_learn_heartbeat.json` |",
    "| 20260925 | Fri | 否 | `daily_summary.json`, `incremental_learn_heartbeat.json` |",
    "| 20260926 | Sat | 否 | `daily_summary.json`, `incremental_learn_heartbeat.json` |",
    "| 20260927 | Sun | 否 | `daily_summary.json`, `incremental_learn_heartbeat.json` |",
    "",
    "**关键: 这 5 个目录里都没有 `selection.json`** —— 而回退梯子第 2 档只认",
    "`daily/<C>/selection.json` ⇒ **不会**把非交易日的空目录当成可用池。故无害。",
    "",
    "## 本轮对先前误判的更正",
    "",
    "先前记录里把「09-25 / 09-26 / 09-27 **没有** `data/daily/`」当作异常之一。",
    "**该说法错误, 现更正**:",
    "",
    "| 日期 | 星期 | `trading_calendar.is_trading_day` | 结论 |",
    "|---|---|---|---|",
    "| 2026-09-25 | Fri | **False** | 非交易日, 无 `selection.json` 是**正确行为** |",
    "| 2026-09-26 | Sat | **False** | 同上 |",
    "| 2026-09-27 | Sun | **False** | 同上 |",
    "",
    "(这三天的目录里其实有 `daily_summary.json`, 来自 `--maint` 分支 —— 但那是",
    "维护产物, 不是选股产物; 「有没有目录」这个问法本身就不对, 应该问",
    "「有没有 `selection.json`」。)",
    "",
    "## 与「12 天中断」条的关系",
    "",
    "本条与 `FINDING-DAEMON-GAP-0909-0920` **互为交叉验证**:",
    "· 本条从**产物侧**(磁盘上缺哪些目录)独立复现了缺口区间;",
    "· 那条从**日志侧**(daemon.log 逐日行数)独立给出同一区间。",
    "两侧**互相印证**, 缺口区间 09-09..09-18(交易日 8 天)可确认。",
    "",
    "## 仍未查(本轮未做)",
    "",
    "· 09-28 的收盘选股(19:10)是否会正常产出 —— **当时尚未到点**, 需盘后再看;",
    "· 09-09..09-18 期间 `data/daily/` 缺失时, 实盘引擎究竟取到了哪一份池",
    "  (live 台账只保留近几天, 该窗口**已无从查证** —— 见既有的",
    "  `FINDING-LEDGER-COVERS-NO-TRADE-DATA-FOR-0909-0920`)。",
])

NEW = [{
    "id": "FINDING-DAILY-COVERAGE-0909-0918",
    "level": "P1",
    "title": ("`data/daily/` 覆盖诊断: 24 个交易日中 **9 天**缺 `selection.json` —— "
              "真缺口为 09-09..09-18 连续 8 个交易日; "
              "09-25/26/27 是**非交易日**, 先前判为缺口属误判, 已更正"),
    "status": "open",
    "detail": DETAIL,
}]

added = 0
for e in NEW:
    if e["id"] in by_id:
        print("skip (已存在):", e["id"])
        continue
    items.append(e)
    added += 1

d["items"] = items
with open(FP, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)

print("added:", added, "| total items:", len(items))
