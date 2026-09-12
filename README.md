# A_STOCK — A股量化轮动系统 (Quant-LLM-DRL)

A 股量化研究/模拟盘系统:全市场日频因子选股、盘中模拟撮合、因子 IC 门控、CVaR-PPO 动态权重、遗传规划因子挖掘与过拟合检测套件。

> 研究/回测用途,非投资建议。仓库仅含核心代码、配置模板与文档;不含行情数据、数据库文件、交易记录或 API 密钥。

## 项目结构

```text
quant-llm-drl-system/
├── .github/                          # GitHub 工作流配置
│   └── workflows/
│       └── ci.yml                    # 持续集成 (核心逻辑回归)
│
├── src/                              # 核心源代码 (逻辑分层)
│   ├── data/                         # 数据层
│   │   ├── arctic_store.py           # ArcticDB 读写客户端 (bars/回执/IC/reward)
│   │   ├── h5i_bar_store.py          # h5i-db 行情/财务/估值查询引擎
│   │   ├── db.py                     # 统一数据访问 (h5i 优先, 退役 duckdb 兼容)
│   │   ├── build_factor_views.py     # 因子宽表构建 (mom20/vol/mom_rev/治理/流动性)
│   │   └── free_stockdb_sync.py      # 本地行情镜像同步与增量更新
│   │
│   ├── factor/                       # 因子研究层
│   │   ├── factor_fusion.py          # 融合因子 (ICIR 加权, 含 IC 门控联动)
│   │   ├── factor_dynamic_weights.py # DRL 动态因子权重分配
│   │   ├── factor_library.py         # 实证 alpha 打分与旧版权重 API
│   │   └── factor_mine/              # 遗传规划(GP)因子挖掘/筛选/评估
│   │
│   ├── strategy/                     # 智能决策层
│   │   ├── drl_train.py              # CVaR-PPO 核心算法 + FactorValueEnv
│   │   ├── explainable_rl.py         # DRL 可解释性
│   │   ├── logic_q.py                # 神经符号化趋势/量价逻辑 (NeSy-TA)
│   │   ├── risk_first.py             # Risk-First 风控优先决策
│   │   └── selector.py               # 候选池选股打分 (等权/加权)
│   │
│   ├── risk/                         # 风险控制层
│   │   ├── factor_gate.py            # IC 门控 (联合判据 + 滞后状态机 + 变点检测
│   │   │                             #   + 因子健康处置 factor_health_flags)
│   │   ├── risk_factor_optimizer.py  # CVaR/回撤/波动率风险因子
│   │   └── degradation.py            # 策略退化防御
│   │
│   ├── validation/                   # 评估与归因
│   │   ├── overfitting_test.py       # 过拟合与稳健性检测套件 (7 维度)
│   │   ├── strategy_validation.py    # 策略指标达标检测
│   │   ├── performance_report.py     # 绩效归因与 IC 监控
│   │   └── attribution_analysis.py   # 收益/因子归因
│   │
│   └── engine/                       # 运行引擎
│       ├── run_daily.py              # 日频任务调度
│       ├── realtime_engine.py        # 盘中模拟撮合引擎
│       ├── daemon.py                 # 交易日守护 (调度/崩溃拉起)
│       ├── dashboard.py              # Web 可视化面板
│       └── premarket_healthcheck.py  # 盘前健康检查
│
├── tests/                            # 测试套件
│   ├── test_factor_gate.py           # 门控单元测试
│   ├── test_factor_health.py         # 因子健康处置测试
│   ├── test_cvar_config.py           # CVaR 配置测试
│   └── …                             # (共 20+ 测试文件)
│
├── scripts/                          # 运维与一次性工具
│   ├── nonoverlap_rerun.py           # 非重叠窗口样本重建
│   ├── pbo_sweep.py                  # PBO 参数扫描 + CSCV 估计
│   ├── backtest_with_gate.py         # 门控历史重放 (含变点/漂移上下文)
│   └── rerun_vnpy_all.py             # 滚动窗口样本重建
│
├── docs/                             # 文档
│   ├── architecture.md               # 系统架构设计
│   ├── deployment.md                 # 部署指南
│   ├── pit-valuation.md              # PIT 估值与样本外验证台账
│   ├── pbo-cscv.md                   # PBO(CSCV) 口径、实现与解读
│   └── api_reference.md              # 核心接口说明
│
├── .env.example                      # 环境变量模板
├── .gitignore                        # Git 忽略规则
├── LICENSE                           # MIT 许可证
├── README.md                         # 项目说明
└── requirements_314.txt              # Python 3.14 核心依赖清单
```

