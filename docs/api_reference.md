# API 参考

核心入口与调用方式。代码布局:`src/` 存放全部源码模块,`scripts/` 存放运维脚本,
`tests/` 存放 pytest 用例;运行目录为**仓库根**(路径均以仓库根为基准,
`config.BASE` 即仓库根),因此模块导入/调用示例统一写作 `src/<module>.py`
或经 `conftest.py` 注入 `src/` 后的裸模块名。

## 配置 `config`

- 常量:资金(`INIT_CAPITAL`/`MAX_STOCKS`)、候选过滤(`POOL_FILTER`)、信号(`SIGNAL_PARAMS`)、DRL/风控参数字典。
- 大部分可用同名环境变量覆盖(见各字段注释)。

## 数据访问

- `db.py`: `StockDB` 统一入口;h5i 优先,退役 duckdb 自动降级。
- `h5i_bar_store.py`: 全 A 日频/财务/估值读取;`duck_available()` 探测。
- `arctic_store.py`: `get_store()`/`ArcticStore`;库:`bars`/`trade_records`/`daily_summary`/`perf_report`/`factor_ic`/`reward_curve`;append-only。

## 因子与选股

- `factor_library.score_factor(name, bars)`: 实证 alpha 打分(`vol`/`mom`),0..1。
- `factor_library.selector_weights()`: 打分权重(经因子健康隔离);`FACTOR_HEALTH_ENABLED=0` 关闭。
- `factor_fusion.py`: 融合打分与截面 IC 计算(参考 `build_plan`/`compute_fusion_score`)。
- `selector.py`: `RotationSelector.select(...)` 输出 Top-N + 归因(thesis)。

## IC 门控 `factor_gate`

- `compute_plan(ic_mean, ic_neg_share, daily_loss, …)`: 门控决策(regime/exposure/freeze/interval)。
- `build_plan_from_cache(...)`: 引擎入口(读融合 IC 缓存;支持 `factor_ics`/`sharpe_history` 上下文)。
- `check_factor_ic_drift(factor_ics)`: 方向有效性 IC 稳定性(flipped/weakened)。
- `factor_health_flags(factor_ics=None)`: 失效因子隔离建议。
- `detect_sharpe_change_point(sharpes)`: 最近窗口变点检测。
- 参数:代码默认值 < `data/factor_gate_config.json` < 环境变量 `FG_*`(若文件不存在自动用默认)。

## DRL

- `drl_train.py`: `CVaR_PPO`、`FactorValueEnv`;`drl_factor_mode`(config)选路;权重输出带 signal 硬边界。

## 回测 / 检测

- `vnpy_backtest.run_vnpy_backtest(day, top_n, lookback_days)` → stats+curve。
- `backtest_with_gate.simulate(ds, ...)` → baseline/gated 指标与 per_day;`use_change_detection`/`use_factor_drift` 默认开启。
- `overfitting_test.py` CLI:`--list/--only/--html`;样本源可用 `OVERFIT_RESULTS_FILE` 覆盖。
- `scripts/nonoverlap_rerun.py`: 非重叠窗口回测样本。

## 运行

- `daemon.py`: `--status` 查看状态,`--stop` 停止(重启 = 直接 `python src/daemon.py`)。
- `realtime_engine.py`: `--once/--interval N/--no-intraday`。
- `dashboard.py`: `--port 8000`,轮询 `data/live_state.json`。

## 测试

```bash
python -m pytest tests/ -q                        # 全量
python -m pytest tests/test_factor_gate.py tests/test_factor_health.py -q
```
