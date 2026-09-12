# 数值单位约定（回撤 / 收益）

> 2026-09-12 统一。此前多处把"已是百分点"的字段再乘 100，导致
> `-0.694%` 被显示为 `-69.4%`、`-1.13%` 被显示为 `-113%`，
> 并在报告里被误读为"长周期最大回撤 79%~86%"（实际为 0.79%~1.13%）。
> 本文件为唯一口径依据，新增字段请遵守。

## 约定

1. **对外/落盘字段统一使用"百分点"(percent, %)**：
   - 字段名含 `_pct` 后缀，或明确位于绩效报告 metrics 中。
   - 例：`max_drawdown_pct: 0.86` 表示 **0.86%**；`total_return_pct: 0.26` 表示 **0.26%**。
   - 负数表示亏损/回撤：`max_drawdown: -1.13` 表示 **-1.13%**。
2. **内部计算函数可返回"比例"(ratio, 0..1)**，但必须在字段名或注释中显式声明，
   且**只允许在来源处换算一次**（×100 转百分点），下游不得再次换算。
3. 展示层（dashboard/报告）**不再做单位启发式推断**（如 `|x|>1 即视为 %`），
   一律按本表的来源单位处理。

## 各单位地图

| 来源 | 字段 | 单位 | 说明 |
|---|---|---|---|
| `performance_report.py` metrics | `total_return` / `annual_return` / `max_drawdown` / `daily_ret_std` | **百分点** | 代码内已 `*100` 后 `_r(...)` 落盘 |
| `backtest_engine.py`(BacktestRunner) | `max_drawdown_pct` / `total_return` | **百分点** | `_max_dd()` 内部 `*100` |
| `vnpy_backtest` stats | `max_ddpercent` | **百分点** | vnpy 原生口径 |
| `strategy_validation.curve_metrics` | `max_drawdown` / `cagr` | **比例(0..1)** | 阈值表 `THRESHOLDS["max_drawdown"].limit=0.30` 即 30% |
| `backtest_with_gate.simulate` → `gated/baseline` | `max_drawdown` | **比例(0..1)** | 透传 `curve_metrics`，消费端需 ×100 |
| `spc.py` 指标 `max_drawdown` | — | **百分点** | `unit="%"`, `lsl=-10.0` 即 -10% |
| `degradation.py` | `max_drawdown`(读 perf) | **百分点** | 与 spc 阈值同口径 |
| `premarket_healthcheck.py` `reflection_diag` / `bt_paper_parity` | `max_drawdown_pct` / `*_pct` | **百分点** | 2026-09-12 修正去掉了重复 ×100 |
| `dashboard.py` 深度分析「回测对照」 | 展示值 | **百分点** | 同上，已移除 `_toPct` 启发式 |
| `dashboard.py` 参数扫描（`/api/bt_scan`） | `mdd_pct` | **百分点** | 来源为 `curve_metrics` 比例，故 ×100 |

## 变更记录

- 2026-09-12：修正 `premarket_healthcheck.py`（reflection_diag 阈值 `-0.08/-0.10` → `-8.0/-10.0`、
  显示 `*100` 去除；bt_paper_parity 的 4 处 `*100` 去除）；
  修正 `dashboard.py` 深度分析回测对照的单位启发式（移除 `_toPct` 与 `m.max_drawdown*100`），
  并为参数扫描的 `*100` 增加来源注释。
