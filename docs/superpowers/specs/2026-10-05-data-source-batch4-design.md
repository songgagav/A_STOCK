# Batch 4: staging、h5i 提交对账与孤儿批次设计

日期：2026-10-05
状态：实现前设计，待用户审阅
范围：`feature/data-source-router-v2`

## 1. 目标

为 Batch 1–3 的数据源元数据、路由、adapter 和 canonical batch 增加一个可审计的
staging/提交边界，解决以下问题：

- canonical 数据写入 h5i 前有完整、可重试的 staged 副本；
- h5i 写入成功后才发布 committed manifest；
- 进程在 h5i 写入和 manifest 发布之间退出时，可以识别并对账孤儿 staged；
- 同一交易日不允许多个成功批次竞争或覆盖；
- 测试完全使用 fake sink/probe 和 `tmp_path`，不触碰生产 h5i 或 Phase E 文件。

## 2. 已确认的边界

### 2.1 不改 h5i 表结构

h5i `daily_bars` 没有 `source` 或 `batch_id` 列。本批不新增 h5i 表，也不修改
`daily_bars` schema。批次信息全部保存在旁路文件中。

现有 `bars_ingest.write_bars()` 和 `h5i_sync.append_daily_bars()` 是既有写入边界；
Batch 4 只定义其上层的批次协调契约，不复制第二套 h5i 写入逻辑。

### 2.2 同一交易日只有一个成功批次

同一 `trade_day` 的第二个批次一律拒绝，包括不同 source 或 source_tier。
数据修正不通过 source router 覆盖，应走独立修正流程。

如果 h5i 已有该日数据，但旁路没有可验证的 committed manifest，结果为
`occupied_unknown`：拒绝新批次，保留人工核对入口，Batch 4 不自动覆盖或删除。

### 2.3 Phase E 隔离

Batch 4 不修改以下文件或行为：

- `signal_snapshot.py`
- `realtime_engine.py`
- `paper_book.py`
- `signal_freeze_watch.py`
- `h5i_bar_store.py`
- `bars_ingest.py` 的既有行为
- `h5i_sync.py` 的既有行为

真实 h5i sink/probe 推迟到 Batch 5。Batch 4 只提供可注入接口和 fake 实现。

## 3. 文件布局

根目录由调用方注入；测试使用 `tmp_path`，生产默认布局为：

```text
data/staging/bars/<batch_id>.jsonl
data/h5i/source_metadata/<batch_id>.json
data/h5i/manifests/<batch_id>.json
```

### 3.1 staging 文件

每行一个 canonical record。写入采用同目录临时文件 + `os.replace`，只有完整文件
替换成功后才认为 staging 就绪。staging 文件内容不包含 metadata。

### 3.2 source metadata

复用 Batch 1 的严格十字段 metadata schema，`ingest_status` 初始为 `staged`。
metadata 只有在 staging 原子写成功后才创建。

### 3.3 manifest

manifest 自包含，不依赖读取 metadata 才能解释：

```json
{
  "batch_id": "2026-09-30-baostock-20261004T120000Z-a3f2c8e1",
  "trade_day": "2026-09-30",
  "source": "baostock",
  "source_tier": "backup",
  "row_count": 5000,
  "content_hash": "<sha256>",
  "committed_at": "2026-10-04T12:00:00Z"
}
```

manifest 仅在 sink 返回 `committed` 后原子写入；写 manifest 失败时 metadata 保持
`staged`，供后续 reconcile。

## 4. content_hash 规范

staging 和 probe 必须调用同一个 `canonical_serialize()`，禁止各自拼接字符串。

### 4.1 字段集

每行只纳入以下九个 canonical 字段：

```text
symbol, trade_day, open, high, low, close,
volume, amount, adj_factor
```

不纳入 source、retrieved_at、metadata 或 manifest 字段。

### 4.2 排序和数值

- 行按 `(trade_day, symbol)` 升序排序；
- 字段名按稳定顺序序列化；
- `volume` 规范化为十进制整数；
- `open/high/low/close/amount/adj_factor` 先转换为有限数字，再用
  `Decimal(str(value))` 量化到小数点后 10 位，输出固定十进制字符串；
