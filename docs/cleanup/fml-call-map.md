# `ml_fusion_bridge` 调用图

## 扫描结果

排除 `_archive/`、`data/`、`logs/` 后，关键词命中 129 条。复核命令：

```powershell
rg -n "ml_fusion_bridge|FML_WEIGHT|_blend_fml|FORCE_FML|compute_fml|fusion_or_fml" `
  --hidden --glob '!.git/**' --glob '!_archive/**' --glob '!data/**' --glob '!logs/**' .
```

## 调用关系库存

| 调用方 | 被调用/依赖 | 入口性质 | 当前判断 |
| --- | --- | --- | --- |
| `src/selector.py` | `FML_WEIGHT`、`_blend_fml` | 生产选股路径 | 活跃，不能直接删除 |
| `src/factor_fusion.py` | `FORCE_FML`、`fusion_or_fml`、fallback | 因子融合主路径 | 活跃，需明确 fallback 语义 |
| `src/ml_fusion_bridge.py` | `compute_fml`、权重/外部模型桥接 | 兼容实现 | 活跃或历史回退，需运行确认 |
| `src/target_weighting.py` | fml fallback 标签与来源 | 目标权重路径 | 活跃，删除桥接前必须重写来源标记 |
| `src/fml_accumulate.py` | bridge 计算 | 研究/累积入口 | 需区分生产与研究调用 |
| `src/fusion_daily_validate.py` | 多种 fallback 场景 | 验证入口 | 测试契约，不能误删 |
| `tests/test_fusion_degrade.py`、`tests/test_weighting_and_fusion.py` | fallback/强制分支回归 | 非生产 | 与生产代码同步调整 |

## 判定

`ml_fusion_bridge` 仍有生产选股和权重链引用，不能按“旧接口”直接删除。后续应先确定 `factor_fusion` 是否能完全替代 bridge，再决定保留、降级为显式 legacy adapter，或删除对应测试和配置。
