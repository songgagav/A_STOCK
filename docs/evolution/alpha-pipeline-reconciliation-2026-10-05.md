# Alpha 管线口径对账：2026-10-05

## 发现

总 IC 证据中的负值与四因子研究结果并不矛盾，因为它们测量的不是同一个字段：

| 产物/链路 | 实际测量 | 当前结果 |
|---|---|---|
| `ic_term_structure.json` / `factor_ic_forward.py` | selector 输出的旧 `signal` 字段 | 11 个窗口均值 `-0.0674`，3/11 为正 |
| `fusion_decompose.py` | `pb_inv`、`ep`、`ocf_ps`、`roe_yy_chg` 的中性化融合 | 生产方向方案 A 总均值 `+0.1039`；按实测符号方案 C 为 `+0.1333` |
| `ic_neutral_check.py` | 四个中性化单因子及融合方案 | 单因子均为正；方案 A `+0.1078`；方案 C `+0.1167` |

`selector.py` 中 `RANK_BY_FUSION` 默认关闭。因而当前研究中的“负 signal IC”不能直接解释为“四因子融合失效”，也不能直接证明最终排序分数的样本外表现。

## 决策

- 不修改 `factor_fusion.DIRECTIONS`。
- 不启用 `RANK_BY_FUSION=1`。
- 不把正向四因子拆解结果直接当作生产策略已通过。
- 先建立两条链路的同窗、同池、同字段 shadow 对照。

## 下一步实验

实验必须分别输出并保存：

1. 旧 `signal` 的 forward RankIC；
2. 四因子融合分的 forward RankIC；
3. 最终 selector `score` 的 forward RankIC；
4. `RANK_BY_FUSION=0/1` 的入选池重叠率、换手和成本调整收益。

实验需要独立缓存目录和独立输出文件，不能复用或覆盖默认 `data/pit/xsec` 缓存。结果先进入 shadow 报告，不能自动接入 PaperBook、signal freeze 或实盘。

## 证据边界

本记录只说明当前几条研究链路的字段定义和结果不同，不构成生产切换批准。下一步若不能在同一数据版本、交易日历和 PIT 口径下完成对账，所有策略升级继续保持冻结。
