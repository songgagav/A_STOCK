# ArcticDB 兼容层迁移计划

状态：替代 schema 与 dormant 只读适配器已实现第一版，但尚未切换任何生产消费者。

`src/arctic_store.py` 目前是历史/兼容访问层，不是数据源 Router 或 h5i 主路径的
依赖。两个本地运行环境均未安装 `arcticdb`，但仍有多个旧消费者直接调用它，
因此不能用“删除 import”或“缺包返回空表”冒充迁移完成。

## 当前库与候选替代物

| 兼容库 | 当前写入 API | 候选权威来源 | 当前结论 |
| --- | --- | --- | --- |
| `bars` | `write_bars/read_bars` | `data/h5i/market.db` 的 `daily_bars` | 已有主数据源，但 vnpy 读取契约仍需等价测试。 |
| `trade_records` | `append_trade/read_trades` | `data/daily/<YYYYMMDD>/trades.json` | 文件产物存在，需统一字段、排序和跨日读取。 |
| `daily_summary` | `write_daily_summary/read_daily_summaries` | `data/daily/<YYYYMMDD>/daily_summary.json` | 可作为第一批双读迁移对象。 |
| `perf_report` | `write_perf_report/read_perf_reports` | `data/performance_report.json` + 每日回执 | 当前不是完整历史序列，不能直接替换。需先定义按日归档。 |
| `factor_ic` | `append_factor_ic/read_factor_ic` | `data/ic/ic_curve_<factor>_k20.csv` 等 | 文件已存在，但列名、窗口和版本字段需固定。 |
| `reward_curve` | `append_reward_curve/read_reward_curve` | `data/drl/<YYYYMMDD>/train_meta.json` | 尚无完整逐步 reward 的权威历史文件，需新增落盘契约。 |

## 迁移不变量

1. 迁移前后相同查询的日期范围、排序、空结果和缺字段语义必须一致。
2. 任何无法判定的数据都返回明确的 `unavailable/unknown` 状态，不能返回空表伪装
   成“没有数据”。
3. 旧 ArcticDB 读路径在替代源通过等价测试前保持不变。
4. 写路径先双写或旁路验证，不能直接停止旧写入；任何写入失败都必须留痕。
5. Router、09:25 快照、PaperBook 和真实/虚拟交易闸门不依赖本迁移的中间状态。

## 替代 schema v1

以下契约只约束文件适配器和双读比较，不代表历史数据已经补齐：

| 数据集 | 权威文件契约 | 主键/排序 | 空结果语义 |
| --- | --- | --- | --- |
| `bars` | `data/h5i/market.db`，由 h5i 适配层读取 | `symbol + trade_day` | 数据源不可用时为 `unavailable`，不能伪造空表 |
| `trade_records` | `data/daily/<YYYYMMDD>/trades.json` | `day,time,ts` 升序 | 无文件返回显式空 schema |
| `daily_summary` | `data/daily/<YYYYMMDD>/daily_summary.json` | `day` 升序、最近 N 日 | 无文件返回显式空 schema |
| `perf_report` | `data/daily/<YYYYMMDD>/performance_report.json` | `day` 升序、最近 N 日 | 根目录最新快照不视为历史 |
| `factor_ic` | `data/ic/ic_curve_<factor>_k20.csv` | `day` 升序、最近 N 日 | 因子文件缺失返回显式空 schema |
| `reward_curve` | `data/drl/<YYYYMMDD>/reward_curve.jsonl` | `day,step` 升序 | `train_meta.json`/PNG 不视为逐步曲线 |

公共约定：日期规范化为 `YYYYMMDD`（成交记录的展示列为
`YYYY-MM-DD`），JSON 使用 UTF-8，读取器只读不补推导字段；生产读取失败必须由上层
区分 `unavailable` 与“确实没有行”。

## 分阶段执行

### M0：契约与夹具

- 为六个库定义 schema version、主键、时间字段、排序规则和空值策略；
- 建立固定小夹具，不读取生产 `data/`；
- 写 `read_range/read_all/list_symbols` 的行为对照测试；
- 明确每个消费者允许的 `unavailable` 行为。

### M1：先迁移 `daily_summary`

- 已新增 `src/file_history_store.py`，通过注入的 `data/daily` 根目录读取按日目录；
- 已新增双环境契约测试，验证排序、最近 N 日限制、空 schema 和 JSON 保留；
- 当前适配器仍是 dormant/side-by-side 组件，不改变 ArcticStore 或生产读写路径；
- 与 ArcticStore 读取结果做双读对比；
- 连续通过固定夹具和历史样本对比后，先切 dashboard/报表只读路径；
- 保留旧写入和回滚开关。

### M2：迁移 `trade_records` 与 `factor_ic`

- 已在 dormant 文件适配器中加入只读 `trades.json` 归一化视图和
  `ic_curve_<factor>_k20.csv` 读取视图；
- 已加入标的过滤、最近 N 日限制、日期排序和因子名路径穿越防护测试；
- 当前仍未接入退化检测、dashboard 或 run_daily；
- 下一步是与 ArcticDB 做字段/排序/空结果双读对比，再逐个切换消费者；
- 在退化计算和诊断工具中逐个切换，禁止批量替换。

### M3：补齐 `perf_report` 与 `reward_curve`

- 已定义按日不可变归档和 JSONL 逐步曲线 schema；
- dormant 适配器已读取 `performance_report.json` 和 `reward_curve.jsonl`，并明确
  不把根目录最新快照、`train_meta.json` 或 PNG 当成历史；
- 已加入 fake round-trip/空结果契约测试；
- 仍未补写生产产物、未切换真实训练或退化消费者。

### 双读比较基础

`src/history_equivalence.py` 提供无副作用的 `compare_history_frames()`：
`None` 被报告为 `unavailable`，不会与空 DataFrame 混淆；字段缺失、行数差异、排序后
内容差异均返回 `mismatch`。它目前只用于 shadow/测试，不自动替换任何 ArcticDB 读取。

### M4：双读观察与退役

- 双读差异连续观察；
- 每个消费者单独提交、单独回归；
- 完成迁移清单后再删除 ArcticDB 写入和依赖；
- 兼容层删除必须作为最后一个独立 PR，附完整回滚点。

## 当前禁止事项

- 不删除 `src/arctic_store.py`；
- 不把 `ModuleNotFoundError` 转换成空 `DataFrame`；
- 不把 ArcticDB 缺失解释为“无退化”；
- 不在 Phase E 观察期间改冻结、PaperBook 或交易消费路径；
- 不使用生产数据目录作为迁移测试写入目录。

## 消费者清单

当前直接消费兼容层的模块见：
`dashboard.py`、`degradation.py`、`drl_train.py`、`dump_trades.py`、
`incremental_learn.py`、`performance_report.py`、`premarket_healthcheck.py`、
`run_daily.py`、`vnpy_backtest.py`、`vnpy_backtest_universe.py`、
`vnpy_full_pull.py`。

迁移完成标准：每个模块有替代来源、等价测试、失败状态定义和独立回滚提交。
