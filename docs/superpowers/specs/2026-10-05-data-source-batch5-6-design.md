# 数据源路由 Batch 5–6 设计

日期：2026-10-05  
状态：实现完成，待合并到 Batch 1–4 Draft PR

## 1. 目标

Batch 5 把 Batch 4 的注入式 `CommitSink` / `ContentProbe` 接到既有 h5i 写入链，
不复制 `h5i_sync.append_daily_bars`，也不修改 `daily_bars` schema。

Batch 6 把旁路 metadata、manifest、staging 和 h5i 运行时状态接入看板，新增
`/api/data-sources`，并在 DB 监控页显示阻断原因和批次概览。

## 2. h5i 兼容边界

现有 `daily_bars` 只有裸六位代码和 OHLCV/成交额字段，没有交易所后缀、
`adj_factor` 或 `batch_id`。因此：

- canonical `600000.SH` 写入前映射为 h5i 的 `600000`；探针读回时按代码规则恢复
  `.SH/.SZ/.BJ`；
- 只接受 `adj_factor == 1.0` 的批次。非单位复权因子无法无损表达时直接返回
  `failed`，不静默丢字段；
- 批次身份和 source provenance 仍由 `data/h5i/source_metadata` 与
  `data/h5i/manifests` 旁路文件保存；
- 默认 sink 委托 `bars_ingest.write_bars(..., dry_run=False)`，因此保留既有质量闸门、
  单调水位和 h5i 主写路径。

## 3. Batch 5 接口

```text
H5ICommitSink.commit(CanonicalBatch) -> CommitResult
H5IContentProbe.probe(trade_day, symbols) -> ProbeResult
```

写入结果：

- `committed`：整批追加行数与预期一致；Batch 4 才能发布 manifest；
- `failed`：运行时缺失、字段不可表达或 writer 明确拒绝；metadata 可转 failed；
- `unknown`：writer 抛异常或行数无法确认；metadata 保持 staged，必须后续对账。

探针用与 staging 相同的 `content_hash` 规范化 canonical 行，避免 float64 往返造成
假差异。生产 h5i 不可用时抛出 `H5IUnavailableError`；看板层捕获并显示阻断。

## 4. Batch 6 状态契约

`read_data_source_status(root)` 返回：

- `status`: `ok | degraded | blocked`；
- `h5i`: `ready | unavailable`、路径和错误；
- `sources`: 按 source、source tier 的批次数；
- `batches`: ingest status 汇总、最近批次、无效 metadata 数；
- `manifests` / `consistency`: manifest 数量、无效 manifest，以及 committed 与旁路
  metadata 不一致的计数；
- `staging.count`: 当前 JSONL staging 文件数。

缺少 `h5i_db` 或数据库路径不可打开时，整体为 `blocked`；旁路对账不一致时为
`degraded`，但 API 仍返回 HTTP 200
和结构化错误，避免看板空白或假绿。该状态只用于运维展示，不授予交易权限，
不改变 signal snapshot、PaperBook 或 Phase E 行为。

## 5. 测试隔离

Batch 5 的 sink writer 和 probe database 均可注入；Batch 6 的 h5i 检查器可注入。
测试只使用 `tmp_path` 和 fake DataFrame/DB，不触碰生产 `data/h5i/market.db`，不调用
真实数据源网络，不修改冻结相关文件。
