# A_stock_OS

A 股量化选股与风控研究系统:全市场日频因子选股、盘中模拟撮合、因子 IC 门控、DRL 动态权重与过拟合检测套件。

> 研究/回测用途,非投资建议。代码与运行产物分离,默认不提交本地行情数据与运行状态。

## 特性

- 全 A 轮动:候选池过滤(市值/换手/ST/次新)→ 因子融合打分 → Top-N 等权/加权持仓,只做多、T+1
- 因子融合:基本面/反转因子 ICIR 加权融合(`factor_fusion.py`),可叠加 GP 挖掘(`factor_mine/`)
- IC 门控(`factor_gate.py`):融合 IC 联合判据 + 风险滞后进入/退出,单日亏损防御,个体因子 IC 漂移监控,Sharpe 变点检测
- DRL 动态权重(`factor_dynamic_weights.py` / `drl_train.py`):CVaR-PPO 生成六维因子权重,signal 权重带硬边界约束
- 过拟合检测(`overfitting_test.py`):CPCV / PBO / 置换检验 / 市场状态依赖 / 参数稳定性 / 策略健康度
- 盘中引擎与看板:实时撮合(`realtime_engine.py`)、绩效归因(`performance_report.py`)、Web 面板(`dashboard.py`)

## 模块地图

| 层 | 模块 |
|---|---|
| 配置 | `config.py` |
| 数据存储 | `db.py` / `h5i_bar_store.py` / `arctic_store.py` / `build_factor_views.py` |
| 因子计算/融合 | `factor_library.py` / `factor_fusion.py` / `factor_dynamic_weights.py` / `factor_gate.py` |
| 选股/交易 | `selector.py` / `target_weighting.py` / `paper_book.py` / `realtime_engine.py` / `slippage_model.py` |
| DRL | `drl_train.py` / `explainable_rl.py` / `multimodal_ensemble.py` / `risk_first.py` |
| 回测/验证 | `vnpy_backtest.py` / `rerun_vnpy_all.py` / `backtest_with_gate.py` / `backtest_audit.py` |
| 评估 | `overfitting_test.py` / `performance_report.py` / `strategy_validation.py` / `attribution_analysis.py` |
| 因子挖掘 | `factor_mine/`(GP 挖掘/筛选/评估,`factor_mine/cli.py` 入口) |
| 服务 | `run_daily.py` / `gate_refresh_daemon.py` / `daemon.py` / `premarket_healthcheck.py` / `dashboard.py` |
| 测试 | `tests/`(pytest) |

> 说明:系统为长期演进的扁平结构(未强制 `src/` 分层),上表给出模块归属;如需重组目录,应同步核对各文件顶部 `sys.path` 与 `from config import …` 的相对引用。

## 环境与安装

- Python 3.14(核心依赖见 [`requirements_314.txt`](requirements_314.txt):numpy / pandas / scipy / scikit-learn / h5py / pyarrow 等)
- 依赖与数据:需自行准备本地行情/财务数据,并可选启用 h5i 数据湖或 ArcticDB 缓存

```bash
pip install -r requirements_314.txt
python -m pytest tests/ -q        # 运行单元测试
```

## 数据与路径约定

- 所有本地数据、运行状态、日志均在 `data/` 与 `logs/` 下,**默认不纳入版本控制**(见 `.gitignore`)
- 可移植路径通过环境变量注入(不硬编码个人目录):

| 环境变量 | 用途 |
|---|---|
| `STOCKDB_ROOT` | 本地行情镜像根(parquet/旧数据湖) |
| `ARCTIC_URI` | ArcticDB LMDB 地址,如 `lmdb://<path>/arcticdb` |
| `PYBAO_DIR` | 本地行情引擎 SDK 目录(可选) |
| `TRAE_PYTHON` | 守护进程使用的外部 Python 解释器(可选) |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | LLM 增强组件(可选) |

复制 [`.env.example`](.env.example) 并按需填写(本项目代码读取真实环境变量;`.env` 需由启动器自行加载)。

## 快速体验

1. 配置数据源并完成一次数据入库(因子视图:见 `build_factor_views.py`)
2. 生成融合因子 IC 缓存:`python refresh_gate_ic.py`
3. 回测主链路:`python rerun_vnpy_all.py`(写入 `data/vnpy_backtest_rerun_results.json`)
4. 门控重放对照:`python backtest_with_gate.py`
5. 过拟合与稳健性检测:`python overfitting_test.py --html`
6. 日频主流程:`python run_daily.py`(选股→纸面撮合→回执)

## 测试

```bash
python -m pytest tests/ -q
```

## 目录约定

```text
A_stock_rotation/
├── config.py / *_engine.py / selector.py …   # 核心源码(平铺)
├── factor_mine/                              # 因子挖掘(GP)
├── scripts/                                  # 数据/迁移/诊断脚本
├── tests/                                    # 单元与集成测试
├── data/                                     # 运行时数据(不入库)
├── logs/                                     # 运行日志(不入库)
├── .env.example                              # 环境变量模板
├── requirements_314.txt
└── .gitignore
```

## License

[MIT](LICENSE)
