# P0：市场状态路由与动态仓位

本阶段先落地两个无副作用、可审计的基础组件：

- `src/regime_detector.py`：基于收益序列识别 `bull`、`bear`、`sideways`、`high_vol`，并提供透明的因子倍率路由。
- `risk_first.DynamicPositionSizer`：按年化波动率、可选预期收益/Kelly 和市场状态计算总仓位缩放，并保留缩放后的现金。

## 语义边界

这两个组件目前是研究/影子模式，默认不改变既有选股、下单和回测路径。只有显式传入 `market_regime` 或调用 `apply_dynamic_position_size` 时才生效。

这样可以先用历史回放比较四种状态下的因子表现和组合曲线，再决定是否接入 `target_weighting` 与 `paper_book` 的生产路径。

## 离线回放入口

`scripts/research/regime_route_report.py` 将市场广度、PIT 因子原值、5 日前瞻收益和状态路由放进同一份研究报告。默认使用 20 个交易日，输出到被 `.gitignore` 忽略的 `data/evolution/regime_route_report.json`：

```powershell
.venv310\Scripts\python.exe scripts\research\regime_route_report.py `
  --end 2026-09-04 --days 20
```

报告目前直接评估 `ep`、`pb_inv`、`roe_yy_chg`、`rev_yoy`；`ocf_ps` 与 `gp4` 只展示已有路由先验，因为 `factor_library.compute_factors` 尚未提供这两个因子的同口径原始回放。缺少 `h5i_db` 时脚本会明确失败，不会写出空报告。该报告不改变生产默认路径，只有在历史结果审阅后才考虑下一步接入。

### 首次回放结果（截至 2026-09-04）

20 个交易日回放成功完成，4 个评估因子每日均有结果，错误数为 0。由于当前市场广度数据从 2026-08-03 起才有足够历史，状态分布为：`unknown` 14 天（预热期）、`bull` 5 天、`sideways` 1 天，尚未形成可用于生产决策的 `bear/high_vol` 样本。

已知的 5 个 `bull` 日中，融合分 5 日 RankIC 均值为 `+0.03598`；`pb_inv` 为 `+0.14525`、`ep` 为 `+0.07941`，而 `rev_yoy` 为 `-0.11215`、`roe_yy_chg` 为 `-0.06297`。这只是小样本影子证据，不能据此自动启用生产路由；下一步应先补足更长的市场状态历史，再做状态条件下的 OOS 复核。

## 防护约束

- 有效收益不足时返回 `unknown`，不把缺数据伪装成正常状态。
- 因子路由始终归一化，输入不被原地修改。
- 动态仓位只缩小目标权重，不重新归一化，因此缩放出的部分确实留在现金。
- 非正预期收益的 Kelly 缩放为 0，并在返回值中记录原因。