> 说明:本仓库源码已**物理迁移到 `src/`**(单层平铺,扁平 import 保持,便于守护与工具直接引用);上表将 `src/` 内文件按逻辑层归入 `data/factor/strategy/risk/validation/engine` 便于理解,文件一一对应真实路径。运行入口统一为 `python src/<脚本>.py`;`tests/` 通过根 `conftest.py` 注入 `src/`。如需进一步把 `src/` 拆成子层(包路径 import),需同步改全量相对引用,见 `docs/architecture.md`。

## 关键模块说明

| 模块 | 职责 |
|---|---|
| `config.py` | 全系统配置:资金规则/候选池过滤/因子开关/风控与 DRL 参数(均可用环境变量覆盖) |
| `factor_gate.py` | 组合风控核心:融合 IC 联合判据进入/退出 risk,3 日滞后;单日亏损防御;个体因子 IC 漂移监控(方向有效性语义:反转义因子"负 IC 加深=更有效");Sharpe 变点检测 + 变点后保持 |
| `factor_health_flags` → `selector_weights` | 因子健康处置:方向翻转/强度收敛(反转义失效)的因子自动从打分权重隔离(置 0);`FACTOR_HEALTH_ENABLED=0` 关闭 |
| `factor_fusion.py` | 四因子 ICIR 加权融合打分(pb_inv/ep/ocf_ps/roe_yy_chg 反转),行业+市值中性 |
| `drl_train.py` | CVaR-PPO 训练六维因子权重(signal 权重硬边界 `[0.10,0.39]`),奖励含尾部风险 |
| `selector.py` | 全 A 候选过滤 → 因子打分 → Top-N;signal/趋势/治理/流动性 + 实证 alpha(低波/超跌反转) |
| `overfitting_test.py` | 7 维度过拟合检测:CPCV / PBO / 置换 / 市场状态依赖 / 参数稳定性 / 健康度(IC 稳定性 121 日口径)/ 回撤换手诊断;`OVERFIT_RESULTS_FILE` 可切换样本源 |

## 重要说明

- **数据与敏感信息**:本仓库仅包含核心代码、配置模板和文档,不包含任何原始行情数据、数据库文件、交易记录或 API 密钥。请自行准备数据源(推荐 free-stockdb 或 CNEquity)。本地数据根通过环境变量注入,不硬编码个人路径。
- **依赖项**:核心依赖包括 numpy、pandas、scipy、scikit-learn、h5py、pyarrow、matplotlib 等;DRL 链路另需 torch / stable-baselines3 / gymnasium(重型,按需安装)。详见 [`requirements_314.txt`](requirements_314.txt)。测试与 CI 的轻量子集仅需 numpy/pandas/scipy/scikit-learn/pytest。
- **环境变量**:`STOCKDB_ROOT`(本地行情镜像根)、`ARCTIC_URI`(ArcticDB 地址)、`PYBAO_DIR`(本地行情 SDK,可选)、`TRAE_PYTHON`(守护进程外部解释器,可选)、`OPENAI_BASE_URL/OPENAI_API_KEY/OPENAI_MODEL`(LLM 增强,可选)。见 [`.env.example`](.env.example)。

## 快速开始

```bash
pip install -r requirements_314.txt
python -m pytest tests/ -q                       # 单元测试
python src/refresh_gate_ic.py                    # 刷新融合 IC 缓存
python src/rerun_vnpy_all.py                     # 滚动窗口回测
python src/backtest_with_gate.py                 # 门控重放对照
python src/overfitting_test.py --html            # 过拟合检测
python src/run_daily.py                          # 日频主流程
```

## 贡献指南

1. Fork 本仓库
2. 创建您的特性分支:`git checkout -b feature/AmazingFeature`
3. 提交您的更改:`git commit -m 'Add some AmazingFeature'`
4. 推送到分支:`git push origin feature/AmazingFeature`
5. 开启一个 Pull Request

## License

[MIT](LICENSE)
