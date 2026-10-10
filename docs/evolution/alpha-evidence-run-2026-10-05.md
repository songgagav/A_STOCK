# Alpha 证据运行记录：2026-10-05

## 结论

本次运行结果为 `not_promotable`，不能据此调整因子权重、启用新信号或改变交易权限。

统一报告的 `--strict` 退出码为 `2`，原因是 IC 期限结构未达到门槛；前向窗口和归因检查通过。

## 运行环境与数据

- IC 重算使用 `.venv310`，因为 `.venv314` 未安装 `h5i_db`。
- `.venv310` 可导入 `h5i_db`，IC 期限结构重算成功；本次记录生成时尚未安装 vn.py。
- h5i 最新视图文件时间约为 `2026-10-05 15:06`。
- `data/daily/20261005/` 已存在。
- 研究结果文件未纳入 Git，仅记录 SHA-256 供复核。

## IC 期限结构

| 视界 | 平均 IC | 正值比例 | 结果 |
|---:|---:|---:|---|
| 1d | -0.0110 | 5/11 | 未通过 |
| 5d | -0.0323 | 5/11 | 未通过 |
| 10d | -0.0496 | 4/11 | 未通过 |
| 20d | -0.0695 | 4/11 | 未通过 |
| 60d | -0.0951 | 3/11 | 未通过 |
| 120d | -0.0674 | 4/11 | 未通过 |

原始 IC 产物的决策树结论为：全负，需确认门控可回溯性后进入换信号流程。

## 其他证据

- 前向窗口：12 个记录，11 个成功；成功记录均为 `forward` 口径。
- 归因：11 条记录，达到最低样本数要求。
- `2026-09-04` 窗口仍未计入有效样本。记录生成时补跑被 `ModuleNotFoundError: vnpy` 阻断，未伪造结果、未覆盖已有成功窗口；后续应按 `pending_maturity` 处理，而不是把它算作失败。

## 2026-10-06 口径更正

- `.venv310` 已安装并验收 `vnpy==4.4.0`、`h5i_db`、`torch` 和 `stable_baselines3`。
- `.venv314` 仍缺 `h5i_db` 与 `vnpy`，不作为完整运行时。
- 报告使用 `--as-of 2026-10-06 --forward-horizon-days 120` 后，前向检查为
  `valid_window_count=11`、`pending_maturity_count=1`、`excluded_count=0`。
- 总状态仍为 `not_promotable`；没有调整生产排序、权重或交易链路。

## 输入指纹

| 输入 | SHA-256 |
|---|---|
| `ic_term_structure.json` | `698935f7177e2897668f8afd08b61cba591d515ee04b26d86f9bf9e2163400d1` |
| `vnpy_backtest_nonoverlap_fwd_results.json` | `da2c0bf5a7f2e6decea2273816e3bd1bda6e453465749eb1ce07c9f9a7e96f64` |
| `attribution_b1.json` | `47959bf3242a96af87d4be0293503e8ac43510d8dde1a6e9a5010f7fcf7e6102` |

## 下一步

先做单因子到多因子的分层证据分析：

1. 单因子 forward RankIC；
2. 因子符号与中性化前后对照；
3. 多因子融合但不接 DRL；
4. 只有在方向和样本外证据改善后，才讨论门控或权重调整。

本记录不代表策略上线批准，也不改变 PaperBook、signal freeze 或执行链路。
