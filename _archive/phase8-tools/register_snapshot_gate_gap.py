# -*- coding: utf-8 -*-
"""一次性: 登记「健康快照的数据源门禁从未生效」这一独立缺陷.

## 结论(三问的答案)

**问 1: 「数据源被判定」需要什么条件?**

`datasource_gate.evaluate` 的 `items` **只**由三个入参的**非 None** 决定:

```python
if engine_probe   is not None: add("stockdb_engine",    classify_engine(engine_probe))
if sync_step      is not None: add("engine_bars_sync",  classify_sync(sync_step))
if db_update_step is not None: add("db_update",         classify_db_update(db_update_step))
...
if not items: level = UNKNOWN        # "没有任何数据源被判定 —— 门禁未生效(不等于健康)"
```

而 `health_state.gather`(守护每 ~5 分钟发布快照的采集层)**只**传后两个, 且它们的
**唯一来源**是 `data/daily/<**今天**>/daily_summary.json` 的
`steps.engine_bars_sync` / `steps.db_update`(见 `health_state.py:219-227`)。

⇒ 条件 = **今天的 `daily_summary.json` 已落盘且带那两个 step**。
   而该文件由 `run_daily` 在 **19:10** 才写 ⇒ **每天 19:10 之前必然 `UNKNOWN`**。

**问 2: 厂商正常时这个条件是否满足?**

**关键**: 即便那两个 step 存在(19:10 之后), 快照路径判的也**只有两个辅助源** ——
`CRITICAL_SOURCES = (SRC_ENGINE,)`, 而 `stockdb_engine` **不在** items 里。
辅助源按 L538-546 只让 `level=DEGRADED`, **永不计入 `halt_sources`**。

⇒ **厂商正常时它仍然永远不会 HALT。** 快照路径的档位在结构上只能是
   `OK / DEGRADED / UNKNOWN`, **`HALT` 不可达**;
   而 `health_state.py:158` 那段「把 HALT 抬成 HALTED」也就**恒不执行**。

**问 3: 不满足 ⇒ 独立问题?**

**是, 独立于厂商。** 证据: `evaluate(engine_probe=...)` 全仓**只有** `run_daily`
一处调用(`run_daily.py:637`, 摄入前)。快照路径不是"厂商坏了才读不到",
而是**结构上从不读取那个关键源**。
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
    "## 实测(2026-09-28, 只读复现: `_tools/diagnose_gate_verdict.py`)",
    "",
    "### 判据参数",
    "",
    "| 参数 | 值 |",
    "|---|---|",
    "| `FAILS_TO_HALT` | 3(连续 3 轮失败才 HALT) |",
    "| `PUBLISHER_GRACE_TRADING_DAYS` | 1(落后 ≤1 个交易日视为正常发布节奏) |",
    "| `ENGINE_LAG_HALT_TRADING_DAYS` | 3 |",
    "| `CRITICAL_SOURCES` | **`('stockdb_engine',)`** —— 只有它能致 HALT |",
    "",
    "### 两个入口的判定结果(同一时刻、同一份落盘 steps)",
    "",
    "| 入口 | 调用形态 | items | level | allow |",
    "|---|---|---|---|---|",
    "| **A 快照路径** | `evaluate(sync_step, db_update_step)`(health_state.gather 的做法) | 2(全是辅助源) | DEGRADED | **True** |",
    "| **B 开盘路径** | `evaluate(engine_probe, sync_step, db_update_step)`(run_daily 的做法) | 3 | **HALT** | **False** |",
    "",
    "同一时刻 A 说 `allow=True`、B 说 `allow=False` —— 而 B **才是权威**。",
    "",
    "### 三个后果",
    "",
    "**① `HALT` 在快照路径不可达(结构性)**",
    "",
    "`stockdb_engine` 只在 `engine_probe` 非 None 时入 items, 而快照路径不传它 ⇒",
    "唯一的关键源缺席 ⇒ `halt_sources` 恒空 ⇒ 档位只能是 `OK/DEGRADED/UNKNOWN`。",
    "`health_state.py:158` 的「HALT -> HALTED」因此**恒不执行**。",
    "而**探针本身是好的**: `DG.probe_engine()` 实测输出含 `freshness`,",
    "`classify_engine` 正确判成 `engine_lag_over_grace`(落后 2 > 宽限 1)——",
    "快照路径**只是不用它**。",
    "",
    "**② 交易时段读的是「昨天那一轮」**",
    "",
    "输入取自 `daily/<**今天**>/daily_summary.json`, 而它 19:10 才写。",
    "故 19:10 之前, `gather()` 要么读不到(`UNKNOWN`), 要么(若文件已存在)",
    "描述的是**上一轮**的门禁输入。实测 2026-09-24 逐时形态:",
    "",
    "```text",
    "UNKNOWN  : 177 条   00:03 .. 19:08     <- 覆盖**整个交易日**",
    "DEGRADED :  56 条   19:17 .. 23:56     <- 收盘管道跑完之后才有判定",
    "```",
    "",
    "⇒ **交易时段内, 守护的健康快照里没有任何当日的门禁判定。**",
    "",
    "**③ 最要紧的一类不一致: 它可能与落盘结论相反**",
    "",
    "2026-09-27 实测:",
    "",
    "| 来源 | level | allow |",
    "|---|---|---|",
    "| **当日落盘的** `steps.datasource_gate`(run_daily 判的, 权威) | **HALT** | **False** |",
    "| 快照路径重算出来的 | DEGRADED | **True** |",
    "",
    "原因: 该日 `engine_bars_sync` / `db_update` 都带 HALT 的 skip 标记 ⇒ 被判",
    "`sync_failed` / `no_table_detail`, 但两者都是**辅助源** ⇒ 只 DEGRADED、不停手,",
    "**看不出**真正致停的是缺席的 `stockdb_engine`。",
    "",
    "### 与 09-21 的对照(用户清单第二问)",
    "",
    "**09-21 没有可对照的基线**: 逐快照统计显示该日 8 条快照全部**没有数据源项**",
    "—— `datasource` 这一段是 09-22 批次(门禁)才引入的。故「厂商正常时它是否生效」",
    "无法用 09-21 回答。",
    "",
    "改用逐日形态统计回答(每 5 分钟一条快照):",
    "",
    "| 日期 | 无数据源项 | UNKNOWN(未生效) | DEGRADED(有判定) | HALT |",
    "|---|---|---|---|---|",
    "| 2026-09-21 | 8 | 0 | 0 | 0 |",
    "| 2026-09-22 | 138 | 0 | 48 | 0 |",
    "| 2026-09-23 | 0 | 177 | 36 | 22 |",
    "| 2026-09-24 | 0 | 177 | 56 | 0 |",
    "| 2026-09-25 | 0 | 46 | 28 | 0 |",
    "| 2026-09-26 | 0 | 45 | 27 | 0 |",
    "| 2026-09-27 | 0 | 45 | 0 | 26 |",
    "",
    "**HALT 并非不可达** —— 09-23 / 09-27 各出现过(22 / 26 条)。但那是",
    "`daily_summary` 里**已落盘的**权威结论经由别的消费方呈现, 或辅助源连续失败所致;",
    "**不是快照路径自己判出来的 `stockdb_engine`**。故不改变上述结论。",
    "",
    "## 为什么这仍然是缺陷(而不是「设计如此」)",
    "",
    "· 「没有任何数据源被判定 —— 不等于健康」这条纪律本身是对的(DISC-2 同源);",
    "· 但它在**每个交易日的全部交易时段**都会触发, 于是这句警告退化成**背景噪音**",
    "  —— 而噪音化的警告等于没有警告(⑥ 号形态的近亲);",
    "· 更要紧的是方向: 门禁真正的`唯一关键源`在快照里**从不被判定**, 使得",
    "  「门禁说要停手」这件事**无法从守护自己的快照里看出来**。",
    "",
    "## 处置建议(**未执行**, 需用户决策)",
    "",
    "| 方案 | 做法 | 代价/风险 |",
    "|---|---|---|",
    "| **A(推荐)** | `health_state.gather` **直接读**当日 `steps.datasource_gate` 的**已落盘结论**(run_daily 已判过), 不再自己重算 | 改动小、且改用**权威结论**; 但 19:10 之前该结论属于上一轮, 需在文本里标明「截至哪一轮」 |",
    "| B | 快照路径也传 `engine_probe`(自行探针) | 每次快照多起一个子进程(每 5 分钟一次), 与 `run_daily` 的账本计数可能**双计**(`unrecorded` 语义要一起处理) |",
    "| C | 保留现状, 但把「19:10 前未生效」显式写成**正常态**而非告警 | 最小改动, 只消噪音; **不解决**关键源缺席 |",
    "",
    "**共同前提**: 先想清「快照要表达的是**当日**门禁结论, 还是**最近一轮**门禁结论」",
    "—— 现在这两者被混在同一个字段里。建议 A, 并在字段里带上「判定所依据的那一轮」。",
])

NEW = [{
    "id": "FINDING-SNAPSHOT-GATE-NEVER-JUDGES-ENGINE",
    "level": "P1",
    "title": ("健康快照的数据源门禁**结构上从不判定关键源** ⇒ `HALT` 不可达; "
              "且交易时段无当日判定(实测 09-24 的 00:03..19:08 全 UNKNOWN); "
              "09-27 出现「快照 allow=True 而落盘结论 allow=False」的方向相反"),
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

with open(FP, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)

print("added:", added, "| total items:", len(items))
