# Phase 1 归档

归档日期：2026-10-02  
原因：大扫除第一批候选，确认无代码引用。  
观察期：一个版本周期。  
后续：若无恢复需求，下一轮永久删除。

- `NewFromHSU`：纯文本垃圾文件。
- `src/debug_pool.py`：一次性调试脚本，依赖已退役的 `legacy_stockdb.duckdb`。
- `update_db_segments.ps1`：退役 DuckDB 分段更新脚本。
