# 数据源 Router 真实 Shadow 验证记录（2026-10-05）

本记录只描述隔离临时目录中的真实网络探针，不代表生产切换已经完成。
所有探针均使用单标的 `600000.SH`、`mode=shadow`，不写生产 `data/`，不写正式
h5i，也不触发 PaperBook 或交易。

## 运行环境

| 项目 | 结果 |
| --- | --- |
| Python | `.venv310` |
| `baostock` | 0.9.4 |
| `mootdx` | 0.11.7 |
| `zzshare` | 0.4.12 |
| `arcticdb` | 未安装；不是当前 Router/h5i shadow 的依赖 |
| 生产 daemon Router | 默认关闭；本次未改变默认开关 |

依赖安装入口已固化为 `requirements-data-sources.txt`。它是可选的 3.10
数据源验证环境，不加入核心 `requirements.txt`，避免核心回归被网络客户端绑定。

## 真实结果

### Baostock

以下 5 个历史交易日均返回 `shadow_staged`，覆盖率为 `1.0`：

`2026-09-15`、`2026-09-16`、`2026-09-17`、`2026-09-18`、`2026-09-22`。

结论：适配器、归一化、质量门和 staging 链路在这组隔离样本上通过。
这不是把 Baostock 自动提升为正式主源；当前元数据仍按 Router 规则受
`source_tier` 和 `execution_allowed=false` 约束。

### ZZShare

同样 5 个交易日均返回 `shadow_staged`，覆盖率为 `1.0`。本次调用未配置
token，匿名接口已返回该单标的历史日线。

结论：ZZShare 的真实适配器在单标的、历史日期的隔离 shadow 样本上可用，
但仍处于 `source_tier=shadow`，不能自动升级为 `backup`。

### mootdx

依赖已安装，客户端可以建立调用，但针对 `600000.SH` 的历史 bar 查询返回
空结果，Router 明确返回 `blocked`，覆盖率 `0.0`，并记录
`coverage_below_expected`，没有伪报成功。此前单日探针也得到相同结果。

结论：mootdx 真实灰度尚未通过。可能需要重新确认通达信服务端、市场参数、
历史 bar 可用范围和接口返回格式；在完成前保持 `shadow`，不得接管正式数据。

追加只读诊断：默认客户端对 `600000`、`000001`、`300750` 在日线
`offset=10/800` 下均返回空 DataFrame；切换到 3 个配置中的备用服务器后均
发生连接超时。未据此修改生产适配器，也没有把超时当成成功。当前最合理的
状态仍是 `blocked`，待通达信服务端网络可用后再复验。

## daemon 入口验证

在临时 root、`DATA_SOURCE_ROUTER_ENABLED=1`、`MODE=shadow`、仅选择
`zzshare` 的条件下，直接调用 daemon 的 Router 入口返回 `shadow_staged`，
覆盖率为 `1.0`。这证明调度接线可被显式启用，但不等于已经打开生产 daemon。

生产启用仍需运维明确设置开关，并先完成连续交易日观察；默认值保持关闭。

## 5 日准入状态

- Baostock：已完成 5 个历史交易日的单标的隔离 shadow 样本；尚未完成线上
  连续运行观察和人工升级。
- ZZShare：已完成 5 个历史交易日的单标的隔离 shadow 样本；尚未完成线上
  连续运行观察和人工升级。
- mootdx：真实查询为空，未达到 5 日准入条件。

因此，本记录不能作为 `shadow -> backup` 的批准凭证。升级仍需人工批准、
连续运行证据、无未解释异常，并保持 `execution_allowed=false` 直到单独的
生产决策完成。

## ArcticDB 兼容层边界

`src/arctic_store.py` 保留为历史/兼容访问层，当前数据源 Router 的主路径是
`staging -> h5i`，不要求安装 ArcticDB。README 和 API 文档继续保留该模块说明，
但 ArcticDB 不应被误写成 Router 的必需依赖。

仍有旧的退化/审计消费者引用 `arctic_store`；这属于独立的兼容层收口任务：
在确定 `perf_report`、`reward_curve`、`trade_records`、`factor_ic` 的替代存储
和口径前，不删除兼容层，也不把缺包解释为“无退化”。
