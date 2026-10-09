# 清理阶段调用图索引

记录日期：2026-10-03
来源分支：`cleanup/phase0-baseline`；清理结果已合并至 `main`

这些文档是 Phase 2 的调用库存和判定入口，不把字符串命中直接等同于生产调用。每份报告同时区分源码、测试、文档和运维入口；Phase 4～7 的退役/合并决策必须在此基础上再做运行路径确认。

## 扫描口径

扫描排除了：

- `_archive/**`：已归档文件不是活跃生产路径；
- `data/**`、`logs/**`：运行数据和日志不作为代码调用方；
- `.git/**`：版本库内部文件不参与判断。

六份报告对应的实际匹配数量：

| 报告 | 关键词命中 |
| --- | ---: |
| [`duckdb-call-map.md`](duckdb-call-map.md) | 532 |
| [`service-lifecycle-call-map.md`](service-lifecycle-call-map.md) | 575 |
| [`fml-call-map.md`](fml-call-map.md) | 129 |
| [`ic-call-map.md`](ic-call-map.md) | 336 |
| [`h5i-call-map.md`](h5i-call-map.md) | 174 |
| [`process-probe-call-map.md`](process-probe-call-map.md) | 105 |

Phase 4–7 的边界结论：

- [`duckdb-boundary.md`](duckdb-boundary.md)：保留兼容层，不再新增 DuckDB 调用；
- [`fml-role.md`](fml-role.md)：标注 `ml_fusion_bridge` 的生产/回退角色和退役条件；
- [`service-lifecycle.md`](service-lifecycle.md)：明确 daemon、keepalive、run_services 与 Celery 的职责边界；
- [`env-blockers.md`](env-blockers.md)：记录 bandit/pip-audit/detect-secrets/coverage/deptry 缺失时的替代检查。

原始复核命令见每份报告。命中数量包含注释、测试和文档，不能直接作为删除依据。

## 后续审计与环境记录

### 安全扫描

- [`bandit-triage.md`](bandit-triage.md)：全量 Bandit 分组和未闭环项；扫描非零，不是通过证明。
- [`bandit-b608-audit.md`](bandit-b608-audit.md)：B608 中危置信度首批 34 条审计队列。
- [`bandit-b608-low-2026-10-03.md`](bandit-b608-low-2026-10-03.md)：低置信度 B608 第二批 30 条逐点记录；剩余 62 条继续排队。
- [`config-external-ref-check.md`](config-external-ref-check.md)：Phase 12 零引用配置的外部引用复核及删除结果。

### 环境与依赖

- [`env-blockers.md`](env-blockers.md)：扫描工具、coverage/deptry、h5i 双环境和回归基线的阻断记录。
- [`pyproject-migration-design.md`](pyproject-migration-design.md)：未来依赖声明迁移设计；本轮不创建 `pyproject.toml`。

### 边界文档

- [`duckdb-boundary.md`](duckdb-boundary.md)：保留兼容层，不新增 DuckDB 调用。
- [`fml-role.md`](fml-role.md)：`ml_fusion_bridge` 生产/回退角色。
- [`service-lifecycle.md`](service-lifecycle.md)：daemon、keepalive、run_services 与 Celery 的职责边界。

## 当前状态（2026-10-03）

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| Phase 0–12 清理 | 已完成并合并 | 主分支 tag：`signal-freeze-a-d-merged` 之前的清理基线保持有效 |
| B608 低置信度第二批 | 已记录 | 30/92，未修改源码；62 条待后续批次 |
| 依赖迁移 | 仅设计 | 不创建 `pyproject.toml`，不改变 CI |
| 09:25 Shadow 观察 | 待交易日 | 10-03 至 10-07 不计数，按日历等待下一交易日 |
| enforce / 实盘 | 未启用 | 需要另行完成 5 个真实交易日证据和人工批准 |
