# 看板优化执行记录

本轮在 `feature/dashboard-optimization` 上先落地第一批低耦合改动，保持默认
`shadow` 行为，不改变虚拟盘的交易消费路径。

## 已完成

### 冻结状态与视觉层

- 新增 `GET /api/signal-freeze`，只读并验证当日 `signal_snapshot_YYYYMMDD.json`。
- 返回 `ready / missing / invalid / tampered / unavailable`、生成时间、hash 前缀、模式、迟到数和未解释差异数。
- 顶栏增加 09:25 冻结轨道：状态异常使用红色 fail-closed 视觉，正常状态使用冰青色；同时展示 `shadow/enforce`、时间、hash、迟到和未解释计数。
- 缺少 `live_state.json` 时使用源文件时间，不再用当前时间伪装旧状态为新鲜数据，并标记 `stale`。
- 看板的 KPI 说明改为 h5i / parquet 口径，避免继续向维护者暗示 DuckDB 是首页主数据源。
- `/api/live` 根据源文件 `mtime_ns + size` 返回轻量 ETag；客户端带相同 `If-None-Match` 时返回 `304`，不重复传输 JSON。服务端不会为每次请求重新读取全文件计算 SHA-256。

### 接口边界与降级

- `/api/db_table/<name>` 只接受 `_DB_TABLE_ZH` 中的精确名称；合法表名返回 200，大小写变体、路径片段、百分号编码、SQL 注入片段、空值和超长输入均返回 HTTP 403 / `TABLE_NOT_ALLOWED`。
- 缺少 `h5i_db` 时，h5i 读取降级为结构化错误，不再让 HTTP 工作线程抛出未处理异常。

### 数据层与轮询调度（Batch 2）

- `read_regime()` 的物化视图读取和 DuckDB fallback 统一为 named-row 字段契约；列顺序变化不会再通过 `row[0]` 静默错位。
- 物化文件缺列或行宽异常时返回 `None`，继续进入既有 fallback，不返回截断字典。
- 新增 [`dashboard-data-source-matrix.md`](dashboard-data-source-matrix.md)，记录主要 API 的主源、降级源和空数据语义。
- 前端主轮询改为可见性敏感的自适应调度：隐藏页面暂停，失败指数退避，恢复可见后立即刷新；各接口原有基础周期保持不变。

## 验证

- 看板契约测试：`15 passed`，覆盖合法表名 200、大小写绕过、路径穿越、SQL 注入、空值和 10000 字符超长输入 403。
- `.venv314` 全量回归：`2728 passed, 29 skipped, 18 warnings`（126.96 秒，退出码 0）。基线 `baseline_test.txt` 为 `2680 passed, 29 skipped, 18 warnings`；本轮跳过数保持 29，未上升。
- PR #10 的 CI run `#196` 当时 4 个 job 全绿（3.11、3.12、DRL、h5i），但 h5i 专用 job 只运行 `test_baostock_backfill.py` 和 `test_h5i_accel_equivalence.py`，没有执行信号冻结测试；core job 的通过依赖于没有 h5i/ML 融合输出的环境，属于环境性假绿和测试覆盖缺口，不是对 h5i-enabled 路径的验证。
- 已修正 Phase B 测试契约：`test_at_0925_engine_freezes_weighted_live_targets` 只断言冻结必需的 `canon`、`target_weight` 和快照权重，允许既有 `fml` / `fml_source` 诊断字段存在。h5i-enabled `.venv310` 和 `.venv314` 的全部 `test_signal_*.py` 均为 `57 passed`。
- CI 覆盖已先行修正：`.github/workflows/ci.yml` 的 h5i job 现在额外运行全部 `tests/test_signal_*.py`，使快照、迟到信号、清理、watchlist、冻结引擎和账本测试都在 h5i-enabled 环境执行。
- 推送后需以新的 GitHub Actions run 作为 h5i 覆盖验收证据。Windows PowerShell 不展开 `tests/test_signal_*.py`，本地验证使用显式文件列表；GitHub Actions 的 Ubuntu shell 会正常展开该 glob。
- 全量回归中的另一项已修正为测试环境隔离问题：`test_alert_rules_single_source` 现在排除仓库内注册的 `.worktrees`，避免把嵌套工作树中的同名配置误判为第二份生产配置。
- `py_compile src/dashboard.py`：通过。
- Batch 2 看板契约：`.venv314` `21 passed`；`.venv310` `21 passed`。
- 真实 HTTP 烟测：冻结状态、h5i 降级、表名白名单均符合预期。
- ETag HTTP 烟测：首请求 `200`，同 ETag 重复请求 `304`；算法为 O(1) 的 `mtime_ns + size`。