- 禁止 NaN、Infinity 和负零；
- JSON 使用 UTF-8、紧凑分隔符、稳定键序；
- 对序列化字节计算 SHA-256。

这样可以消除 h5i 的 float64 往返表示差异，同时保留 A 股价格和成交额所需精度。

### 4.3 probe 的判定

`exists` 只表示该交易日有数据，不能独立证明批次已提交。孤儿 staged 对账必须同时
满足：

```text
probe.exists == true
probe.row_count == staged_row_count
probe.content_hash == staged_content_hash
```

任一不满足，都不能转为 committed；已有数据但无法匹配时返回 `occupied_unknown`。

## 5. 接口契约

### 5.1 CommitSink

```python
class CommitSink(Protocol):
    def commit(self, batch: CanonicalBatch) -> CommitResult: ...
```

`CommitResult`：

```text
status: committed | failed | unknown
row_count: int
error: str | None
```

- `committed`：确认整批写入成功；允许发布 manifest；
- `failed`：确认没有需要对账的写入；metadata 转为 `failed`；
- `unknown`：调用异常或可能部分写入；metadata 保持 `staged`，禁止发布 manifest。

### 5.2 ContentProbe

```python
class ContentProbe(Protocol):
    def probe(trade_day: str, symbols: list[str]) -> ProbeResult: ...
```

`ProbeResult`：

```text
exists: bool
row_count: int
content_hash: str | None
```

Batch 4 只实现 fake probe；真实 h5i 读回和 hash 计算在 Batch 5 实现。

### 5.3 协调入口

实现提供三个纯协调动作：

```text
stage_batch(root, metadata, canonical_batch)
commit_staged(root, batch_id, sink)
reconcile_staged(root, batch_id, probe)
```

所有路径由 `root` 参数注入，禁止函数内部依赖当前工作目录。

## 6. 状态和失败处理

```text
canonical batch
    ↓ quality passed
atomic staging write
    ↓ success
metadata = staged
    ↓ sink.commit
committed → atomic manifest → metadata = committed
failed    → metadata = failed, no manifest
unknown   → metadata remains staged, reconcile required
```

### 6.1 occupied_unknown

`occupied_unknown` 是 reconcile/提交结果，不新增 Batch 1 的 `IngestStatus` 枚举值。
Batch 4 只负责返回该结果并阻止新批次；人工确认成功、失败或需要重写的退出路径推迟
到 Batch 5。

### 6.2 幂等

- 已 committed 的同一 `batch_id` 重复提交：返回已提交，不重复调用 sink；
- 同 `trade_day` 的不同 batch_id：拒绝；
- staged 批次重复提交：仍需持有该交易日锁，并重新执行 probe/commit 规则；
- 不自动覆盖既有 h5i 数据。

## 7. 并发

Batch 4 使用进程内按 `trade_day` 的全局锁，锁在 `finally` 中释放，因此 sink 抛出
异常也不会永久占锁。

本批不实现超时和跨进程文件锁。未来启用多进程时必须升级为跨进程锁并定义过期锁
恢复规则；在此之前不允许把内存锁误称为跨进程互斥。

## 8. TDD 验收清单

1. staging 原子写入；
2. staging 成功后才创建 metadata；
3. metadata 初始状态为 `staged`；
4. manifest 只能在 sink 成功后创建；
5. sink 明确失败不创建 manifest；
6. sink 未知失败保持 staged；
7. manifest 内容包含 `source_tier`；
8. manifest row count/hash 正确；
9. canonical hash round-trip 稳定；
10. probe 返回 exists、row_count、content_hash；
11. 孤儿 staged 与 probe 完全匹配后转 committed；
12. probe 不匹配时保持 staged；
13. 同交易日第二批次被拒绝；
14. 同 batch_id 重试幂等；
15. 异常后交易日锁释放；
16. 测试路径全部来自 `tmp_path`；
17. occupied_unknown 阻止新批次。

## 9. 非目标

- 真实 Baostock/AkShare/mootdx/ZZShare 网络调用；
- 真实 h5i sink/probe；
- staging 30 日清理；
- occupied_unknown 人工解除流程；
- dashboard、daemon、审计账本接入；
- 多进程锁；
- Phase E 或交易执行链路。
