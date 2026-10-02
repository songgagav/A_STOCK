# `ml_fusion_bridge` 角色说明

记录日期：2026-10-02  
适用分支：`cleanup/phase0-baseline`

## 角色定位

`ml_fusion_bridge` 是历史 FML（模型融合）路径的兼容桥，不是可以凭文件名直接删除的死模块。生产默认路径已经以 `factor_fusion.fusion_or_fml` 为主，但桥接和 fallback 仍由配置、验证和研究入口引用。

## 当前边界

| 位置 | 责任 | 当前状态 |
| --- | --- | --- |
| `src/selector.py` | 使用 `FML_WEIGHT` / `_blend_fml` 参与选股融合 | 生产选股链，保留 |
| `src/factor_fusion.py` | `FORCE_FML`、`fusion_or_fml` 和 fallback 选择 | 主融合入口，保留并明确 fallback |
| `src/ml_fusion_bridge.py` | FML 计算、权重和外部模型桥接 | 兼容实现，保留 |
| `src/target_weighting.py` | 记录 FML fallback 来源和标签 | 目标权重链，保留 |
| `src/fml_accumulate.py` | FML 研究/累积计算 | 先区分研究和生产调用 |
| `src/fusion_daily_validate.py` | fallback 和降级场景验证 | 测试/验证契约，保留 |

## 运行约定

- 生产默认走 `factor_fusion.fusion_or_fml`；
- `FORCE_FML` 是显式兼容开关，开启时必须在运行记录中留下来源标记；
- fallback 不能伪装成主路径成功，必须可审计、可计数；
- 删除 bridge 前必须先证明所有生产调用已经迁移，并保留旧结果的回放能力。

## 退役条件

以下条件全部满足后，才重新评估退役：

1. `FORCE_FML` 没有生产、计划任务或外部脚本调用方；
2. `selector`、`target_weighting` 和验证入口已使用同一主融合实现；
3. fallback 和降级测试已迁移到新路径；
4. 历史回测和同日回放结果有对照证据；
5. 文档、配置模板和回滚步骤已更新。

本阶段只做角色标注，不删除 bridge，不改变 FML 权重或方向。
