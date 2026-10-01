# -*- coding: utf-8 -*-
"""一次性: 登记「h5i 不存 OHLC 导致回测无法按开盘价撮合」的待办。

背景(本轮实测): 生产成交锚 **open**(09:30), 回测锚 **close**; 两者不等价
(配对 t=+1.16, n=51 => 方向明确但幅度不显著)。而"把回测改成开盘成交"需要先补数据管道 ——
**厂商引擎有 OHLC, 落 h5i 时被丢掉**。
"""
from __future__ import annotations

import json
import os

FP = os.path.join("ops", "acceptance_status.json")
d = json.load(open(FP, encoding="utf-8"))
items = d["items"]
have = {it["id"] for it in items}
NL = "\n"

NEW = [{
    "id": "TODO-H5I-DROPS-OHLC",
    "level": "P1",
    "title": ("h5i 落库时丢掉 OHLC(只留 close) ⇒ 回测无法按开盘价撮合, "
              "与生产成交时点(09:30)不一致"),
    "status": "open",
    "detail": NL.join([
        "## 实测事实(2026-09-27)",
        "",
        "| 源 | 列 |",
        "|---|---|",
        "| **h5i** `bars_on_day` | `['symbol','close','change_pct','turnover','amount']` —— **无 OHLC** |",
        "| **厂商引擎** `engine_bars_sync.fetch_day()` | `['symbol','date','open','high','low','close','volume','amount','change_pct','turnover']` —— **有 OHLC** |",
        "",
        "⇒ **开盘价在源头可得, 是在落 h5i 时被丢掉的** —— 不是「没有这个数据」, 而是「没存」。",
        "",
        "## 为什么这件事有后果",
        "",
        "1. **生产成交锚在开盘**: 生产成交时间实测集中在 `09:30:06 ~ 09:30:21`;",
        "   实测 |成交价/open − 1| 中位 **0.41%** vs |成交价/close − 1| 中位 **0.72%**",
        "   ⇒ 生产确实锚 open(偏离更小);",
        "2. **回测锚在收盘**: `portfolio_backtest` 用 `price_of(day, canon)`(收盘价)撮合;",
        "3. ⇒ **两者取价不同**, 抵消不掉(买单在收盘=买当日更高价, 但往返上漂移只部分抵消);",
        "4. 更直接的是: **没有 OHLC 就无法复现实盘的成交时点**, 也无法做任何日内口径的复核",
        "   (例如「开盘买入」策略的回测、盘中止损的复现)。",
        "",
        "## 量级(诚实版: 方向明确, 幅度不显著)",
        "",
        "同一批 51 笔生产往返:",
        "",
        "| 口径 | 均值 | 中位 |",
        "|---|---|---|",
        "| 生产(实际成交价, 锚 open) | +0.3348% | +0.0882% |",
        "| 回测(收盘→收盘) | -0.2303% | 0.0000% |",
        "| **差** | **+0.5652pp / 往返** | **+0.3277pp** |",
        "",
        "**配对统计: n=51, sd=3.4813, se=0.4875, t=+1.16 ⇒ 不显著**;",
        "正号 31/51=61%; 最大 3 个 |差| = 18.03/11.18/6.21pp ⇒ **均值被离群点拉动**。",
        "⇒ 只能下「**两者不等价**」, **不能**下「差 0.57pp」—— 那是单月单批的估计。",
        "",
        "## 与既有教训的关系",
        "",
        "「回测池与虚拟盘池分叉」的**第三个实例**: 名字相同、跑的不是同一件事。",
        "前两个是 ①池口径分叉(已修) ②`min_hold_days` 未实现(本轮已修);",
        "本条是 ③**成交时点分叉(未修, 因缺数据)**。",
        "",
        "## 建议做法(按代价排序)",
        "",
        "```text",
        "① 最小改动: 在 hist-window-protocol.md §3b 强制标注该差异(本轮已做) ——",
        "   任何回测结论引用时都要带口径说明;",
        "② 中等: 让 h5i_sync 把 open/high/low 一并落库(源已有, 只改落库层),",
        "   历史缺口用 baostock 回填路径补(其 FIELDS 已含 open/high/low);",
        "③ 较大: 撮合层支持选择 open/close 价位, 并与实盘对照验证。",
        "```",
    ]),
    "evidence": NL.join([
        "`_tools/timing_gap.py`(可复现): 对比 115 笔成交价 vs open/close;",
        "10 个交易日的全市场开->收漂移; 51 笔 FIFO 配对 + 配对 t 检验;",
        "h5i 与引擎列名实测输出; 结论写入 `docs/hist-window-protocol.md` §3b + 必报项第 6 条;",
        "`tests/test_backtest_interpretation.py` 新增守卫(锁该文档含本标注)。",
    ]),
    "next": NL.join([
        "先只用**最小改动**(强制标注), 不要急着改管道 —— 因为幅度**不显著**(t=1.16),",
        "改管道的收益尚不足以证明其代价。**触发条件**: 若将来要做的策略变更依赖开盘价",
        "(如盘中止损/开盘买入), 或配对样本扩到 n>=200 后 t 仍 >=2, 再动 ②/③。",
    ]),
}]

added = 0
for e in NEW:
    if e["id"] in have:
        print("skip (已存在):", e["id"])
        continue
    items.append(e)
    added += 1

d["generated_at"] = "2026-09-27"
with open(FP, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print("added:", added, "| total items:", len(items))
