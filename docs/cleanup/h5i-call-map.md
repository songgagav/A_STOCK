# h5i 层调用图

## 扫描结果

排除 `_archive/`、`data/`、`logs/` 后，关键词命中 174 条。复核命令：

```powershell
rg -n "h5i_sync|h5i_ingest|h5i_rebuild|h5i_bar_store" `
  --hidden --glob '!.git/**' --glob '!_archive/**' --glob '!data/**' --glob '!logs/**' .
```

## 调用关系库存

| 调用方/模块 | 被调用/职责 | 入口性质 | 当前判断 |
| --- | --- | --- | --- |
| `src/h5i_bar_store.py` | h5i 核心读取 | 生产读取层 | 保留 |
| `src/h5i_sync.py` | h5i 同步 | 数据维护入口 | 需与 engine sync 对照 |
| `src/h5i_ingest.py` | h5i 写入/摄取 | 数据摄取入口 | 需确认是否与 bars ingest 重叠 |
| `src/h5i_rebuild.py` | 重建/一次性修复 | 工具入口 | 若无生产调用，移归档工具 |
| `src/engine_bars_sync.py` | Vendor Engine → h5i | 主数据链候选 | 优先确认是否为唯一同步入口 |
| `src/bars_ingest.py` | 行情摄取 | 数据链入口 | 与 h5i ingest 的职责边界待确认 |
| `src/baostock_adapter.py`、`src/baostock_backfill.py` | 补洞/回填 | research/backfill | 不应混入生产主链 |
| `tests/test_engine_bars_sync.py`、`tests/test_bars_ingest.py` | 数据链回归 | 非生产 | 迁移时必须保持契约 |

## 判定

h5i 核心读取层应保留。需要解决的是 `h5i_sync`、`h5i_ingest`、`engine_bars_sync`、`bars_ingest` 和 `h5i_rebuild` 的职责重叠，而不是按文件名直接删除。补洞适配器应与 Vendor Engine 主链分开。
