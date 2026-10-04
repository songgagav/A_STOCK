# 依赖人工复核记录

记录日期：2026-10-04
范围：`src/**/*.py` 的顶层 import 与 `requirements*.txt` 对照。
处置原则：只记录候选，不因静态 import 计数直接删除依赖。

## 代码中出现、但未在 requirements 文件声明的模块

| 模块 | 判断 | 处置 |
|---|---|---|
| `h5i_db` | Python 3.10 主数据源原生扩展 | 由 `.venv310` 安装脚本维护，不加入 3.14 公共依赖 |
| `baostock` | 回填/数据同步可选运行依赖 | 保留，按 `.venv310` 入口使用 |
| `akshare` | 数据更新兜底路径 | 保留，独立环境按需安装 |
| `vnpy` | 纸面盘/执行相关可选组件 | 保留，不能由静态 import 判断为死依赖 |
| `stock_sdk` | 厂商行情引擎外部 SDK | 保留，属于部署环境依赖 |
| `fetch_orderbook_daily` | 外部/本地行情接口 | 需部署环境确认，不删除 |
| `gplearn` | 研究/因子挖掘路径 | 需研究脚本运行时确认，不删除 |
| `arcticdb` | 已有退役/降级记录 | 不在本批处理，按退役文档管理 |

## requirements 中未作为直接 import 出现的项目

`bandit`、`pip-audit`、`detect-secrets`、`pytest`、`flower`、`redis`、`h5py`、
`seaborn`、`deap` 等属于安全扫描、测试、观测栈或可选研究依赖。它们不能按生产
源码直接 import 的结果判定为未使用。

## 结论

本批没有安全的依赖删除项。未来若要瘦身，应按“一个依赖、一个调用族、一次回归”
执行，并优先检查可选运行入口，而不是仅按 AST import 计数删除。
