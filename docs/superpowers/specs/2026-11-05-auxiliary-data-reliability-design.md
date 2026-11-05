# 辅助数据源可靠性设计

## 目标

为新闻、盘口、龙虎榜、大宗交易及同类已退役数据源恢复独立、可追踪、可重试的采集与落盘能力，同时保持它们为 `research_only`，不改变 selector、Fusion、DRL、目标权重、PaperBook、撮合、broker、HALT 或 Observation Epoch 的策略语义。

## 非目标

- 不复活 `legacy_stockdb.duckdb`。
- 不把辅助源失败升级为交易熔断；只有已存在的策略关键源继续按原门禁判定。
- 不让新闻、盘口、龙虎榜或大宗交易自动进入评分、排名、权重或交易。
- 不承诺第三方接口永远在线；承诺失败可见、可分类、可重试且不会伪造成功。
- 本阶段不把新采集的财务数据写入权威 `h5i.financials`；财务数据直接影响策略，需要单独的 PIT、覆盖率和新 Epoch 验收。

## 现状

旧采集函数仍存在于 `src/update_db.py`，但它们通过已删除的 DuckDB 连接写入，因此抓取逻辑存在而持久化链路不存在。当前明确受影响的研究源包括：

- `stock_news` / `news`
- `orderbook_snapshot`
- `lhb`
- `dzjy_daily` / `block_trade`
- 旧注册表中的 `events`、`adj_factors` 等无替代写入路径条目

其中新闻、龙虎榜和大宗交易可由 AkShare 对应接口获取；盘口为实时快照，只能在交易时段采集且不能事后补造。

## 方案比较

### 方案一：重建旧 DuckDB

复用代码最多，但重新引入已退役单点，并可能让旧查询路径重新进入生产。否决。

### 方案二：直接给 H5i 增表

查询统一，但异构新闻/盘口结构和 H5i 单调时间约束耦合较重，也容易被误认为权威策略数据。暂不采用。

### 方案三：独立内容寻址辅助数据层（采用）

每次采集保存原始数据、规范化记录和 manifest；同样输入得到同样 `run_id`，manifest 为提交标记，已完成目录禁止原地覆盖。每源另有一个可重建的 latest receipt，供守护、看板和告警读取。

## 数据契约

正式构建入口必须显式提供：

- source
- trade_day
- observed_at
- provider
- raw records
- normalized records
- publication state
- output root

构建器不使用 `date.today()`、不寻找“最新文件”，也不调用网络。

状态限定为：

- `available`
- `pending_publication`
- `not_applicable`
- `missing`
- `blocked`
- `invalid`
- `tampered`

每个 manifest 至少包含 schema、run/source/day/time、provider、source tier、authoritative/strategy flags、coverage、record counts、raw/normalized hashes、builder version、status 和 reasons。

目录布局：

```text
data/auxiliary/
  bundles/<source>/<trade_day>/<run_id>/
    raw.json
    records.jsonl
    manifest.json
  receipts/<source>.json
```

`manifest.json` 最后写入；同名 finalized 目录存在时先校验内容，任何不一致均报告 `tampered`，不得覆盖。

## 来源和节奏

| source | provider | cadence | coverage | production authority |
|---|---|---|---|---|
| stock_news | AkShare/Eastmoney | 盘后一次，失败重试 | `全部A股` 关键词查询前 10 条，非全市场新闻流 | research_only |
| lhb | AkShare/Eastmoney | 交易日盘后，允许 T+1 补齐 | 当日上榜股票 | research_only |
| block_trade | AkShare/Eastmoney | 交易日盘后，允许 T+1 补齐 | 当日大宗逐笔 | research_only |
| orderbook_snapshot | mootdx/TDX | 交易时段固定快照 | 明确传入的观察股票集合 | research_only |

盘口 receipt 必须记录请求股票数、成功数和失败数；不能把现货价或收盘价伪装为盘口。错过采集窗口时为 `not_applicable` 或 `missing`，不能历史补造。

实时盘口入口同时校验实际时钟、`observed_at` 和 `trade_day`：三者必须是同一上海交易日，且实际时钟与观测时间都在 14:50–15:00，偏差不超过 60 秒。覆盖率必须按请求代码与返回代码的交集计算，并要求同时存在有效买一/卖一深度；额外代码不得顶替缺失的目标代码。

## 调度与降级

辅助阶段由 `AStockDataUpdate` 独立运行；每源成功日幂等，失败按既有节流重试。守护只写辅助目录和自己的 receipt，不调用 selector、target、PaperBook、DRL 或 broker。

所有网络采集在独立子进程中运行，存在 120 秒硬超时，防止新闻等第三方请求阻塞整个日线守护。龙虎榜和大宗交易失败日进入明确的 T+1 backlog，次日优先回放缺日，再采集当日。`--once` 不得绕过 18:00 发布窗口。

latest receipt 使用跨进程锁并且只允许 `observed_at` 单调前进；守护状态使用同目录临时文件、`fsync` 和原子替换。重叠轮次在可配置等待后返回 `already_running/retryable`，不相互覆盖状态。

辅助源失败只生成明确的非关键降级：

```text
affects_strategy = false
authoritative = false
observation_impact = non_critical_degradation
```

只有当未来另行批准某源进入策略时，才同时修改 source tier、策略门禁、Epoch 身份和回归测试。

## 验收

- 测试先失败后实现。
- 相同输入得到相同 run id；finalized 数据不可覆盖。
- 任一 constituent 改动后验证为 tampered。
- pending publication 不记为 failed。
- raw 和 normalized 同时保存并可重算。
- 空结果不统一填 0；按源和发布时间分类。
- 所有显式列出的研究源均为 `authoritative=false`、`affects_strategy=false`。
- 网络抓取与纯 builder 分离；builder 测试不依赖网络。
- 现有 selection/target/position/trading/HALT 文件在 one-shot 前后哈希不变。
- 既有 core/security 回归保持通过。


