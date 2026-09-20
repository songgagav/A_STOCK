# 只用 Python 3.14 直读 h5i 段文件：可行性、版本校验与校验和评估

> 状态：**评估报告（未实施）**。用户 2026-09-20 决策 D 之后要求"可并行准备"本项，
> 目标是在**不依赖 h5i_db**（其原生扩展仅支持 CPython 3.10）的前提下，用 pyarrow 直接
> 读取 `data/h5i/market.db` 的段文件，从而让持有 torch 的 3.14 解释器一站式跑通 DRL。
> 本报告只做**评估**，不实施；结论中的"能否校验"均已实测，非推测。

## 1. 实测的落盘布局

`data/h5i/market.db` **是一个目录**，不是单文件库。实测结构：

```
market.db/
  FORMAT                 # {"format_version":1,"min_reader_version":1,
                         #  "created_at_ns":...,"created_by":"h5i-db 0.1.6"}
  CATALOG.lock
  catalog/tables/<sha256(name)>.json
                         # {"name":"daily_bars","table_id":"<uuid>",
                         #  "created_at_ns":...,"spec_revision":1,"checksum":"<64hex>"}
  tables/<uuid>/
    HEAD                 # {"format":1,"table_id":...,"sequence":8,
                         #  "manifest_checksum":"<64hex>"}
    manifests/000000000008.json
                         # {"format":1,"sequence":8,"parent":7,
                         #  "parent_checksum":"<64hex>","op":"append",
                         #  "schema_revision":1,"rows":16452776,"bytes":481459451,
                         #  "time_range":[...],
                         #  "segments":[{"id","path","rows","bytes","checksum",
                         #               "time_range","sorted","schema_revision",
                         #               "columns":{col:{min,max,null_count,
                         #                               distinct_values}}}, ...]}
    spec/00000001.json   # schema 定义
    segments/<id>.parquet
```

表名 → table_id 的映射在 `catalog/tables/*.json` 里（文件名是**表名的 sha256**）。
实测 `daily_bars`：`table_id=06efd5b7-…`、`spec_revision=1`、`sequence=8`、
manifest 声明 `rows=16452776`、`bytes=481459451`、`segments=129`。
每个 manifest 的 `segments[]` 还带**逐列统计**（min/max/null_count/distinct_values）
—— 这对"读之前先判断该段是否可能包含目标日期"很有用（`time_range` 可按段跳过）。

## 2. 版本校验方案（**可行，且有in-file依据**）

| 校验项 | 依据字段 | 处置 |
|---|---|---|
| 格式版本 | `FORMAT.format_version` | 必须 `<= SUPPORTED_FORMAT`；否则**拒绝读取并响亮报错** |
| 最低读者版本 | `FORMAT.min_reader_version` | 必须 `<= READER_VERSION`；否则拒绝（这是上游给的**明确兼容性声明**） |
| 生成者 | `FORMAT.created_by` | 只记录（诊断用），如 `h5i-db 0.1.6` |
| 单表格式 | `HEAD.format` / `manifest.format` | 必须一致且已知，否则拒绝 |
| schema 版本 | `catalog.spec_revision` / `manifest.schema_revision` | 未知版本**视为致命**（不猜测列义） |
| 快照有效性 | `HEAD.sequence` → `manifests/<seq>.json` 必须存在 | 缺失即拒绝（半提交/被截断） |

**结论**：版本校验**可以做得比 h5i_db 更严格**，因为 `min_reader_version` 就是上游
为读者准备的兼容性闸门。这是本方案最扎实的一块。

## 3. 校验和方案（**这里有硬约束，必须如实说明**）

实测发现（这是本节最重要的结论）：

1. **声明值与文件 sha256 不一致**：
   - `HEAD.manifest_checksum = 39421f6f48e5ced7…`，而 manifest 文件的
     `sha256(file bytes) = 505840823cad315f…` ⇒ **不一致**；
   - 某 segment 声明 `checksum = ed81cfb2ee719208b5a202f7…`，而该 parquet 的
     `sha256(file bytes) = 480948a2fb7e5e4d3bbee7d5…` ⇒ **不一致**。
   - 唯一对得上的是 `bytes`：声明 `3354556`，实际 `3354556` ⇒ **一致**。
