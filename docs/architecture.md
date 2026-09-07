# 系统架构设计

## 分层视图

系统按"数据 → 因子 → 决策 → 风控 → 执行/评估"组织,源码当前为扁平演进布局,下表给出逻辑归属(与 README 结构树一致)。

```text
[数据层]  h5i/ArcticDB/free-stockdb  -> 因子宽表(daily_bars/valuation/financials)
   │
[因子层]  基本面/反转因子 + 融合(ICIR) + GP挖掘 + 逐日截面 IC 曲线
   │
[决策层]  selector(候选过滤+打分+Top-N) / DRL 动态权重(CVaR-PPO)
   │
[风控层]  factor_gate(IC门控 + 单日亏损 + IC漂移 + Sharpe变点 + 因子健康隔离)
   │      risk_first(熔断) / degradation(退化防御)
   │
[执行层]  paper_book(纸面撮合,T+1/涨停不买/跌停不卖) + realtime_engine
   │
[评估层]  performance_report(归因) + overfitting_test(稳健性) + backtest_with_gate
```

## 关键链路

1. **日频选股** `run_daily.py` → `selector.py`:候选池过滤(市值/换手/ST/次新)→ 因子打分(signal/趋势/治理/流动性 + 实证 alpha vol、mom_rev + 基本面 pb_rev/roe/mf_net)→ 融合分/DRL 权重加权 → Top-N。
2. **因子融合与健康** `factor_fusion.py`(ICIR 加权)→ `factor_gate.factor_health_flags` 依据近 121/20 日 IC 判定反转义因子是否方向翻转/收敛,失效因子在 `selector_weights` 中被隔离(权重 0)。
3. **IC 门控** `factor_gate.compute_plan`:risk 进入需 mean<‑0.005 且负占比>0.55(且 ICIR<‑0.5)连续 3 日;单日亏损 ≤‑2% 冻结新买;个体因子漂移 ≥2/3 不稳增强门控;Sharpe 变点(最近 5 跳变窗口)>阈值进 caution、≥严重阈值跳级 risk 并保持 3 日。
4. **DRL** `drl_train.py`:CVaR-PPO 在 FactorValueEnv 上生成 signal/trend/govern/liquidity/vol/mom_rev 六维权重;signal 权重硬边界 `[0.10, 0.39]`,约束后其余权重重新归一化。
5. **评估闭环** `overfitting_test.py`(7 维度)→ 输出 JSON/HTML;`backtest_with_gate.py` 用同一 `factor_gate` 逻辑做无前视连续重放,验证门控对回撤的收窄(当前基线→门控最大回撤 8.23%→7.69%)。

## 源码布局与演进(2026-09-07 已完成 src/ 迁移)

- 全部 Python 源码已迁移至 `src/`(单层平铺,扁平 import 不变;`factor_mine/` 位于 `src/factor_mine/`)。
- 运行入口:统一 `python src/<脚本>.py`;`src/daemon.py`/`run_daily.py`/`realtime_engine.py` 等互以 `src/` 内路径 spawn。
- `data/`、`logs/` 位于仓库根,代码经 `config.BASE`(仓库根)定位;`tests/` 依赖根 `conftest.py` 把 `src/` 注入 `sys.path`。
- 若未来需把 `src/` 拆为子层(包路径),步骤:1) 停止守护/非交易时段执行;2) 同步改全部 `from X import …` 为包引用或每层注入 `sys.path`;3) 全量回归后重启守护。
