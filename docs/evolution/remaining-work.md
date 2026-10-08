# 剩余任务总清单

更新时间：2026-10-09  
用途：记录当前仓库中尚未闭环的工程任务、外部阻塞和策略决策；本文件是任务索引，不把 `SKIP`、空结果或文档存在误读为已完成。

## 结论先行

- **h5i 是当前主数据源**：行情、财务、估值和主要看板读取使用 h5i 及其物化视图。
- **ArcticDB 保留兼容层**：`src/arctic_store.py` 暂不删除，但 ArcticDB 不是常规运行必需依赖。
- **ArcticDB 当前不可用**：两个常用解释器均未安装 `arcticdb`；相关历史退化分析必须显示 `unavailable`，不能显示为健康或空指标。
- **LLM 盘后点评按用户决定暂缓**：其超时问题不在本清单中优先处理。
- **PaperBook 仍是唯一执行边界**：数据源切换、h5i 写入和看板改造都不得绕过快照、PaperBook 和交易闸门。

## A. 本轮已处理

| 任务 | 状态 | 证据/说明 |
| --- | --- | --- |
| README 标注 h5i 主源与 ArcticDB 可选兼容层 | 已完成 | README 架构树、配置说明和模块说明已更新 |
| 缺 ArcticDB 的退化检测显式返回不可用 | 已完成 | `degradation.run_full_check()` 返回 `status=unavailable`，不生成空的健康分 |
| ArcticDB 兼容层保留任务登记 | 已完成 | 本文件及 `docs/vulnerability-register.md` 的现有 P2 记录互相引用 |

## B. ArcticDB 兼容层迁移（P1，需独立变更批次）

### B1. 当前边界

`arctic_store.py` 仍被以下链路使用：

- `degradation.py`：`perf_report`、`reward_curve`、`trade_records`、`factor_ic`；
- `agent_orchestrator.py`、`agent_tools.py`：调用退化检查；
- `dashboard.py`：退化面板和 SPC 历史读取；
- `performance_report.py`、`incremental_learn.py`、部分 DRL 路径：可选写入。

### B2. 后续顺序

1. 盘点四类历史数据的实际落盘覆盖率和最近日期；
2. 为每类数据选择目标后端（优先已有日级 JSON/Parquet/h5i，不能凭空假设 h5i 已包含这些字段）；
3. 写适配器和双源等价测试，先只读，不删除 ArcticDB 代码；
4. 将退化检测消费方切到适配器，并在来源缺失时返回 `unavailable`；
5. 连续回归并核对 dashboard、Agent、增量学习三类消费者；
6. 仅在调用图为零且历史数据迁移有证据后，才删除 `arctic_store.py`、`ARCTIC_URI` 和依赖说明。

### B3. 验收条件

- 无 ArcticDB 时：所有调用方明确显示 `unavailable`/`UNKNOWN`，不得产生 `OK` 或空健康分；
- 有迁移后端时：四类历史数据均有字段、日期范围和来源版本证明；
- dashboard 与 Agent 的输出不再直接依赖 `get_store()`；
- `arctic_store.py` 的全仓生产调用为零，测试/迁移工具除外并有说明；
- `.venv310`、`.venv314` 和 CI 均有定向回归证据。

## C. 数据源路由 V2

| 批次 | 状态 | 下一动作 |
| --- | --- | --- |
| Batch 1：metadata schema | 已完成 | 保持 `source_tier` 枚举和 batch_id 契约 |
| Batch 2：质量阈值与切换判定 | 已完成 | 补充边界回归时不得改冻结链 |
| Batch 3：adapter/normalize/quality | 已完成 | 真实联网源验证单独记录，不伪报 fake transport 为生产验证 |
| Batch 4：staging/manifest/孤儿对账 | 已完成 | 真实 h5i 写入前保持 fake sink/probe 隔离 |
| Batch 5：真实 sink/probe | 已实现，待独立 PR/合并验收 | `feature/data-source-router-v2` 已包含真实 h5i sink/probe；先在隔离库验证，不能把分支存在误读为已进入当前生产分支 |
| Batch 6：看板与审计状态 | 已实现，待独立 PR/合并验收 | 同分支已提供批次、来源健康度和 `occupied_unknown` 展示；不得接入交易触发 |

