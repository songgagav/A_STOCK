# 第 5 点以下任务状态（2026-10-05）

本文记录“第 5 点以下”任务的当前可验证状态。它区分已完成的审计/文档工作、
可以继续推进的工程工作，以及必须等待真实运行条件或策略决策的事项。

## 已完成并有证据的工作

### 安全扫描

扫描解释器：
`A_stock_rotation/.venv314/Scripts/python.exe`。

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `pip-audit -r requirements.txt --format columns` | 通过 | 返回 `No known vulnerabilities found`，退出码 0。 |
| `detect-secrets scan --force-use-all-plugins .` | 通过 | 默认仓库扫描结果为空。 |
| `detect-secrets scan --all-files --force-use-all-plugins` | 不作为通过依据 | 会扫描运行时数据、缓存和日志，出现高熵伪阳性；需分区扫描时再使用。 |
| `bandit -r src -ll -f txt` | 未通过，但无 high | 0 high、125 medium、346 low；不能将“无 high”表述为扫描全通过。 |

Bandit 当前的 125 个 medium 命中全部属于 `B608`（SQL 字符串构造），其中
32 个为 medium-confidence、93 个为 low-confidence；没有 high 命中。本轮不批量
添加 `# nosec`，而是继续按 SQL 输入边界逐项审计，确认参数化、固定白名单或
仅内部生成值后再分别记录例外。

### ArcticDB 兼容层盘点

`.venv310` 和 `.venv314` 均未安装 `arcticdb`。这不等于可以删除
`src/arctic_store.py`：它仍被以下 11 个下游模块直接消费：

- `dashboard.py`
- `degradation.py`
- `drl_train.py`
- `dump_trades.py`
- `incremental_learn.py`
- `performance_report.py`
- `premarket_healthcheck.py`
- `run_daily.py`
- `vnpy_backtest.py`
- `vnpy_backtest_universe.py`
- `vnpy_full_pull.py`

当前兼容层声明的库包括 `bars`、`trade_records`、`daily_summary`、
`perf_report`、`factor_ic`、`reward_curve`。其中 `perf_report`、
`trade_records`、`factor_ic` 和 `reward_curve` 尚未完成统一的文件/h5i 替代
口径。因此本轮结论是：

- 保留兼容层及其失败可见性；
- 不把缺少 ArcticDB 伪装成空结果或“无退化”；
- 不删除旧调用方；
- 迁移必须先为每个库定义替代 schema、读写契约和回读等价测试。

本轮已先完成 `daily_summary` 的只读文件适配器 `src/file_history_store.py` 和
双环境 3 项契约测试。它目前不接入任何生产消费者，属于可回滚的 M1 基础，不代表
ArcticDB 已迁移或可以卸载。

随后补充了同一适配器的 `trade_records` 与 `factor_ic` 只读视图，并覆盖日期排序、
最近 N 日、标的过滤及因子名路径穿越防护；这些视图同样尚未切换生产消费者。

本轮继续补齐了 `perf_report` 按日 JSON 和 `reward_curve` JSONL 的 dormant 读取契约，
并增加双读 DataFrame 比较器；根目录最新绩效快照、`train_meta.json` 和曲线 PNG 均未被
误当成历史序列，生产消费者仍保持原 ArcticDB 路径。

### 数据源 Router

Baostock、ZZShare 的隔离 shadow 样本已完成；mootdx 因真实 bars 为空且备用服务器
连接超时，保持 `coverage_below_expected` 阻断。Router 默认仍为 shadow，未接入实盘，
详见 [data-source-router-shadow-2026-10-05.md](data-source-router-shadow-2026-10-05.md)。

## 仍未闭环的任务

### ArcticDB 迁移

这是待设计和分阶段实现的工程任务，不是依赖安装问题。正确顺序为：

1. 为六类库分别确定文件或 h5i 的权威替代存储；
2. 为旧 API 建立替代读取接口和 schema 版本；
3. 用固定夹具完成读写、排序、时间范围和空数据语义的等价测试；
4. 先双读 shadow，再逐模块切换；
5. 观察无差异后，才允许移除 ArcticDB 依赖和兼容层。

在第 1 步未完成前，不执行全仓替换，也不将异常捕获改成返回空表。

### Phase E / Phase F

Phase E 需要连续 5 个真实交易日、daemon 实际运行、快照为 `ready`、差异均有分类，
并写入 `signal-freeze-promotion.md`。当前文档仍为空模板，不能计为完成。

Phase F 只能在上述证据齐全并经过人工批准后启用，而且继续限制为
`TRADE_BROKER=paper`；本轮没有启用 `enforce`，没有接入实盘。

### 外部条件或策略决策阻断

- mootdx 真实上游无有效 bars，需上游可用或人工选择新的已验证备源；
- 真实券商成交对账当前没有账户，无法闭环；
- 09-02 台账缺失、fidelity_compare 无解释点，无法从代码恢复；
- P0-3、09:25 冻结细节和 CVaR 参数涉及风险偏好，未获明确策略决策前不接线；
- DRL 真实训练依赖完整 h5i/训练环境，当前不能伪造通过；
- LLM 盘后点评按用户要求暂不处理。

## 后续工程顺序

1. 先为 ArcticDB 六类库完成替代 schema 设计和消费者映射表；
2. 为一个低风险库（优先 `daily_summary`）做双读等价测试，不改默认生产路径；
3. 完成 5 个真实交易日的 Phase E 记录；
4. 分批审计 Bandit medium/low 命中并将 SQL 边界纳入测试；
5. mootdx 上游恢复后重新做隔离 shadow，不直接升为 backup；
6. 所有阶段通过后，再分别提交迁移和 Phase F 的独立 PR。

本文件不构成“迁移完成”或“Phase E 通过”的声明；它是当前状态和证据边界的审计记录。
