# IC 链调用图

## 扫描结果

排除 `_archive/`、`data/`、`logs/` 后，关键词命中 336 条。复核命令：

```powershell
rg -n "ic_track|ic_backtest|ic_curve_refresh|weight_optimizer|ic_history|ICIR|Spearman" `
  --hidden --glob '!.git/**' --glob '!_archive/**' --glob '!data/**' --glob '!logs/**' .
```

## 调用关系库存

| 模块 | 作用 | 入口性质 | 当前判断 |
| --- | --- | --- | --- |
| `src/ic_track.py` | IC/ICIR 跟踪与历史记录 | 研究和健康监控 | 需确认输出消费者 |
| `src/ic_backtest.py` | IC 回测 | 研究入口 | 若无自动消费者，可归档但不能先删 |
| `src/ic_curve_refresh.py` | IC 曲线刷新 | 调度/刷新入口 | 需检查 run_daily、dashboard 或报表调用 |
| `src/weight_optimizer.py` | 权重优化 | 生产/研究候选入口 | 需确认是否被 factor gate 或训练链读取 |
| `src/factor_fusion.py` | ICIR/截面 IC 融合 | 核心生产因子路径 | 活跃，不能按研究脚本处理 |
| `src/run_daily.py`、`src/premarket_healthcheck.py` | 日频更新/健康检查 | 生产调度入口 | 重点检查是否消费 IC 状态 |
| `src/factor_mine/**`、`src/drl_train.py` | 因子研究/训练 | 研究/训练入口 | 与生产 IC 口径需分开记录 |

## 判定

当前扫描只能证明 IC 组件之间存在大量名称和实现引用，不能直接判定某个模块无消费者。下一步必须沿 `run_daily`、factor gate、fusion、dashboard 和训练脚本追踪输出文件/状态字段，再决定归档。
