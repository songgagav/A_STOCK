# 每日行情数据自动补全设计

## 目标

在每日收盘后自动发现并补齐缺失交易日的行情数据，优先使用 StockDB
引擎主链，只有在“引擎缺口”和“H5i 存储缺口”同时成立时，才使用已审计的
Baostock 备用回补链。补全必须可重复、可审计、失败可见，并且不得改变选股、
排序、目标权重、DRL、PaperBook、撮合或 broker 行为。

## 非目标与安全边界

- 不修改 StockDB 主源内容，不把备用源写回 StockDB。
- 不自动清除 HALT、不自动资格化 observation day。
- 不改变 `TRADE_BROKER=paper`、`DRL_PLAN_MODE=shadow`、
  `FUSION_WEIGHT_MODE=shadow`、`RANK_BY_FUSION=0`、冻结 selector 权重。
- 不使用新浪现货快照作为自动补全主路径；该接口无法可靠证明数据所属交易日。
- 不在更新守护内运行选股、目标计划、DRL 训练或交易执行。

## 现状与问题

仓库已有：

- `src/engine_bars_sync.py`：StockDB 引擎到 H5i 的主同步链；
- `src/backfill_trigger.py`：A 且 B 判据、官方日历和默认关闭开关；
- `src/baostock_backfill.py`：Baostock 按日回补、全覆盖门禁、质量守卫、
  区间替换和 provenance；
- `src/data_update_daemon.py`：按时间窗口运行的更新守护。

当前问题是旧日线阶段通过已有 `daily_bars` 日期推导缺口，整天没有行时无法
发现；同时 Baostock 回补触发器没有成为独立服务的确定性每日入口。实现后，
日线阶段统一改由 StockDB 主同步结果、H5i 水位和官方交易日历判定，不再保留
这条按已有行猜测缺口的路径。

## 方案与选择

### 方案一：只运行 StockDB→H5i 主链

安全边界最简单，但无法解决 StockDB 未发布而 H5i 同时缺数的情况。

### 方案二：主链优先 + 双缺口时使用 Baostock 备用源（选定）

收盘后先执行 StockDB→H5i 同步，再使用官方交易日历计算缺口。只有以下条件
全部满足时才调用 Baostock：

1. 自动补全开关显式开启；
2. 日历强度为 `official`；
3. StockDB 引擎日早于期望日；
4. H5i 水位早于期望日；
5. 待补日期在回溯窗口内。

Baostock 每个日期必须通过全覆盖、停牌分类、归一化、OHLC/流动性质量守卫后
整日写入；任一日期失败则该日期拒写。写入后必须同时验证 H5i 水位前进和
StockDB 引擎日不变，并追加 provenance。该方案解决当前问题，同时保持主源
与备用源语义可区分。

### 方案三：把回补嵌入 AStockDaemon

进程更少，但数据源重试、网络阻塞或回补异常会和守护、选股、纸面执行耦合，
不采用。

## 设计

### 1. 独立服务入口

保留 `src/data_update_daemon.py` 作为单一更新入口，并新增 Windows 服务托管
脚本 `ops/install_data_update_service.ps1`，服务名为 `AStockDataUpdate`。

服务行为：

- 自动启动，崩溃由 NSSM 重启并受节流保护；
- 每 5 分钟轮询，但日线阶段只在 15:45 后执行；
- 每个交易日和每个补全日期幂等，只在状态未成功时重试；
- 服务只写数据更新回执、H5i 行情库和 provenance，不写 selector、target、
  PaperBook、DRL pointer 或 broker 状态；
- 失败写入 `data/data_update_last.json`，不伪造成功状态。

### 2. 主链与备用链顺序

`run_round()` 的日线阶段改为：

1. 调用 `engine_bars_sync.sync_to_latest(apply=True)`；
2. 重新读取 StockDB 引擎日、H5i 水位和官方日历期望日；
3. 通过 `backfill_trigger.decide()` 计算 A/B 判据和 `missing_days`；
4. A/B 未同时成立时只记录原因，不调用 Baostock；
5. A/B 同时成立且开关开启时，调用 `.venv310` 的
   `baostock_backfill.py --days ... --write`；
6. 解析落盘 JSON 回执并校验每个请求日期为 `written`，再标记该日期成功；
7. 回补失败、覆盖不足、日历不可判定或解释器不可用时，保持失败/阻塞状态，
   供下一轮重试。

`data_update_daemon` 不能再通过 `SELECT DISTINCT daily_bars` 推断官方缺口；
缺口日期必须来自 `backfill_trigger.missing_days()` 的官方日历。

### 3. 自动开关

生产默认配置写入 `data/backfill_switch.json`（gitignored）：

```json
{
  "enabled": true,
  "lookback_days": 10
}
```

环境变量 `BACKFILL_ENABLED` 仍优先于文件；任何非法值、损坏文件或缺失文件都
按关闭处理。启用只授权“满足 A 且 B 后执行已审计 Baostock 回补”，不授权任何
策略或交易行为变化。

### 4. 回执与 provenance

`data/data_update_last.json` 至少记录：

- 主链 probe/sync 结果；
- `engine_day`、`h5i_watermark`、`expected_day`；
- A/B 判据、日历强度、待补交易日；
- 备用源是否执行、解释器、每日期状态、覆盖数和质量守卫；
- `h5i_watermark_after`、`engine_day_after`、`engine_probe_unchanged`；
- 错误原因和下一次重试依据。

Baostock 既有 `data/backfill_provenance.jsonl` 继续作为逐日来源记录；任何
备用源行都必须明确 `source=baostock`、`affects_selection=false`。

### 5. 测试验收

必须先写失败测试，再实现：

- 完整缺失日能从官方日历发现，而不是只依赖已有 `daily_bars` 日期；
- 主链追平时不调用备用源；
- 仅 H5i 缺口时不调用备用源；
- A/B 双缺口且启用时调用备用源；
- 默认关闭、坏开关和弱日历都不写库；
- Baostock 覆盖不足或质量守卫失败时整日拒写；
- 成功回补后 H5i 水位前进且引擎日不变；
- 失败回补下一轮可重试，不把失败日期标成成功；
- 服务入口不导入选股、DRL 或 broker 执行路径；
- 既有回补、数据门禁和纸面安全回归保持通过。

## 验收结论

只有在独立服务、历史 fixture、真实 dry-run、受控单日 write、回执/provenance
和回归测试全部通过后，才把 `AStockDataUpdate` 设为自动启动。即使自动回补
成功，也只表示 H5i 下游数据补齐；StockDB 引擎仍保持独立事实，未发布期间不
得据此宣称选股主源已追平。
