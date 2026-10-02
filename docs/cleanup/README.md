# 清理阶段调用图索引

记录日期：2026-10-02  
分支：`cleanup/phase0-baseline`

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

## Phase 3.3 路径常量统一：已关闭

**结论**：不合并三处路径定义。

**原因**：

- 经调用图确认，三处定义分别属于读取、写入和测试 fixture 边界；
- 强行合并会引入新的模块耦合，与“只清理、不加功能”的原则冲突；
- 真正需要统一的是环境变量名和默认值，不是变量本身。

后续只有在环境变量命名不一致造成实际问题时，才单独处理该项。

## 已知测试缺口（下轮处理）

| 模块 | 风险 | 优先级 |
| --- | --- | --- |
| `factor_fusion.py` | 核心融合逻辑缺少独立单元测试 | 高 |
| `target_weighting.py` | 权重分配边界覆盖不足 | 中 |
| `selector.py` | 选股主流程缺少入口级 smoke 测试 | 中 |

补测原则：只验证业务契约，不锁定实现细节；不引入新依赖。

## P0 只读审计结果（2026-10-02）

- `TODO/FIXME/XXX/HACK`：未发现实际待办标记；命中内容是接口示例或历史说明；
- `print(...)`：源码中有 965 处，主要分布在诊断、回填、审计和命令行报告脚本，暂不批量删除；
- 静默异常 AST 审计：决策链路 97 处、数据链路 27 处、其它链路 720 处。该结果只生成审计清单，不等同于全部缺陷；后续按决策链路优先逐处确认日志、返回值和降级语义。

本轮只处理确定无引用的配置项和明确的静默降级；任何需要改变回退策略的修改另开专项提交。
