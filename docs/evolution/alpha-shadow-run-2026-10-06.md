# Alpha shadow 对照运行记录：2026-10-06

## 运行范围

本次使用 `.venv310`，读取主工作区已有的 11 个共同 PIT 截面：

- `data/pit/xsec/<day>.parquet`
- `data/pit/fusion_x/<day>.parquet`
- `data/h5i/market.db` 的只读日线与交易日历

运行提交：`f3648c8`。实验配置：

- `FACTOR_HEALTH_ENABLED=1`
- `RANK_BY_FUSION=0`
- 因子权重/方向来自 `data/fusion_decompose.json`
- `control_prod`/`selector_score` 使用 `selector_weights(as_of=day)` 重建旧 selector composite score
- `fusion_rank_on` 在本次配置下等价于纯融合（alpha=1.0）

原始输入、manifest 和 result 只写入本地临时 shadow 缓存，不进入 Git，不写入
`selection.json`、`target_plan.json`、PaperBook、signal freeze 或 broker 状态。

## PIT 覆盖率

`fusion_x` 缺失的 xsec 行被明确排除，并写入每个输入文件的 `coverage` 字段；没有
静默补零或伪造融合值。

| 决策日 | xsec | 纳入 | 排除 |
|---|---:|---:|---:|
| 2018-06-29 | 1563 | 1412 | 151 |
| 2019-06-28 | 1530 | 1520 | 10 |
| 2020-01-03 | 1621 | 1593 | 28 |
| 2020-12-31 | 1798 | 1746 | 52 |
| 2021-07-02 | 1886 | 1856 | 30 |
| 2022-06-30 | 2093 | 2043 | 50 |
| 2022-12-30 | 2110 | 2096 | 14 |
| 2023-12-29 | 2250 | 2216 | 34 |
| 2024-07-03 | 1856 | 1849 | 7 |
| 2025-06-30 | 2390 | 2343 | 47 |
| 2026-03-05 | 3022 | 2947 | 75 |

## 结果摘要

以下是每个 arm 的 11 窗口均值。收益是 Top-N 平均前向收益小数，不是超额收益，
未扣交易成本；RankIC 是全纳入截面 RankIC。

### RankIC

| arm | 1d | 5d | 10d | 20d | 60d | 120d |
|---|---:|---:|---:|---:|---:|---:|
| control_prod | 0.0089 | 0.0127 | 0.0104 | -0.0263 | -0.0197 | 0.0033 |
| legacy_signal | -0.0140 | -0.0309 | -0.0472 | -0.0655 | -0.0941 | -0.0666 |
| fusion_A | 0.0248 | 0.1050 | 0.1238 | 0.0947 | 0.1052 | 0.0959 |
| selector_score | 0.0089 | 0.0127 | 0.0104 | -0.0263 | -0.0197 | 0.0033 |
| fusion_rank_on | 0.0248 | 0.1050 | 0.1238 | 0.0947 | 0.1052 | 0.0959 |

### Top-N 平均前向收益

| arm | 1d | 5d | 10d | 20d | 60d | 120d |
|---|---:|---:|---:|---:|---:|---:|
| control_prod | 0.0035 | 0.0085 | 0.0108 | 0.0046 | 0.0484 | 0.0618 |
| legacy_signal | 0.0017 | 0.0038 | 0.0096 | 0.0048 | 0.0535 | 0.0330 |
| fusion_A | 0.0021 | 0.0085 | 0.0183 | 0.0139 | -0.0226 | -0.0387 |
| selector_score | 0.0035 | 0.0085 | 0.0108 | 0.0046 | 0.0484 | 0.0618 |
| fusion_rank_on | 0.0021 | 0.0085 | 0.0183 | 0.0139 | -0.0226 | -0.0387 |

### 排序差异与换手代理

| 对比 | 平均 Top-N Jaccard | 平均 rank correlation |
|---|---:|---:|
| fusion_A vs control_prod | 0.0275 | 0.1804 |
| legacy_signal vs control_prod | 0.3706 | 0.6139 |

`control_prod` 的窗口换手代理为 `0.9455`，`fusion_A`/`fusion_rank_on` 为 `0.9818`。
这里的 previous holdings 是相邻 shadow 窗口的 `control_prod` Top-N，而不是连续每日实盘
持仓；因此只能作为窗口间替换率诊断，不能当作真实日换手成本。

## 结论与边界

1. 这次运行确认了旧 `legacy_signal` 的多期限负 RankIC，也确认了 `fusion_A` 的截面
   RankIC 为正，但二者在长期 Top-N 前向收益上并不一致。
2. `fusion_A` 的 120d Top-N 平均收益为 `-0.0387`，因此不能依据正 RankIC 自动晋级。
3. `fusion_rank_on` 在 alpha=1.0 下与 `fusion_A` 相同；这不是独立的生产切换证据。
4. `selection_cache` 没有作为本次控制组的权威输入；控制分数按当日 selector 权重从
   xsec 字段重建，避免复用旧缓存版本造成口径污染。
5. 当前不修改 `RANK_BY_FUSION`、生产权重、PaperBook、signal freeze 或交易链路。

后续必须在连续、明确的 OOS 窗口上补充超额收益、交易成本、真实持仓换手和成熟度审计，
再由人工决定是否进入新的研究结论；本报告不构成 promotion。
