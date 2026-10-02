# 09:25 信号冻结与迟到信号治理设计

状态：已批准，待实施计划。  
日期：2026-10-03  
范围：shadow 与 `PaperBook` 虚拟盘；不接券商、不接实盘。

## 目标与不变量

在上海时区每个交易日 09:25:00 固化当日唯一权威的目标信号快照。此后自动决策只能消费经验证的快照，不能因新文件、午间重选或数据源延迟而改变当日目标池。

- 09:25 前的候选仅用于准备、日志和监控，不是权威决策输入。
- 快照不存在、不可解析或校验失败时，系统只估值、不调仓，并留下可审计告警。
- 所有自动交易仍限 `TRADE_BROKER == "paper"` 的 `PaperBook` 虚拟撮合；非 paper broker 一律拒绝。
- 迟到信号不丢弃，但没有完整人工批准时绝不进入目标池或触发交易。

## 模块边界

新增小型模块 `src/signal_snapshot.py`，只负责快照构造、规范化哈希、原子读写、验证、迟到归档、审核记录和冻结文件清理。

`realtime_engine.py` 是唯一决策消费者和冻结触发者：09:25 前准备候选，09:25 时重新解析、加权、写入快照，之后只读取快照。它不再把候选目标写入 `live_state.json` 或 `state.json` 的权威目标字段。

`signal_freeze_watch.py` 保留为哈希链告警账本。冻结、迟到、归档/清理失败、审核结论和 shadow 差异都记录到该账本。

`paper_book.py` 不决定目标池。其 `PriceFeed` 的报价 watchlist 在冻结后只使用“已验证当日快照 targets + `PaperBook.positions` 的当前副本”，不再扫描当日候选 `selection.json`。持仓副本是报价/估值输入而非策略决策输入。

## 快照位置与 schema

路径：`data/daily/<YYYYMMDD>/signal_snapshot_<YYYYMMDD>.json`。

`schema_version` 固定为 `1`。关键字段如下：

```json
{
  "schema_version": 1,
  "date": "20261003",
  "generated_at": "2026-10-03T09:25:00+08:00",
  "generated_at_utc": "2026-10-03T01:25:00Z",
  "targets": [],
  "weights": {},
  "source_tier": "drl_same_day",
  "source_date": "20261002",
  "source_artifacts": [
    {"path": "data/drl/20261002/target_plan.json", "sha256": "..."}
  ],
  "input_hash": "...",
  "snapshot_hash": "..."
}
```

时间使用 `zoneinfo.ZoneInfo("Asia/Shanghai")`，并同时写 UTC 时间。`source_artifacts` 仅列实际消费的、相对仓库根的输入文件，使用 SHA-256。

所有规范化 JSON 使用 UTF-8 及：

