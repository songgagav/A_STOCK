# 环境阻断与轻量替代检查

记录日期：2026-10-02  
适用分支：`evolution/p0-regime-position`

## 当前状态

本轮已重新探测工具并执行可运行的扫描。工具可用不等于扫描通过；超时或非零结果均保留为阻断：

| 工具 | 用途 | 当前结果 | 替代证据 |
| --- | --- | --- | --- |
| `bandit` | Python 静态安全扫描 | 已运行但非零：0 high、126 medium、343 low；按 `-ll` 不通过 | 需对 B608 等动态 SQL 命中逐项分级，未将 0 high 误报为全通过 |
| `pip-audit` | 依赖漏洞扫描 | 工具可用，但 `-r requirements.txt --no-deps` 等待外部源超过 90 秒后中断；无结论 | 保留为 blocked，网络/镜像可用后重跑 |
| `detect-secrets` | 秘密扫描 | 工具可用，但当前工作树扫描超过 90 秒无输出后中断；无结论 | 保留历史人工扫描证据，并拆分工作树/历史提交扫描 |
| `coverage` | 测试覆盖率 | 未安装 | 使用 pytest 收集清单和完整回归 |
| `deptry` | 依赖使用分析 | 未安装 | 使用源码导入清单与 requirements 人工对照 |

“未安装”只表示该检查未执行，不代表项目通过对应工具的判定。

## 已生成的替代证据

- `tests_list.txt`、`imports.txt`：本轮未生成。
- `config_usage.txt`：已于 2026-10-02 生成 35 个配置键的标识符边界统计；其中 9 个零引用配置已完成外部引用复核、逐项删除和逐项回归，详见 `config-external-ref-check.md`。
- 历史记录中的 pytest 回归数字只作为旧基线，不能替代本轮全量回归。

这些文件是审计辅助材料，不是完整的依赖或安全证明。导入计数包含注释、字符串和兼容代码，配置命中也不能证明配置一定被运行时读取。

## 后续执行顺序

1. 对 Bandit 的中危 SQL 命中做输入边界审计并形成逐项例外/修复清单；
2. 使用锁定依赖执行 `pip-audit -r requirements-lock.txt`，并记录包版本和例外；
3. 将 detect-secrets 拆成可在 Windows 完成的工作树分区扫描，再覆盖 Git 历史；历史伪造测试值要保留提交/文件/原因留痕；
4. 将三项扫描加入 CI，工具缺失时必须返回显式 blocked，而不是成功；
5. `coverage`、`deptry` 安装后再补完整报告，不删除当前替代证据。

## 当前回归基线

旧基线为 `2637 passed, 50 skipped, 18 warnings`。2026-10-02 `.venv314` 全量结果为 `2680 passed, 29 skipped, 18 warnings`；跳过明细为 24 项依赖 `h5i_db`、5 项依赖 `baostock`。此前的 50 与本轮 29 来自不同环境/测试状态，不能直接解释为 21 项因 `importorskip` 变成通过；后续比较必须保存同一解释器、依赖锁定和提交的 skip 清单。

## h5i_db 双环境测试策略

这不是“修复 h5i_db 未安装”，而是把可选集成能力缺失从失败变成显式跳过；真实 h5i 验证仍由专用解释器承担。

- `.venv314`：日常单元/常规集成回归；h5i 相关测试通过 `pytest.importorskip("h5i_db")` 显式跳过，Baostock 真实 fetcher 在未安装 `baostock` 时同样跳过。
- `.venv310`：h5i/回填专用验证环境；本轮 Baostock 触发→写入→留痕定向集为 `249 passed`，真实 h5i 写入测试在此环境执行。
- CI 已有 `regression-h5i`（Python 3.10 + `h5i-db`）。CI #192 的本地可复现原因已由
  测试夹具修复：无日历 checkout、错误的临时 Arrow schema、以及对生产 h5i 数据库的
  隐式依赖。`.venv310` 的自包含定向集为 `32 passed`；仍需 push 后等待远端 job，
  在 GitHub Actions 给出成功退出码前不得标记为通过。

## 2026-10-05 扫描复核

在 `feature/data-source-router-v2` 的 `.venv314` 中重新执行：

- `pip-audit -r requirements.txt --format columns`：退出码 0，`No known vulnerabilities found`；
- `detect-secrets scan --force-use-all-plugins .`：退出码 0，结果为空；
- `bandit -r src -ll -f txt`：退出码 1，0 high、125 medium、346 low，仍按未通过处理。

`detect-secrets --all-files` 会包含运行时数据、缓存和日志，并产生高熵伪阳性，
因此不能用它替代默认仓库扫描的清洁结果。完整任务状态和 ArcticDB 兼容层边界见
`docs/evolution/post-point5-status-2026-10-05.md`。
