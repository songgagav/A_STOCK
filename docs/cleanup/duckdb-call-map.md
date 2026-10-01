# DuckDB 调用图

## 扫描结果

排除 `_archive/`、`data/`、`logs/` 后，关键词命中 532 条。复核命令：

```powershell
rg -n "duckdb|DUCKDB_PATH|legacy_stockdb|BAR_STORE|duck_available|DB_UPDATE_TARGETS|_h5i_mirror_if_enabled" `
  --hidden --glob '!.git/**' --glob '!_archive/**' --glob '!data/**' --glob '!logs/**' `
  --glob '*.py' --glob '*.ps1' --glob '*.bat' --glob '*.xml' --glob '*.md' .
```

## 调用关系库存

| 调用方 | 被调用/依赖 | 入口性质 | 当前判断 |
| --- | --- | --- | --- |
| `src/dashboard.py` | DuckDB 查询、`DUCKDB_PATH`、`update_db` | Web 面板和手动更新入口 | 活跃，不能删除 |
| `src/agent_tools.py` | DuckDB 只读查询、BAR_STORE 回退 | 工具/研究与兼容入口 | 需确认调用频率后再收敛 |
| `src/backtest_engine.py` | `DUCKDB_PATH`、`BAR_STORE=duck` 分支 | 回测兼容入口 | 保留或改为显式 legacy 适配器 |
| `src/cn_lake_feed.py` | DuckDB 连接封装 | 数据源兼容入口 | 需确认是否仍被外部脚本调用 |
| `src/build_factor_views.py` | DuckDB 物化/视图兼容逻辑 | 构建工具入口 | 需单独验证 h5i 等价链路 |
| `src/backfill_change_pct.py` | DuckDB 写入补录 | 一次性/补录入口 | 不应与生产读取链混为一谈 |
| `src/update_db.py` | 分段/全量更新链 | 数据维护入口 | 仍有 `dashboard`、`run_daily` 等调用关系 |
| `src/config.py` | `DUCKDB_PATH`、`DB_UPDATE_TARGETS`、历史标签 | 配置与兼容定义 | 不能只因注释写“退役”而删除 |
| `src/premarket_healthcheck.py` | 依赖状态与数据源检查 | 盘前健康入口 | 需保留明确的 retired/active 语义 |
| `tests/**`、`docs/**` | 迁移说明、回归约束 | 非生产 | 不能据此证明生产调用，但删除代码时需同步更新 |

## 判定

当前至少存在活跃 dashboard、数据维护和兼容读取路径，属于“部分引用仍在生产/运维链”的情形。结论是保留兼容层，后续只清理经过调用链确认的无引用分支，并修正文档中“已退役”与实际代码不一致的描述。

## 下一步

1. 为 `dashboard`、`run_daily`、`agent_tools` 和回测入口分别做一次运行路径确认。
2. 把 legacy DuckDB 访问集中到明确的适配模块，不能先删除常量再修调用方。
3. 在 h5i 等价读取和回退测试完成前，不删除 DuckDB 依赖。
