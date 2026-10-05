# Alpha Shadow 对照设计

## 目标

在不改变生产排序、权重、09:25 快照、PaperBook 或交易通道的前提下，回答一个
明确问题：正向的四因子融合信号是在因子层有效，还是在 selector 混合、排序和
Top-N 过程中被稀释或反转。

本阶段只做诊断，不做 promotion。生产默认仍为 `RANK_BY_FUSION=0`，总报告继续
使用 `not_promotable` 作为当前结论。

## 共同输入原则

每个交易日的所有 arm 必须共享：

- 同一份 PIT 截面；
- 同一股票池过滤结果；
- 同一 forward-return 观察窗口；
- 同一缺失值和 fallback 处理结果。

唯一允许变化的是排序变量。输入行通过纯数据结构传入
`src/alpha_shadow.py`，不从生产 selector 读取全局环境变量，也不在 shadow 中
打开生产开关。

## 诊断 arm

| arm | 排序字段 | 用途 |
|---|---|---|
| `control_prod` | `score` | 当前生产排序基线 |
| `legacy_signal` | `signal` | 重现旧 selector 信号证据 |
| `fusion_A` | `fusion_A` | 生产方向四因子融合 |
| `selector_score` | `selector_score` | 观察最终 selector 分数 |
| `fusion_rank_on` | `fusion_rank_on` | 观察开启融合排名后的变化 |

输入行必须显式提供 `symbol` 和 `scores`。缺字段、非数值或非有限值直接报错，
不静默删除股票。

## 实验身份与缓存

`build_experiment_manifest()` 记录：

- 代码 SHA；
- 各输入产物 SHA-256；
- 排序无关的股票池哈希；
- factor/direction/weight 版本；
- selector 变体；
- 环境标志；
- arm 列表和 `experiment_config_hash`。

manifest 的整体哈希决定缓存目录：

```text
data/shadow_alpha/<experiment_hash>/manifest.json
```

manifest 通过同目录临时文件加 `os.replace` 原子落盘。原始行情和研究产物仍留在
本地数据目录，不进入 Git。

## 输出指标

`evaluate_shadow_arms()` 当前输出每个 arm 的完整排名、Top-N 标的，并相对
`control_prod` 计算：

- 共同标的数；
- Top-N Jaccard；
- 全排名 Spearman 相关。

后续真实数据 runner 再追加 RankIC（1/5/10/20/60/120d）、Q1-Q5 单调性、前向
收益/超额、换手、缺失率、fallback 次数和 universe size。没有这些共同输入和
完整指标前，不得把 shadow 结果称为策略提升。

## 环境边界

- `.venv310`：canonical runtime，已验收 `h5i_db`、`torch`、`stable_baselines3`
  和 `vnpy==4.4.0`；用于数据与执行 shadow。
- `.venv314`：纯 Python 研究/兼容性回归；缺 `h5i_db` 和 `vnpy` 时必须显式记录，
  不使用 `PYTHONPATH` 混用 site-packages。
- `2026-09-04` 的 120d 窗口在 2026-10-06 尚未成熟，报告标记
  `pending_maturity`，不计入失败，也不补造结果。

## 明确不做

- 不打开 `RANK_BY_FUSION`；
- 不修改生产权重；
- 不写 PaperBook 或真实 broker；
- 不用 vn.py 缺失时的自研 fallback 冒充 vn.py 结果；
- 不把 shadow 结果自动写回生产决策文件。
