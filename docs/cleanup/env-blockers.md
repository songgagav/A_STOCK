# 环境阻断与轻量替代检查

记录日期：2026-10-02  
适用分支：`cleanup/phase0-baseline`

## 当前状态

以下工具在当前 Windows 工作环境中未发现，因而没有伪报扫描通过：

| 工具 | 用途 | 当前结果 | 替代证据 |
| --- | --- | --- | --- |
| `bandit` | Python 静态安全扫描 | 未安装，未运行 | 等离线 wheel/网络恢复后运行；当前保留历史秘密扫描和人工复核 |
| `pip-audit` | 依赖漏洞扫描 | 未安装，未运行 | 先核对 `requirements*.txt` 与已知漏洞库；网络恢复后补跑 |
| `detect-secrets` | 秘密扫描 | 未安装，未运行 | 已有项目秘密扫描结果；本阶段补充历史提交审计 |
| `coverage` | 测试覆盖率 | 未安装 | 使用 pytest 收集清单和完整回归 |
| `deptry` | 依赖使用分析 | 未安装 | 使用源码导入清单与 requirements 人工对照 |

“未安装”只表示该检查未执行，不代表项目通过对应工具的判定。

## 已生成的替代证据

- `tests_list.txt`：本地生成的 pytest 收集清单，共 2687 个测试；不纳入版本库，避免测试名称中的占位符触发秘密扫描；
- `imports.txt`：本地生成的 `src/` 顶层 `import/from` 初步清单；不纳入版本库；
- `config_usage.txt`：本地生成的 `src/config.py` 大写配置名命中候选计数；不纳入版本库。

这些文件是审计辅助材料，不是完整的依赖或安全证明。导入计数包含注释、字符串和兼容代码，配置命中也不能证明配置一定被运行时读取。

## 后续执行顺序

1. 网络或本地 wheel 可用后，在隔离环境执行 `python -m bandit -r src -ll`；
2. 使用锁定依赖执行 `pip-audit -r requirements-lock.txt`，并记录包版本和例外；
3. 使用 `detect-secrets scan` 同时覆盖工作树和 Git 历史，历史伪造测试值要保留提交/文件/原因留痕；
4. 将三项扫描加入 CI，工具缺失时必须返回显式 blocked，而不是成功；
5. `coverage`、`deptry` 安装后再补完整报告，不删除当前替代证据。

## 当前回归基线

分类变更后的完整回归为 `2637 passed, 50 skipped, 18 warnings`。跳过项主要由当前环境缺少 PyYAML、baostock 或 h5i-db 引起，已由 pytest 显式记录。

## 替代检查方案

### 测试瘦身（Phase 10 替代）

`coverage` 未安装时，先收集测试清单：

    .venv314\Scripts\python.exe -m pytest tests --collect-only -q > tests_list.txt

人工检查重复或重叠的命名模式：

- `test_*_wiring.py`
- `test_*_paths.py`
- `test_*_all_paths.py`
- `test_*_helpers.py`

`tests_list.txt` 只作为本地审计产物，不纳入版本库。

### 依赖瘦身（Phase 11 替代）

`deptry` 未安装时，生成源码导入清单并与依赖文件人工对照：

    rg -o "^(import|from) \S+" src/ | Sort-Object -Unique > imports.txt
    Get-Content requirements.txt, requirements_314.txt, requirements-dev.txt

该方法不能替代完整的包解析，只用于筛出待复核候选。

### 工具恢复后

    .venv314\Scripts\python.exe -m pip install coverage deptry

安装成功后补正式报告；若安装失败，保持 `blocked` 标记，不将替代检查伪报为正式工具通过。
