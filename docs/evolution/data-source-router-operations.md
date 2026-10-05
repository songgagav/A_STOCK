# 数据源 Router 运维说明

本文件记录数据源 Router 的生产接入边界。它解决的是“数据能否进入
staging/h5i”的问题，不改变 09:25 信号快照、PaperBook 或交易闸门。

## 当前状态

- Baostock、mootdx、ZZShare 已实现统一 `fetch(trade_day, symbols)` 适配器。
- 适配器默认懒加载第三方依赖；测试使用注入 transport，不访问真实网络。
- Router 默认关闭；daemon 只有在 `DATA_SOURCE_ROUTER_ENABLED=1` 时才调用。
- Router 默认 `shadow`：抓取、归一化、质量检查并写入 staging，不写 h5i。
- `enforce` 需要同时设置 `DATA_SOURCE_ROUTER_MODE=enforce` 和
  `DATA_SOURCE_ROUTER_CONFIRM=I_UNDERSTAND`，仍只能通过已有 h5i sink/probe，
  不接实盘下单。
- mootdx、ZZShare 在完成至少 5 个交易日的 shadow 验证、人工批准前保持
  `source_tier=shadow`，不能自动升级为 backup。

## 配置

```powershell
$env:DATA_SOURCE_ROUTER_ENABLED = "1"
$env:DATA_SOURCE_ROUTER_MODE = "shadow"
$env:DATA_SOURCE_ROUTER_SOURCES = "baostock,mootdx,zzshare"
$env:DATA_SOURCE_ROUTER_SYMBOLS = "600000.SH,600519.SH"
$env:DATA_SOURCE_ROUTER_STAGING_RETENTION_DAYS = "30"
```

也可以用 `DATA_SOURCE_ROUTER_SYMBOLS_FILE` 指向每行一个标的的文件。生产
默认不从环境变量读取交易权限；每个批次的 `execution_allowed` 仍为 `false`，
交易必须继续经过快照和 PaperBook。

真实网络 smoke 必须显式指定小范围标的、使用 shadow，并将 root 指向临时目录。
不得用真实生产 `data/` 做首次联调。

## daemon 调度

daemon 只在收盘窗口内每天调用一次 Router，并在看护周期内每天调用一次
`cleanup_staging`。Router 关闭时返回 `disabled`，不会导入第三方客户端、访问
网络或修改状态。状态写入 daemon 状态文件的
`last_source_router_day`、`last_staging_cleanup_day` 字段。

生产启用前检查：

1. 先以 shadow 跑单日、小样本，确认 quality、coverage、source_tier 和审计日志。
2. 确认 h5i 运行时可用；缺少 `h5i_db` 必须显示为 blocked/failed，不能当空表成功。
3. 观察期内不修改 signal freeze、PaperBook、实时选股消费路径。
4. 未经人工批准，不切换 `shadow -> backup`，不启用 enforce。

## `occupied_unknown` 人工解除

写入冲突、已有 manifest 不匹配、probe 行数或哈希不匹配时，批次进入
`occupied_unknown`，并写入：

```text
data/h5i/reconcile_reviews/<batch_id>.json
```

自动流程不会覆盖、删除或猜测该批次。人工处理示例：

```powershell
python -m data_sources.reconcile `
  2026-09-30-baostock-20261004T120000Z-a3f2c8e1 `
  --root . `
  --reviewer ops `
  --verdict confirmed_match `
  --comment "h5i probe 行数和 canonical hash 已人工核对"
```

允许的 verdict：

- `confirmed_match`：必须由 probe 同时核对行数和 canonical content hash，成功后
  才写 manifest 并转为 `committed`。
- `rejected`：转为 `failed`，保留审核记录，不自动删除 h5i 数据。
- `unresolved`：保持 `occupied_unknown`，审核记录继续为 open。

## staging 清理

默认保留 30 天，只删除有合法 metadata 且状态为 `committed` 或 `failed` 的旧
`.jsonl` staging payload。以下对象永不因年龄自动删除：

- `staged`、`occupied_unknown`、缺 metadata 的孤儿 payload；
- metadata/JSON 非法的对象；
- 删除时遇到锁或权限错误的对象（下次重试并报告）。

清理失败不影响当日交易或 Router 结果；daemon 会记录结构化结果，人工可根据
`deleted/kept/invalid/orphaned` 计数处理。

## 未完成与明确限制

- 真实线上灰度只能在依赖和数据源可用后由运维显式执行；本分支的自动化测试不
  伪造真实网络通过。
- 真实验证记录见 `docs/evolution/data-source-router-shadow-2026-10-05.md`：
  Baostock 和 ZZShare 完成了隔离历史样本，mootdx 因真实返回空数据保持 blocked。
- `enforce` 不是默认配置，当前不接实盘。
- staging 清理不负责人工解除 `occupied_unknown`，两者必须分开审计。
- `src/arctic_store.py` 是兼容层，不是当前 Router/h5i 路径的必需依赖；旧退化
  消费者的替代存储收口另列任务，未完成前不得把 ArcticDB 缺包当作无退化。

第 5 点以下任务的最新扫描、ArcticDB 消费者盘点和 Phase E/F 边界见
`docs/evolution/post-point5-status-2026-10-05.md`。
