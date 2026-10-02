# -*- coding: utf-8 -*-
"""一次性: 登记本轮三项(档位命中率 / 切换日志 / 7 天窗口不可诊断)。

## 本轮实证

· **`drl_same_day` 命中率 = 0**(37 条留痕里**: 0 次**);
  `selection_same_day` 22 次(59.5%) / `drl_cross_day` 15 次(40.5%);
· 日志实测: `drl/*/target_plan` 被**逐条"校验未过"**跳过
  (因 `generated_at` 不在 `[前一交易日16:00, 当日00:00)`);
· `data/daily/` 在 09-08 与 09-21 之间**完全没有目录** ⇒ 那 10 天第 2 档无可选;
· 因此 09-10..09-18 复用 `daily/20260908` 的池 —— **不是梯子回退到更早日期,
  而是"根本没有更新的文件"**。
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
    "id": "FINDING-DRL-SAME-DAY-RUNG-NEVER-HITS",
    "level": "P1",
    "title": ("回退梯子第 1 档(`drl_same_day`)**命中率 0%** —— "
              "所有 target_plan 都被「校验未过」跳过"),
    "status": "open",
    "detail": NL.join([
        "## 实测(2026-09-27, `data/targets_source.jsonl` 37 条留痕)",
        "",
        "| 档位 | 命中次数 | 占比 |",
        "|---|---|---|",
        "| `selection_same_day` | **22** | **59.5%** |",
        "| `drl_cross_day` | **15** | **40.5%** |",
        "| **`drl_same_day`** | **0** | **0%** |",
        "",
        "⇒ **第 1 档从未命中**。而代码里它是**优先档**(`_select_targets_hist` 第 1 段)。",
        "",
        "## 为什么(日志直接给出原因)",
        "",
        "`drl/*/target_plan` 被**逐条**跳过, 理由是校验窗口:",
        "",
        "```text",
        "drl/20260907 target_plan 校验未过, 跳过:",
        "  generated_at 2026-09-20 20:20:07",
        "  不在正式窗口 [2026-09-07 16:00:00, 2026-09-08 00:00:00)",
        "drl/20260905 ... generated_at 2026-09-05 15:46:50 ...",
        "drl/20260904 ... 2026-09-04 17:55:59 ...",
        "  (以下 09-03 / 09-02 / 09-01 / 08-30 / 08-28 / 08-27 / 08-26",
        "    以及 2021/2020/2019/2018 的历史目录全部同样「校验未过」)",
        "```",
        "",
        "**校验语义**(`_plan_is_formal`): 计划必须生成于**前一交易日 16:00 之后、当日 00:00 之前**",
        "—— 即「收盘后、开盘前」生成。实测各 plan 的 `generated_at` 全部落在这个窗口之外。",
        "",
        "## 两个可能的解释(本轮**未**区分)",
        "",
        "1. **流程问题**: `target_plan` 的生成时点确实漂到了别处",
        "   (例如 09-20 20:20 生成了一份**标称 09-07** 的计划 —— 标称与生成时点不一致);",
        "2. **口径问题**: 校验窗口按「自然日 16:00–24:00」, 而实际生成发生在别的时点,",
        "   两者设计不同源。",
        "",
        "⇒ **在区分之前不要动校验逻辑** —— 它同时是防前视的闸门。",
    ]),
    "evidence": NL.join([
        "`data/targets_source.jsonl`(37 条留痕, 字段 at/consume_day/sel_day/rung/n);",
        "`logs/*.log` 中 `drl/*/target_plan 校验未过` 逐条记录(本轮实测复现);",
        "`src/realtime_engine.py::_try_load_daily_plan` + `_plan_is_formal`;",
        "`src/portfolio_live.py::make_targets_of(store=None, live_pool=False)` 调",
        "`BacktestRunner._select_targets_hist`。",
    ]),
    "next": NL.join([
        "① 查 `target_plan.json` 的 `generated_at` 与它所在目录日期的**分布关系**",
        "   (是否系统性错位);",
        "② 若确认是**生成时点漂移**, 修生成侧; 若确认是**校验窗口口径**, 改校验 ——",
        "   **两者处置相反, 必须先区分**;",
        "③ 第 1 档长期 0% 意味着**回测与实盘都拿不到「当日正式计划」**,",
        "   这可能是「回测≠实盘」的又一个来源。",
    ]),
}, {
    "id": "FINDING-DAILY-SELECTION-GAP-0909-0920",
    "level": "P1",
    "title": ("`data/daily/` 在 **09-09..09-20 完全没有目录** ⇒ 那 10 天第 2 档无可选, "
              "只能复用 `daily/20260908`"),
    "status": "open",
    "detail": NL.join([
        "## 实测(2026-09-27)",
        "",
        "`data/daily/` 的日期目录(筛选 09-08..09-21):",
        "",
        "```text",
        "20260908  selection=True",
        "20260921  selection=True          <-- 中间 12 天没有任何目录",
        "```",
        "",
        "`data/drl/` 对应区间:",
        "",
        "```text",
        "20260907  plan=True",
        "20260908  plan=False              <-- 有目录但**没有 target_plan**",
        "20260921  plan=True",
        "```",
        "",
        "⇒ 09-09..09-18 这些决策日:",
        "· 第 1 档(当日/跨日 DRL plan) — 全部「校验未过」(见并列条);",
        "· 第 2 档(selection) — **没有比 09-08 更新的文件**;",
        "⇒ 梯子**只能**一直返回 `daily/20260908` 的池。",
        "",
        "## 结论: **不是「梯子回退到更早日期」, 而是「根本没有更新的文件」**",
        "",
        "用户问的「为什么回退到 09-08 而不是更近的日期」—— 答案是**没有更近的**。",
        "这直接解释了本仓另一条发现里的现象:",
        "`daily/20260908/selection.json` 被**跨 10 天复用**(滞后 2→10 天),",
        "以及目标池日度重叠在那 7 天**恒为 10/10**。",
        "",
        "## 为什么重要",
        "",
        "1. **这不是梯子的 bug** —— 梯子按「取 C<D 的最近可用」工作, 而最近可用的就是 09-08;",
        "2. **真正的问题是上游没有产出**: 12 天里 `data/daily/` 一个目录都没写。",
        "   是「没跑」还是「跑了没落盘」, 本轮**未查**;",
        "3. 这也解释了为何那 7 天的池完全不变 —— 它们本来就是**同一份文件**。",
    ]),
    "evidence": NL.join([
        "实测 `Get-ChildItem data/daily -Directory` 与 `data/drl -Directory` 的日期清单;",
        "`_tools/overlap.py` 第 4 节的来源反查(集合相等匹配)与滞后表;",
        "`data/targets_source.jsonl` 中该区间的 `sel_day` 恒为 `20260921`(留痕口径)。",
    ]),
    "next": NL.join([
        "① 查 09-09..09-20 的 `run_daily` 是否**跑过**、`select` 步骤是否成功",
        "   (对比 `data/daily/<day>/daily_summary.json` 是否存在);",
        "② 若跑过但没写 `selection.json` ⇒ 是**落盘问题**;若没跑 ⇒ 是**调度问题**;",
        "③ 该窗口是**天然对照**: 池不变, 若持有期仍短, 则卖出与池变动无关 ——",
        "   但**台账在 09-10..09-18 为空**(live 只到 09-22, bak 只到 09-09)",
        "   ⇒ 该对照**当前做不了**, 见并列条。",
    ]),
}, {
    "id": "FINDING-LEDGER-COVERS-NO-TRADE-DATA-FOR-0909-0920",
    "level": "P2",
    "title": ("台账在 09-09..09-20 **完全无记录** ⇒ "
              "「池不变期间卖出统计」这一诊断当前做不了"),
    "status": "open",
    "detail": NL.join([
        "## 实测(2026-09-27)",
        "",
        "针对「池完全相同的 7 天(09-10..09-18)」统计卖出:",
        "",
        "| 台账 | 覆盖该窗口天数 | 卖出 | 买入 |",
        "|---|---|---|---|",
        "| `data/state.json`(生产盘) | **0/7** | 0 | 0 |",
        "| `data/_backup_before_dryrun/state.json`(预检) | **0/7** | 0 | 0 |",
        "",
        "原因: 两个台账的**覆盖区间都不含该窗口** ——",
        "· live: 09-22..09-24(3 天);",
        "· bak:  08-27..09-09(9 天)。",
        "",
        "⇒ 用户设想的判据(「若卖出 > 0 => 卖出不是池变动造成的」/「若 = 0 => 池变动是主因」)",
        "**在本窗口上无法执行** —— 不是「卖出为 0」, 而是**没有记录**。",
        "",
        "## 为什么必须写下来",
        "",
        "「卖出 = 0」与「无记录」在数值上**都是 0**, 但含义相反。",
        "若不写清, 后人会把「无记录」读成「没有卖出」, 从而**反向**得出结论",
        "(误以为池变动确实不引发卖出)。这是本仓反复出现的形态:",
        "**缺失被读成零**。",
    ]),
    "evidence": NL.join([
        "实测: 对 `2026-09-10/11/14/15/16/17/18` 逐日统计两台账的 `trades_history`,",
        "两边的覆盖天数均为 0;",
        "live 覆盖 09-22..09-24 / bak 覆盖 08-27..09-09(见 `_tools/provenance_check.py`)。",
    ]),
    "next": NL.join([
        "① 该诊断**等台账累积**到覆盖 09-10..09-18 才能做 —— 而 live 台账只保留最近几天,",
        "   故实际上**永远补不回来**;",
        "② 若确需该对照, 只能**重放**(用当时的 target_plan/selection 与行情重跑),",
        "   但那会引入重放口径, 不能当生产证据;",
        "③ **判据**: 报「0 笔」前先确认台账**覆盖**了该区间 —— 覆盖为 0 时报「无记录」。",
    ]),
}]

added = 0
for e in NEW:
    if e["id"] in have:
        print("skip (已存在):", e["id"])
        continue
    items.append(e)
    added += 1

d["generated_at"] = "2026-09-28"
with open(FP, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print("added:", added, "| total items:", len(items))
