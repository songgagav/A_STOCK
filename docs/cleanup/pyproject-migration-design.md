# `pyproject.toml` 依赖迁移设计（不实施）

状态：设计稿，不创建 `pyproject.toml`，不改变当前安装方式和 CI 行为。  
日期：2026-10-03

## 当前依赖事实

仓库当前使用四层文件：

| 文件 | 作用 | 约束 |
| --- | --- | --- |
| `requirements.txt` | 公共运行入口 | 复用 `requirements_314.txt`，另含 `duckdb`、`polars` |
| `requirements_314.txt` | Python 3.14 核心科学计算依赖 | 不包含 h5i 原生扩展 |
| `requirements-dev.txt` | 测试、DRL、观测、安全扫描依赖 | 同时描述了 h5i/CPython 3.10 的隔离要求 |
| `requirements-lock.txt` | 当前环境的冻结版本记录 | 由环境导出，不能直接当作跨平台解析方案 |

`h5i-db` 只能在独立的 Python 3.10 环境中验证；它不能直接成为 Python 3.14 的默认依赖。CI 也已经把 h5i 放在独立的 Python 3.10 job 中。这个边界在迁移后必须保持。

## 推荐目标形态

采用“项目元数据 + 可选依赖组 + 现有锁定文件过渡”的方案，而不是一次性删除 requirements 文件：

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "a-stock-rotation"
requires-python = ">=3.10,<3.15"
dependencies = [
  # 仅放 Python 3.10/3.14 都支持的公共运行依赖
]

[project.optional-dependencies]
dev = []
drl = []
security = []
h5i = []  # 仅作为 3.10 环境的文档化组，不进入默认安装
```

上面是结构示意，不是可直接提交的完整依赖列表。实际列表必须先由现有文件、顶层 import、CI job 和锁定版本逐项对照后填入。

## 依赖分层约定

- `project.dependencies`：只放运行 `run_daily`、门控、回测所需且跨支持 Python 版本的依赖。
- `dev`：pytest、测试所需的 DuckDB/Polars/Prometheus 以及开发工具。
- `drl`：torch、gymnasium、stable-baselines3 和 DRL 测试依赖；保持 CPU/GPU 安装策略由 CI/安装脚本决定。
- `security`：bandit、pip-audit、detect-secrets；扫描工具缺失时必须显式 blocked，不能把未运行写成通过。
- `h5i`：只描述独立 3.10 环境的依赖边界；在实现阶段应由专用安装脚本或单独 constraints 文件消费，不能让 3.14 默认解析到该扩展。

## 迁移顺序（未来单独 PR）

1. 从四个 requirements 文件和源码顶层 import 生成候选清单，标记运行/测试/DRL/安全用途。
2. 对每个候选确认最小支持 Python 版本、平台 wheel、是否可放入默认依赖。
3. 先添加 `pyproject.toml` 的等价声明，同时保留 requirements 文件；CI 做双路径安装并比较收集结果。
4. 为 Python 3.10 h5i job 保留单独安装步骤和 import 验证，不让它复用 3.14 依赖组。
5. 生成按 Python/平台区分的锁定产物，验证本地 `.venv314`、`.venv310` 和三个 CI job。
6. 连续回归通过后，才在独立 PR 中决定哪些 requirements 文件转为兼容入口或退役。

## 验收条件

- `pytest --collect-only` 的测试集合与迁移前一致；
- `.venv314` 的常规回归、`.venv310` 的 h5i 定向回归和 DRL job 均可独立安装；
- `h5i-db` 不会被 Python 3.14 默认依赖解析；
- `requirements-lock.txt` 与新锁定方案的差异有逐包解释；
- CI 安全扫描、快照 shadow 观察和 `TRADE_BROKER=paper` 约束不被依赖迁移改变。

## 本轮明确不做

- 不创建 `pyproject.toml`；
- 不移动或删除四个 requirements 文件；
- 不升级依赖版本；
- 不修改 CI 的安装命令。