2. **算法不在 Python 层**：在 h5i_db 安装目录里检索 `checksum` / `hashlib` / `sha256`
   只命中 `backtest*.py` 里与库格式**无关**的自用哈希；`manifest/segment/HEAD/catalog`
   的 checksum 计算全部位于**原生扩展 `_native.pyd`**（仅 CPython 3.10），
   Python 侧读不到其算法。

⇒ **因此：**

| 能做的（py3.14 + pyarrow） | 不能做的 |
|---|---|
| 读 `FORMAT` 做版本闸门（见 §2） | **独立重算** manifest/segment 的声明 checksum |
| 交叉校验 `bytes`（声明 vs 文件实际大小，O(1)） | 由声明 checksum 证明"段字节未被篡改" |
| 交叉校验 `rows`（各 segment `rows` 之和 vs manifest `rows`；列级 `null_count` 与实读比对） | 用 h5i 的 checksum 做**真实性**验证（无算法、无密钥） |
| 校验 manifest **链的自洽性**：child 的 `parent_checksum` 是否等于其 `parent` 清单里自报的 checksum（**声明 vs 声明**） | 判定"这份声明本身是否被改过" |
| 逐段 `time_range` 跳读、列统计预筛 | |

**推荐的自有校验和方案（TOFU，trust-on-first-use）**：
首次成功读取某 segment 时，用 pyarrow 读出的**逻辑内容**（或原始文件字节）计算
**我们自己的 sha256**，连同 `(table_id, segment_id, rows, size)` 写入一份 sidecar 台账
（如 `data/h5i/_reader_cache.json`）。
之后每次读取校验：
- `size` 必须与声明 `bytes` 及台账一致（廉价，抓截断/替换）；
- `rows` 必须与 manifest 声明及台账一致；
- 台账里的 sha256 必须与实算一致（抓内容变更）；
并在**首次遇到新 segment** 时明确记录"首次登记"事件（而非静默信任）。

这能提供**相对首次读取的篡改检测与截断检测**，但**不能**提供"与 h5i_db 视角一致的
权威校验"，也无法检测"首次读取时就已经是错的"。此限制必须写进文档与日志，
不能让下游误以为"校验通过 = 数据可信"。

**开销**：`daily_bars` 单表 129 段 / ~459 MB。每次全量 sha256 约为读盘量级。
故建议分级：**每次运行**做版本闸门 + `bytes`/`rows` 交叉校验（O(1) 或 O(段数) 且不读内容）；
**深度校验**（实算 sha256）只在首次登记、或显式 `--deep-verify`、或文件 mtime/size 变化时做。

## 4. 风险与结论

| 风险 | 说明 | 缓解 |
|---|---|---|
| **格式脆性** | 依赖 h5i_db 内部布局（manifest 链、catalog 命名）；上游一旦改版即失效 | §2 的版本闸门**强制**在 `format_version`/`min_reader_version` 变化时拒绝读取（响亮失败，而不是读出错数据） |
| **无法权威校验** | 见 §3；checksum 算法在 3.10-only 原生扩展内 | TOFU 台账 + 明确声明其局限；不把"校验通过"表述为"数据可信" |
| **静默错读** | 若某天布局变化而版本字段未变，可能读到不完整快照 | 必须校验 manifest 链自洽（`parent_checksum` 递推至 `parent=None`）+ `rows` 合计 + `HEAD.sequence` 存在性 |
| **并发写入** | 写入侧可能有 `HEAD.lock`/`CATALOG.lock`；读者需处理"读到正在提交的快照" | 读 `HEAD` → 读对应 manifest → **复读 HEAD 确认 sequence 未变**（乐观快照读） |
| **绕过官方接口** | 下游若同时存在 h5i_db 与直读两条路径，可能出现语义漂移 | 直读实现必须**单点封装**（一个模块），并保留 `source` 标记（沿用 `_load_plan_frame` 的 `res["source"]` 做法） |

**结论**：**技术可行，且版本校验能做得很扎实**（上游提供了 `min_reader_version`）。
但**校验和一项无法照搬**——h5i 的 checksum 不是文件哈希、算法只在 3.10 原生扩展里，
故 py3.14 侧只能做"声明之间自洽 + 大小/行数交叉校验 + 自有 TOFU 台账"，
**不能**声称达到了 h5i_db 的权威校验级别。若用户接受这一局限，建议按 §2+§3 实施；
若要求"校验强度不低于 h5i_db"，则本路径**不满足**，应回到 (a)/(c) 方案。