### 提交前结论

- 四项检查中，ETag 与白名单矩阵已通过，跳过数未增加。
- 当前本地全量回归已通过；远端 h5i job 的新覆盖仍待推送后验证。看板优化、CI 覆盖修正和冻结测试契约修正均未修改冻结生产逻辑。
- 本文档只记录本批看板改动和验证结果；仓库中其他未提交文档改动保持原样，不在本批提交范围内。

## 后续清单（尚未在本批次实现）

1. **本批已完成第一步**：`read_regime()` 的物化 Parquet 与 DuckDB fallback 已统一为 named-row 字段契约；数据源边界见 [`dashboard-data-source-matrix.md`](dashboard-data-source-matrix.md)。DuckDB 旧链路仍保留，待后续迁移批次处理。
2. 在 ETag 基础上评估 SSE，确认连接生命周期和守护重启语义后再实现。
3. 抽离内联 HTML/CSS/JS，随后做增量 DOM 更新。
4. 统一 API 错误 envelope、健康检查 TTL、结构化日志和看板进程纳管审计。
5. 补充并发读写、缓存竞态和生产数据源双环境回归。

## Visual V1 运行验证与旧面板边界

- `ee1f9bf` 将 Visual V1（设计令牌、布局骨架、空状态）和 Visual V2（ticker、topbar、交易时段轨道、冻结审计轨道）合并在同一视觉提交中；视觉契约测试单独位于 `4422911`。
- 旧面板没有删除：`#cards`、`#eqCanvas`、`#kCanvas`、`#flabCanvas` 及其对应渲染逻辑仍然保留。
- 新 CSS 目前通过共享的 `.panel`、`.card` 规则覆盖旧面板的基础容器样式，但没有针对上述旧 ID 的专门重写；旧 canvas 的内联背景、尺寸和部分旧变量仍可能造成新旧风格混合。
- 当前决策是逐块替换：后续 V3–V7 分批处理 Hero、净值主图、审计/因子暴露、瀑布图及其余旧视图，避免一次性删除既有数据视图。当前代码中没有独立的瀑布图组件。

### localhost:8000 运维阻塞

- `localhost:8000` 当前由 PID `14500` 提供，返回的是旧页面，不包含 `ticker`、`freezeTrack`、`sessionRail`。
- 当前用户尝试停止该 PID 时收到“拒绝访问”；`src/daemon.py` 当前只提供守护进程 `--stop/--status`，没有独立的 dashboard restart 命令，且状态检查显示守护进程处于 HALTED，因此本轮不直接停止整个守护进程。
- 新视觉已在独立的 8765 端口完成 HTTP 与浏览器验证；8000 的真实端口验证待管理员权限或服务管理入口重启后完成。

## Visual V3：Hero 日收益序列

- `/api/live` 新增向后兼容的 `daily_series` 字段，聚合 `data/daily/*/daily_summary.json` 中的有效净值记录，最多返回最近 20 个交易日。
- 当前有效历史为 16 个交易日，前端诚实显示 16/20，不补零、不伪造历史。
- 序列字段为 `day`、`equity`、`nav`、`dd`、`daily_return`；回撤和收益使用小数单位。
- 保留 `#cards` 作为旧 JS 挂载点，新增 `#heroSeries`；无历史时显示“历史数据积累中”。
- 后端和前端分别提交：`89cd995`、`f662403`。
- `.venv314` 全量回归：`2754 passed, 29 skipped, 18 warnings`；临时 8766 端口 HTTP 验证返回 16 条 `daily_series`。
- 8000 若仍运行旧进程，需要重启后才能加载 V3 静态资源；不改变交易消费路径。