Batch 5/6 不得修改 `signal_snapshot.py`、冻结消费逻辑、PaperBook watchlist 和 `signal_freeze_watch.py`。

验证记录：`feature/data-source-router-v2` 定向数据源/看板测试为 **103 passed**；当前工作分支尚未合并该分支，合并前仍需 CI、隔离 h5i 库和生产路径审查。

## D. Phase E 信号冻结观察

| 项目 | 状态 |
| --- | --- |
| 默认 shadow、PaperBook、非实盘 | 已确认 |
| stockdb 数据新鲜度和 daemon runtime | 已修复并验证 |
| 2026-10-08 起连续 5 个合格交易日观察 | 待运行 |
| 每日 promotion 记录、差异分类和 unexplained 归零规则 | 已准备 |
| enforce 切换 | 禁止提前执行，需 5 日证据 + 人工批准 |

### D1. 2026-10-09 运行准备

- `E:\A_stockDB\数据更新.exe` 已将行情端点追平至 `20261008`；h5i 已显式追加 5,517 行，freshness 为 `lag_trading_days=0`。
- `.venv310` 盘前前置检查通过；守护进程已运行并等待今日 08:30 启动虚拟盘引擎，`TRADE_BROKER=paper`。
- 09:25 快照的无时区时钟缺陷已修复并有回归测试；当前只能称为“开盘准备就绪”，必须等今日 08:30/09:25 新鲜产物验证后再计入 Phase E。
- 2026-10-08 的 live_state 曾记录 `snapshot_write_failed:ValueError`、`spot empty` 和空仓；这些历史事实保留为 blocked/warn 证据，不补写为成功。

观察日必须同时满足：交易日、数据新鲜、daemon 健康、快照 `ready`、当天记录已落盘。LLM 点评不可用不应被伪装成可用，但按当前决定不阻塞冻结观察。

## E. 不能在当前环境闭环的事项

| 项目 | 状态 | 重新打开条件 |
| --- | --- | --- |
| P0-2 的 2026-09-02 原始台账 | 已知缺口 | 恢复原始台账或可信备份 |
| 真实券商成交对账 | 已知缺口 | 接入脱敏成交回报 |
| DRL 完整真实训练 | 环境/算力阻断 | 固定解释器、数据版本、随机种子和产物 |
| P0-3 降仓/清仓策略 | 待策略决策 | 用户确认动作和阈值 |
| CVaR 熔断参数 | 待策略决策 | 确认窗口、置信度、动作、恢复条件 |
| 09:25 enforce 切换 | 待观察证据 | 完成 Phase E 5 日观察并人工批准 |

## F. 低优先级维护项

- Bandit 剩余低置信度 B608：按批次审计，只记录证据，不批量加 `# nosec`；
- Phase 12 配置：外部引用确认后逐项删除，每项独立回归；
- `pb_rev`、`roe`、`mf_net` 历史 IC 刷新：当前保持显式 `WARN`，完成数据输入和无前视测试后再升级；
- `detect-secrets`、`pip-audit`：当前环境/网络阻断，保持“未运行”记录；
- Dashboard SSE、前后端彻底拆分、增量 DOM：按看板计划独立变更，不与数据源路由混合。

## 工作纪律

1. `SKIP` 只表示该检查未执行或组件按约定退役，不等于通过。
2. 空 DataFrame、空解释点和缺少台账不等于“无风险”。
3. 真实数据源、真实 h5i 写入和 Phase E 观察必须使用隔离验证与可追溯版本。
4. 每项功能变更遵循：测试先行 → 定向验证 → 双环境/CI 回归 → 独立提交。