```python
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

`input_hash` 是实际决策输入（目标、权重、来源和输入文件摘要）的 SHA-256。`snapshot_hash` 对完整快照计算，但计算时保留 `snapshot_hash` 字段并将其值设为 `null`，以消除自引用循环。它能发现损坏或未同步修改，不宣称防御能同时改写内容和哈希的主动攻击者。

## 冻结状态与失败分级

09:25 前，候选可用于引擎内存准备和监控；`live_state.json` 只发布 `snapshot_status=pending` 等状态，不复制候选 targets；`state.json.top_targets` 不把候选作为权威输出。

09:25 时重新解析并写入快照。写入使用既有 `utils.atomic_write_json`。目录会先创建；写入、读取和验证成功后才标记 `snapshot_status=ready`，并在 `live_state.json` 写 `snapshot_ref` 与 `snapshot_hash`。

| 级别 | 条件 | 自动行为 |
| --- | --- | --- |
| L1 | 文件不存在 | 仅估值，不调仓，高优先级告警 |
| L2 | JSON、schema、字段或版本无效 | 仅估值，不调仓，异常告警 |
| L3 | `input_hash` 或 `snapshot_hash` 不匹配 | 拒绝虚拟盘交易，最高优先级篡改疑点告警 |

L1/L2/L3 当日不可自动恢复：事后补写不代表 09:25 的输入。紧急调整只能走独立人工干预流程。任何失败都不会触发清理。

午间重选及所有 09:25 后结果都不得替换 `self.targets`。快照不存在时也按理论 09:25 截止判断迟到，而不以实际写入成功时间延后截止。

## 迟到信号与人工审核

迟到窗口是 09:25:00（不含）至 15:00:00（含）。窗口内的结果写入：

`data/daily/<YYYYMMDD>/late_signals_<YYYYMMDD>.json`

每条记录包括候选 ID、到达时间、来源、延迟原因（未知为 `unknown`）、完整可复算载荷、载荷哈希、`late_for_consume_day` 和当时的 `snapshot_status`。超出窗口的结果不进入审核队列，但照样写入账本并标为 `out_of_window`。

审核文件为：

`data/daily/<YYYYMMDD>/late_review_<YYYYMMDD>.json`

审核项必须记录 `reviewer`、`reviewed_at`、`candidate_id`、`payload_hash`、`verdict`（`approved` 或 `rejected`）和 `comment`，并写入信号冻结账本。

仅当审核批准整个候选池、全部权重和原始载荷哈希均匹配时，该候选才可作为下一交易日 09:25 的可选输入。任何部分批准均等同未批准，不能自动合并；紧急局部调整走人工干预通道。无审核记录或被拒绝的候选不影响后续自动交易。

## 报价、清理与并发

冻结后的报价 watchlist = 通过验证的当日快照 targets ∪ 当前 `PaperBook.positions` 副本。持仓在进程启动时由 `state.json` 恢复，但运行时以 `PaperBook.positions` 为准；读取时复制，避免与撮合更新争用。

快照、迟到和审核文件使用原子写入。引擎是冻结账本的正常写入者；相关帮助函数还要用进程内串行保护，避免未来守护任务并发导致丢失更新。

仅在当日快照成功写入且验证通过后清理。按照交易日历保留 90 个交易日，删除第 91 个交易日及更早的冻结相关文件（快照、迟到、审核文件），不删除日目录或其他日数据。日历不可用、清理失败或快照失败均跳过删除并告警。

## 模式与上线

运行控制文件：`config/signal_freeze_mode.json`。

```json
{
  "mode": "shadow",
  "promoted_at": "2026-10-10T15:00:00+08:00",
  "promoted_by": "宋嘎嘎V",
  "evidence": "docs/evolution/signal-freeze-promotion.md"
}
```

默认 `shadow`：生成、验证快照并记录快照与后续候选的差异，但不切换既有消费路径。每个差异必须标记为 `expected_timing`、`data_refresh`、`source_disagreement` 或 `unexplained`；未分类即为 `unexplained`。

连续 5 个交易日没有任何 `unexplained` 差异后，人工写入具名提升记录才可切到 `enforce`。`enforce` 只影响虚拟盘：引擎强制消费快照、停用午间目标替换；不接实盘。

## 验收与实现顺序

测试拆分为：

- `tests/test_signal_snapshot.py`：schema、哈希稳定性、09:25 后只读、L1/L2/L3。
- `tests/test_signal_late_signals.py`：迟到归档、窗口、审核、账本。
- `tests/test_signal_cleanup.py`：90 日清理与失败不清理。
- `tests/test_signal_watchlist.py`：快照 targets 与实际持仓的并集。

Phase A：快照模块、hash、schema。  
Phase B：消费切换、L1/L2/L3、watchlist。  
Phase C：迟到归档与账本。  
Phase D：审核与 90 日清理。  
Phase E：shadow 运行观察，连续 5 个交易日验收。  
Phase F：具名人工提升至 enforce，仍只接虚拟盘。

每个 A-D 阶段独立提交、定向测试和全量回归。Phase E 是运行期验收，不把运行观察伪造为自动化通过。
