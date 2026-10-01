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

原始复核命令见每份报告。命中数量包含注释、测试和文档，不能直接作为删除依据。
