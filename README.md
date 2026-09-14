# A_STOCK — A 股量化轮动研究系统

[![CI](https://github.com/songgagav/A_STOCK/actions/workflows/ci.yml/badge.svg)](https://github.com/songgagav/A_STOCK/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A_STOCK 是一个面向 A 股的量化研究、回测与模拟盘系统，围绕以下闭环构建：

```text
行情/财务/估值数据
        ↓
因子计算与因子挖掘
        ↓
候选池过滤与组合构建
        ↓
IC 门控 / 风险因子 / DRL 动态权重
        ↓
A 股规则纸面撮合
        ↓
绩效归因、过拟合检测与运行监控
```

项目重点不是单一选股公式，而是建立一套可审计、可回放、可降级、可持续运行的研究基础设施。

> 本项目仅用于研究、教学和模拟交易，不构成任何投资建议。默认执行通道为 PaperBook 纸面撮合，不连接真实券商，也不保证任何历史结果能够在未来复现。

## 功能概览

| 模块 | 能力 |
| --- | --- |
| 数据层 | A 股日线、财务、估值、资金流、融资融券与本地数据湖接入 |
| PIT 数据 | 按 `as_of` 严格取历史可见数据，避免估值和股票池前视偏差 |
| 因子层 | 基本面、趋势、流动性、波动率、反转、治理因子及 ICIR 融合 |
| 因子挖掘 | 遗传规划因子生成、筛选、OOS 检验与因子注册 |
| 决策层 | 截面打分、候选池 Top-N、CVaR-PPO 动态因子权重、可解释分析 |
| 风控层 | IC 门控、因子健康隔离、回撤/波动/CVaR 约束、策略退化防御 |
| 执行层 | A 股 T+1、整手、涨跌停、停牌、滑点、佣金、印花税与止损模拟 |
| 评估层 | 回测、滚动样本外验证、CPCV/PBO、压力测试、收益归因 |
| 运行层 | 日频调度、盘中模拟引擎、健康检查、Web 看板、Prometheus 指标 |

## 核心设计原则

1. **研究信号与执行隔离**：LLM 或 DRL 只生成结构化信号/目标权重，不能绕过确定性风控直接下单。
2. **Point-in-Time 优先**：历史选股只使用当日及以前可获得的数据；估值补丁通过 as-of 合并读取。
3. **风险优先**：因子失效、IC 漂移、Sharpe 变点、组合回撤等情况可以降低暴露或冻结新买入。
4. **安全默认值**：实时引擎默认使用 PaperBook；未知数据、日历异常和策略降级会记录状态。
5. **可审计运行**：选股结果、目标权重、成交、净值、门控状态和训练元数据均支持落盘。

## 架构

源码采用扁平 `src/` 布局，逻辑上分为以下层次：

```text
数据层       db.py / h5i_bar_store.py / arctic_store.py
因子层       factor_fusion.py / factor_library.py / factor_mine/
决策层       selector.py / drl_train.py / target_weighting.py
风控层       factor_gate.py / risk_first.py / risk_factor_optimizer.py
执行层       paper_book.py / realtime_engine.py / backtest_engine.py
评估层       performance_report.py / attribution_analysis.py
             overfitting_test.py / strategy_validation.py
运维层       daemon.py / run_services.py / dashboard.py
             health_check.py / metrics_server.py
```

主要入口：

| 路径 | 用途 |
| --- | --- |
| `src/run_daily.py` | 收盘选股、因子/权重更新及日频任务 |
| `src/realtime_engine.py` | 盘中价格刷新与 PaperBook 模拟撮合 |
| `src/backtest_engine.py` | 连续交易日撮合回放 |
| `src/factor_gate.py` | IC 门控、因子漂移和状态机 |
| `src/drl_train.py` | CVaR-PPO 因子权重训练 |
| `src/overfitting_test.py` | 稳健性与过拟合检测 |
| `src/dashboard.py` | 本地 Web 看板 |
| `scripts/check_data_completeness.py` | 数据完整性巡检 |
| `scripts/nonoverlap_rerun.py` | 非重叠窗口样本外重跑 |

## 项目结构

```text
A_stock_rotation/
├── .github/                          # GitHub 工作流配置
│   └── workflows/
│       └── ci.yml                    # 核心回归 + DRL 回归
│
├── src/                              # 核心源代码（当前为扁平模块布局）
│   ├── db.py                          # 统一数据访问入口
│   ├── h5i_bar_store.py               # h5i 行情/财务/估值查询
│   ├── arctic_store.py                # ArcticDB 读写与审计存储
│   ├── build_factor_views.py          # 因子宽表与视图构建
│   ├── free_stockdb_sync.py           # 本地行情镜像同步
│   │
│   ├── factor_fusion.py               # ICIR 因子融合与截面 IC
│   ├── factor_library.py              # 实证 alpha 与选股权重
│   ├── factor_dynamic_weights.py      # DRL 动态因子权重
│   ├── factor_gate.py                 # IC 门控、漂移与变点状态机
│   ├── factor_mine/                   # 遗传规划因子挖掘/筛选/OOS
│   │
│   ├── selector.py                    # 候选池过滤、打分与 Top-N
│   ├── target_weighting.py            # 目标权重归一化与加权
│   ├── drl_train.py                   # CVaR-PPO 与 FactorValueEnv
│   ├── explainable_rl.py              # DRL 可解释性分析
│   ├── logic_q.py                     # 神经符号化趋势/量价逻辑
│   ├── risk_first.py                  # Risk-First 决策层
│   ├── risk_factor_optimizer.py       # CVaR/回撤/波动率风险因子
│   ├── degradation.py                 # 策略退化防御
│   │
│   ├── paper_book.py                  # A 股规则纸面账户与撮合
│   ├── realtime_engine.py             # 盘中模拟撮合引擎
│   ├── backtest_engine.py             # 连续交易日回放
│   ├── vnpy_backtest.py               # vn.py 回测适配
│   ├── run_daily.py                   # 日频主流程
│   ├── daemon.py                      # 交易日守护与自动拉起
│   ├── run_services.py                # 盘中服务启动/停止/状态
│   ├── dashboard.py                   # Web 看板
│   ├── premarket_healthcheck.py       # 盘前健康检查
│   ├── trading_calendar.py            # A 股交易日历与节假日判断
│   └── metrics_server.py              # Prometheus 指标服务
│
├── tests/                             # 单元、集成和 DRL 回归测试
│   ├── test_factor_gate.py            # IC 门控
│   ├── test_factor_health.py          # 因子健康隔离
│   ├── test_pit_valuation.py          # PIT 估值与数据源回退
│   ├── test_risk_first.py             # Risk-First 风控
│   ├── test_execution_decomposition.py# 执行成本与撮合
│   └── …                              # 其余策略、因子、回测测试
│
├── scripts/                           # 数据补录、诊断、回测和验证
│   ├── backfill_pe_ttm.py             # PE(TTM) PIT 补丁与双源回退
│   ├── backfill_valuation.py          # 估值快照补录
│   ├── check_data_completeness.py     # 数据完整性巡检
│   ├── nonoverlap_rerun.py            # 非重叠窗口样本外重跑
│   ├── diag_data.py                   # 数据诊断
│   └── validate_h5i_migration.py      # h5i 迁移校验
│
├── ops/                               # 观测、告警与服务配置
│   ├── prometheus.yml                 # Prometheus 抓取配置
│   ├── alertmanager.yml               # 告警路由
│   ├── alert_rules.yml                # 策略/数据告警规则
│   ├── astock_db_dashboard.json       # Grafana 数据库看板
│   └── start_obs_stack.ps1            # 观测栈启动脚本
│
├── docs/                              # 项目文档
│   ├── architecture.md                # 系统架构与数据流
│   ├── deployment.md                  # 环境和部署指南
│   ├── pit-valuation.md               # PIT 估值与样本外验证台账
│   ├── units.md                       # 收益/回撤单位约定
│   ├── symbols.md                     # 标的代码形态约定
│   ├── perf-plan.md                   # 回测性能改进计划
│   ├── pbo-cscv.md                    # PBO/CSCV 口径与解读
│   └── api_reference.md               # 核心接口说明
│
├── data/                              # 本地行情、状态和运行产物（不提交 Git）
├── logs/                              # 日志、净值和绩效报告（不提交 Git）
├── .env.example                       # 环境变量模板
├── .gitignore                         # 数据、密钥、模型与缓存忽略规则
├── conftest.py                        # 测试路径初始化
├── requirements_314.txt               # 核心依赖
├── requirements-dev.txt               # 测试、DRL 与观测栈依赖
├── requirements-lock.txt              # 锁定依赖清单
├── LICENSE                            # MIT 许可证
└── README.md                          # 项目说明
```

源码文件实际位于扁平的 `src/` 下；上面的数据层、因子层、决策层、风控层、评估层和运行层是逻辑分层，不代表物理目录。统一运行入口为 `python src/<脚本>.py`，测试通过根目录 `conftest.py` 注入 `src/`。

## 环境要求

- Windows/Linux 均可用于轻量研究和测试。
- 推荐 Python 3.14 虚拟环境用于核心回归、DRL 和纯 Python 工具。
- 部分本地 `h5i_db` 数据库组件使用 CPython 3.10 原生扩展；运行依赖该组件的数据脚本时，必须使用与 `_native.pyd` 匹配的 Python 3.10 环境。
- 数据库、行情镜像、API token 和运行产物不随仓库提供。

### 创建测试/DRL 环境

```powershell
cd A_stock_rotation
py -3.14 -m venv .venv314
.venv314\Scripts\python.exe -m pip install --upgrade pip
.venv314\Scripts\python.exe -m pip install -r requirements-dev.txt
```

如果只需要不含 DRL 的基础逻辑，可安装核心依赖并额外安装 pytest：

```powershell
.venv314\Scripts\python.exe -m pip install -r requirements_314.txt pytest
```

CPU 环境安装 PyTorch 时，可按本机平台参考 PyTorch 官方 wheel 源，再安装 `stable-baselines3` 和 `gymnasium`。

## 配置

复制环境变量模板并按实际数据位置填写：

```powershell
Copy-Item .env.example .env
```

常用配置项：

| 变量 | 说明 |
| --- | --- |
| `STOCKDB_ROOT` | 本地行情镜像或 free-stockdb 根目录 |
| `ARCTIC_URI` | ArcticDB 地址；未配置时使用可用的本地后端 |
| `PYBAO_DIR` | 可选的本地行情 SDK 路径 |
| `TRAE_PYTHON` | 守护任务调用的外部 Python 解释器 |
| `OPENAI_BASE_URL` | 可选的 LLM 服务地址 |
| `OPENAI_API_KEY` | LLM API 密钥，只通过环境变量提供 |
| `OPENAI_MODEL` | LLM 模型名 |
| `FACTOR_HEALTH_ENABLED` | 因子健康隔离开关，默认开启 |
| `OVERFIT_RESULTS_FILE` | 覆盖过拟合检测使用的结果文件 |

不要把 `.env`、token、数据库、行情文件、日志或模型权重提交到 Git。仓库的 `.gitignore` 已覆盖常见敏感配置和运行产物，但提交前仍应检查 `git status`。

## 常用命令

以下命令默认在仓库根目录执行。

### 测试

```powershell
# 完整回归
.venv314\Scripts\python.exe -m pytest tests -q

# PIT 估值与数据回退专项
.venv314\Scripts\python.exe -m pytest tests/test_pit_valuation.py -q

# 风控与因子门控专项
.venv314\Scripts\python.exe -m pytest tests/test_factor_gate.py tests/test_factor_health.py -q
```

CI 分为两部分：

- `regression-core`：不依赖 GPU 的核心逻辑全量回归，运行于 Python 3.11/3.12；
- `regression-drl`：包含 PyTorch、Stable-Baselines3 和 Gymnasium 的 DRL 回归。

### 日频、回测和过拟合检测

```powershell
# 刷新 IC 与门控缓存
.venv314\Scripts\python.exe src/refresh_gate_ic.py

# 日频主流程
.venv314\Scripts\python.exe src/run_daily.py

# 连续交易日 PaperBook 回放
.venv314\Scripts\python.exe src/backtest_engine.py --days 10

# 指定起始日期回放
.venv314\Scripts\python.exe src/backtest_engine.py --start 2026-06-01

# 门控历史重放
.venv314\Scripts\python.exe src/backtest_with_gate.py

# 非重叠样本外重跑
.venv314\Scripts\python.exe scripts/nonoverlap_rerun.py

# 过拟合/稳健性检测
.venv314\Scripts\python.exe src/overfitting_test.py --html

# 数据完整性巡检
.venv314\Scripts\python.exe scripts/check_data_completeness.py
```

### 盘中模拟与看板

```powershell
# 单次模拟 tick，不启动常驻循环
.venv314\Scripts\python.exe src/realtime_engine.py --once

# 启动盘中 PaperBook 引擎和 Web 看板
.venv314\Scripts\python.exe src/run_services.py start

# 查看服务状态
.venv314\Scripts\python.exe src/run_services.py status

# 停止服务
.venv314\Scripts\python.exe src/run_services.py stop

# 直接启动看板
.venv314\Scripts\python.exe src/dashboard.py --port 8000
```

浏览器访问 `http://localhost:8000`。看板通常读取 `data/live_state.json`、`data/state.json` 和每日运行产物。

### 一键启动后台栈

```powershell
powershell -ExecutionPolicy Bypass -File ops/start_obs_stack.ps1
```

幂等（已在跑的只报 `OK`，重复执行不会起第二个），一次拉起全部常驻服务并做端口自检：

| 端口 | 服务 | 说明 |
|---|---|---|
| 8000 | Web 看板 | 选股结果 / 模拟盘 / 数据更新入口 |
| 3000 | Grafana | 数据库看板（admin/admin） |
| 9090 | Prometheus | 指标库，抓取 `astock-db`(9101) |
| 9093 | Alertmanager | 告警路由 → 本地 webhook |
| 9111 | alert_hook | 告警落盘 `logs/alerts.log`（设 `DING_WEBHOOK_URL` 则转发钉钉） |
| 9101 | metrics_server | `/metrics` 暴露端 |
| 6379 | Redis | Celery broker/backend（看板“全量数据库更新”依赖） |
| — | Celery worker | 全量数据更新任务（solo pool） |
| — | sentinel daemon | 估值覆盖率哨兵，每日 18:30（见 `docs/patch-retirement-watch.md`） |

各常驻服务日志在 `logs/`：`dashboard.log`、`sentinel_daemon.log`、`alert_hook.log`、`alerts.log`。

## 数据与 Point-in-Time 口径

历史选股和回测必须区分“当日可见数据”和“当前最新数据”。本项目的 PIT 路径包括：

- `db._universe_asof_h5i(as_of)`：按历史日期合并日线与估值；
- `db._pe_patch_asof(as_of)`：读取 PE/free_cap 补丁，并严格过滤 `ts <= as_of`；
- `filter_universe(...)`：执行 A 股代码段、ST、次新、流通市值和流动性过滤；
- `scripts/nonoverlap_rerun.py`：使用非重叠窗口检查样本外稳定性。

详细口径见 [`docs/pit-valuation.md`](docs/pit-valuation.md)、[`docs/units.md`](docs/units.md) 和 [`docs/symbols.md`](docs/symbols.md)。

当前仍需注意：

1. 历史 ST 名称数据并不完整；PIT `is_st` 覆盖不足时，未知值会保留而不是强行剔除。
2. 某些早期区间的总市值缺失，会使用流通股本 × 价格进行近似补齐。
3. 如果回测使用固定的当前股票池，只能验证撮合链路，不能代表完整历史选股能力。
4. 任何回测结果都必须同时报告数据窗口、股票池、交易成本、滑点、换手率和最大回撤。

## 风控和执行边界

PaperBook/盘中模拟默认实现以下 A 股规则：

- 只做多、不使用杠杆；
- T+1，当日买入股份不可当日卖出；
- 买入和卖出按 100 股整手处理；
- 涨停不买、跌停不卖；停牌或无有效价格时跳过；
- 支持佣金最低收费、卖出印花税、过户费和滑点；
- 支持单股止损、组合回撤控制、换手预算和 IC 门控；
- 目标权重计算失败时记录 `degraded` 状态，明确使用的降级策略；
- 交易日历不可用时，盘中路径保守拒绝交易。

当前 `realtime_engine.py` 的交易通道固定为 PaperBook。任何非 `paper` 的交易通道配置都会拒绝下单，不会静默切换到真实交易。

## 结果解读

本仓库包含历史实验报告和模拟运行产物，但它们主要用于诊断和回归，不能直接作为策略收益承诺。评估时建议重点关注：

- 样本外年化收益、Sharpe、Calmar 与最大回撤；
- 交易成本和换手率对结果的影响；
- 不同市场状态下的表现，而不是只看单一窗口；
- 基线、门控、DRL 和风险增强版本之间的增量；
- 回测中是否存在未来数据、幸存者偏差、固定股票池或不现实成交假设。

推荐先完成 PIT 数据补齐和非重叠样本外验证，再评估任何资金可用性结论。

## 文档索引

- [`docs/architecture.md`](docs/architecture.md)：系统分层和关键数据流；
- [`docs/deployment.md`](docs/deployment.md)：安装、环境变量和运行部署；
- [`docs/api_reference.md`](docs/api_reference.md)：主要模块和接口；
- [`docs/pit-valuation.md`](docs/pit-valuation.md)：PIT 估值与数据缺口台账；
- [`docs/pbo-cscv.md`](docs/pbo-cscv.md)：PBO/CSCV 口径与解读；
- [`docs/perf-plan.md`](docs/perf-plan.md)：回测性能改进计划；
- [`ops/`](ops/)：Prometheus、Grafana 和告警配置。

## 开发约定

1. 新增数据源时必须注明是否联网、数据时间口径和失败降级行为。
2. 涉及历史数据的逻辑必须明确 `as_of`，并增加无前视测试。
3. 涉及风控、执行或数据补丁的修复必须添加回归用例。
4. 修改运行入口后同步更新 README、部署文档和 CI。
5. 提交前执行 `git diff --check` 和相关测试，并确认没有敏感文件进入暂存区。

## 许可证

本项目采用 [MIT License](LICENSE)。
