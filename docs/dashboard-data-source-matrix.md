# 看板数据源覆盖矩阵

本文件记录 `src/dashboard.py` 的主要 API、权威来源、降级路径和空数据语义。
它是数据源迁移和看板改造的边界清单，不等同于“所有 DuckDB 调用已删除”。

## 约定

- **主源**：默认运行路径优先读取的来源。
- **降级源**：主源不可用时才尝试的来源；降级必须保留错误或状态信息，不能把异常伪装成新鲜数据。
- **空数据**：没有可用记录时返回空结构或明确错误，不能用当前时间伪装更新时间。
- `BAR_STORE=h5i` 是默认的行情读取模式；`BAR_STORE=duck` 仅用于兼容和对照。

## API 矩阵

| API | 主源 | 降级/辅助来源 | 缺失时的语义 | 当前验证 |
|---|---|---|---|---|
| `/api/live` | `data/live_state.json` | `data/state.json` | 返回回退状态，并用源文件 mtime 标记 `stale` | ETag 契约测试、HTTP 烟测 |
| `/api/signal-freeze` | 当日已校验 `signal_snapshot_YYYYMMDD.json` | `signal_freeze_events.jsonl`、当日 `late_signals_YYYYMMDD.json` 仅用于计数/审计 | `missing/invalid/tampered/unavailable` 显式展示，不能重算目标池 | 冻结状态契约测试 |
| `/api/history` | `data/daily/*/daily_summary.json` | 无 | 缺失日期跳过，保留可用历史 | 既有历史回归 |
| `/api/market` | `data/market/<day>/market.json` | 无 | 返回空日期列表或错误信息 | 既有面板回归 |
| `/api/regime` | `data/views/parquet/v_market_breadth.parquet`、`v_factor_ic_latest.parquet` | 同名 DuckDB 视图 | Parquet 缺失/缺列时才 fallback；两条路径使用同一 named-row 字段契约 | 本批 named-row 契约测试 |
| `/api/marketboard` | h5i `daily_bars`（默认）+ `market.json` | `BAR_STORE=duck` 时走 DuckDB 等价实现 | h5i 无数据返回结构化错误，不抛出未处理异常 | 既有 h5i/duck 等价回归 |
| `/api/abnormal` | h5i `daily_bars`（默认） | DuckDB 等价查询 | 返回 `ok=false` 与错误原因 | 既有 h5i/duck 等价回归 |
| `/api/kline` | h5i `daily_bars` | DuckDB 兼容读取 | 无标的或无数据返回空结构/错误 | 既有接口回归 |
| `/api/health` | 运行期进程/数据源状态 + `data/health/premarket.json` | 无 | 结构化 `level/checks`，不以 HTTP 200 代替业务健康 | health 合约回归 |
| `/api/views` | `data/views/build_meta.json` | 无 | 返回 `ok=false`，提示先构建视图 | 文件源回归 |
| `/api/target_plan` | `data/drl/<day>/target_plan.json` | 当日目录 | 返回 `ok=false` 与生成提示 | DRL 相关回归 |
| `/api/logs` | `logs/` 增量读取 | 无 | 返回当前位置和可读错误 | 既有日志面板回归 |
| `/api/db_table/<name>` | 已登记表名对应的数据读取器 | 按 `BAR_STORE` 选择 h5i/兼容路径 | 非精确白名单一律 403 `TABLE_NOT_ALLOWED` | 8 类输入边界测试 |

## 视图读取契约

`read_regime()` 的宽度和因子 IC 两个组件现在都遵循同一规则：

1. 物化 Parquet 使用显式列名读取。
2. 返回 `list[dict]`，调用方通过字段名访问，而不是 `row[0]` 位置访问。
3. 物化文件缺失、读取失败或缺少必需列时返回 `None`，触发既有 DuckDB fallback。
4. DuckDB fallback 也通过同一列名列表映射为字典行。
5. 其他仍依赖 tuple 的 DuckDB 查询暂不改动，避免把本批次扩大为全仓数据源迁移。

## 迁移边界

以下事项不属于本批次，不能从矩阵的“降级源”描述推断为已完成：

- 删除 `dashboard.py` 中全部 DuckDB 兼容调用；
- 把所有 API 统一到 h5i；
- Parquet/视图构建任务本身的 schema 版本管理；
- SSE、页面隐藏退避、HTML/CSS/JS 文件拆分；
- 并发写入、缓存竞态和生产数据源双环境压测。

后续任何数据源改造都应先更新本矩阵，再补对应 API 的 contract test。
