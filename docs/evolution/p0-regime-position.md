# P0：市场状态路由与动态仓位

本阶段先落地两个无副作用、可审计的基础组件：

- `src/regime_detector.py`：基于收益序列识别 `bull`、`bear`、`sideways`、`high_vol`，并提供透明的因子倍率路由。
- `risk_first.DynamicPositionSizer`：按年化波动率、可选预期收益/Kelly 和市场状态计算总仓位缩放，并保留缩放后的现金。

## 语义边界

这两个组件目前是研究/影子模式，默认不改变既有选股、下单和回测路径。只有显式传入 `market_regime` 或调用 `apply_dynamic_position_size` 时才生效。

这样可以先用历史回放比较四种状态下的因子表现和组合曲线，再决定是否接入 `target_weighting` 与 `paper_book` 的生产路径。

## 防护约束

- 有效收益不足时返回 `unknown`，不把缺数据伪装成正常状态。
- 因子路由始终归一化，输入不被原地修改。
- 动态仓位只缩小目标权重，不重新归一化，因此缩放出的部分确实留在现金。
- 非正预期收益的 Kelly 缩放为 0，并在返回值中记录原因。
