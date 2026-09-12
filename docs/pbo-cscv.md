# PBO（回测过拟合概率）— CSCV 实现与解读

> 2026-09-13 建立。本文件说明 PBO 的口径、实现、成本、复现方式，以及**它证明了什么、
> 没有证明什么**——后者同样重要。

## 1. 指标含义

PBO（Probability of Backtest Overfitting）回答一个具体问题：

> 如果我在历史数据上把一批候选配置挑出"表现最好"的那一个，这个选择在**没见过的
> 时间段**上还能不能站得住？

PBO 高（> 50%）表示"样本内最优"在有样本外大概率落到底部一半，即那次挑参数基本是在
拟合噪声。

## 2. 为什么替换旧实现

旧 `overfitting_test.test_pbo()` 的算法是：

```
shuffled = choice(sharpes, n, replace=False) + N(0, 0.5 * std(sharpes))
rank     = P(max(shuffled) < max(sharpes))
pbo      = 1 - rank
```

`choice(..., replace=False)` 只是**置换**，因此 `max(shuffled)` 的分母项恒等于
`max(sharpes)`，判据退化为"噪声 ε 是否把其他配置抬过最大值"：

```
P(max(shuffled) < max(sharpes))  <=  P(ε < 0)  =  1/2
=>  pbo >= 0.5   恒成立
```

也就是说 **`pbo < 50%` 这项检查在数学上永不可能通过**，且噪声尺度 `0.5×std` 是写死的
常数、与策略优劣无关。实测值可佐证：12 窗口 0.7308、7 窗口 0.5366，两次都落在 0.5 以上。

## 3. 现实现：标准 CSCV

参考 Bailey, Borwein, López de Prado & Zhu (2014), *The Probability of Backtest
Overfitting*, Journal of Computational Finance。

输入是一个 **配置 × 时间** 的收益矩阵，要求每列（配置）在同一批时间块上被评估：

1. 时间轴切成 S 个不相交等长块（S 为偶数）；
2. 对 `C(S, S/2)` 种"取 S/2 块作 IS、其余作 OOS"的划分：
   - `n*` = IS 上表现最好的配置
   - `w_c = rank_OOS(n*) / (N+1) ∈ (0,1)`（归一化排名）
   - `λ_c = logit(w_c)`
3. `PBO = P(λ_c < 0)`，即 IS 最优配置在 OOS 落到后一半的频率。

```python
from pbo_cscv import cscv_pbo
res = cscv_pbo([block_1, block_2, ...])   # 每块 shape = (天数, 配置数)
res["pbo"], res["omega_mean"], res["is_oos_slope"]
```

同时输出论文的辅助量：`prob_oos_loss`（IS 最优在 OOS 亏损的频率）、
`is_oos_slope` / `is_oos_intercept`、λ 的分位数。

### 本项目的映射

| CSCV 要素 | 本项目取值 | 理由 |
|---|---|---|
| 时间块（S） | 12 个**非重叠** 120 交易日窗口 | 复用已有 OOS 窗口集，天然互不重叠 |
| 配置（N） | 18 = `top_n{5,8,10,12,15,20}` × `weight_mode{equal,signal,rank}` | 持仓只数与权重构造是研究中最典型的搜索维度 |
| 表现度量 | 年化 Sharpe（无风险利率取 0） | 与其余检查口径一致 |
| 组合数 | `C(12,6) = 924` | S=12 时的完整枚举 |

`lookback_days` **刻意固定为 120**：它同时决定回测区间长度，若变动会让各配置落在不同
时间区间，破坏 CSCV"同块可比"的前提。

## 4. 成本与复现

实测（2026-09-13，本机）：

| 步骤 | 单次耗时 | 说明 |
|---|---|---|
| PIT 现场选股 | 约 30–160s | 与 `top_n`/权重无关；按 `day` 落盘缓存（`data/pit/selection_cache/`），每窗口只算一次 |
| 纯仿真 | 约 3–13s | `top_n` 越大越慢 |

全量 12 窗口 × 18 配置 **实测 51.3 分钟**。脚本按 `(窗口, 配置)` 缓存
（`data/pbo/curves/`），可断点续跑。

```bash
python scripts/pbo_sweep.py                    # 全量扫描 + CSCV
python scripts/pbo_sweep.py --report-only      # 只重算 CSCV，不跑回测
python scripts/pbo_sweep.py --limit-windows 2  # 小样本试跑
```

产物：

- `data/pbo/curves/<day>__pbo_tn<N>_<mode>.json` — 每个 (窗口, 配置) 的日净值/收益
- `data/pbo/pbo_result.json` — CSCV 结果，由 `src/overfitting_test.py` 的 `[PBO]` 检查读取

扫描产物隔离写入 `data/vnpy_backtest/<day>__pbo_tn<N>_<mode>/`，并关闭 ArcticDB 持久化
（`persist_arctic=False`），**不会覆盖正式 OOS 窗口的 `summary.json` / `curve.json`**。

### 复现验证

`tests/test_pbo_cscv.py`（10 例）用合成数据校验估计量本身的正确性：

| 设定 | PBO 实测 |
|---|---|
| 纯噪声（18 个配置均为 i.i.d. 随机） | 均值 **0.515**（std 0.186） |
| 存在 1 个真实优势配置 | 均值 **0.000** |
| 优势强度 +0.0% / +0.1% / +0.2% / +0.4% 每日 | 0.462 / 0.322 / 0.097 / 0.000 |

单次 CSCV 估计因 924 个组合高度重叠而方差较大（std≈0.19），故测试对多种子取均值。
**关键守卫**：断言 PBO 不得退化为常数函数——这正是旧实现的失效模式。

## 5. 本次结果

```
时间块 S=12   配置 N=18   组合数 924
PBO = 0.0000 (0.00%)              [< 50% 视为通过]
λ 分布: mean=2.196  p05=1.322  p50=2.140  p95=2.890
归一化 OOS 排名均值 ω̄ = 0.885      [0.5 = 无筛选力]
IS 最优配置 OOS 亏损概率 = 0.000
IS 最优: IS Sharpe 2.659 -> OOS Sharpe 2.548
OOS~IS 回归斜率 = 0.095
```

## 6. 这个结果证明了什么，没有证明什么

**证明了**：在"持仓只数 × 权重构造"这个 18 组配置的搜索空间里，样本内挑出的最优配置在
样本外依然稳定（924 种划分下无一次落到底部一半，ω̄=0.885）。也就是说，**常用的选参
流程本身没有过拟合这些超参**。

**没有证明**：

- **不覆盖信号本身**。PBO 检验的是"从候选里挑一个"这个动作；因子/门控/市场状态模型
  的预测力由置换检验（`p=1/2^12`）与 CPCV 负责。
- **配置轴偏窄**。18 组配置高度相关（同一份选股，仅切片与权重不同），因此该 PBO 的
  信息量低于"跨因子权重网格"的 PBO。若要更强的结论，应把上游因子权重/门控阈值也纳入
  配置轴——但那需要每个变体重跑选股（约 150s/窗口/变体），成本会显著上升。
- **回归斜率 0.095 偏低**。论文把低斜率视作过拟合讯号。此处偏低的主因是同一窗口内各
  配置的 Sharpe 高度相关、方差主要由行情状态贡献，而非配置差异，故斜率被压平；
  应以 PBO 为首要判据。若后续纳入更"独立"的配置轴，需重新审视该斜率。

## 7. 相关文件

- `src/pbo_cscv.py` — CSCV 纯算法（无 IO 依赖，便于测试）
- `scripts/pbo_sweep.py` — 参数扫描 + 矩阵构建 + CSCV 调用
- `src/overfitting_test.py` — `test_pbo()` 读取 `data/pbo/pbo_result.json`
- `src/vnpy_backtest.py` — `sel_n` / `out_tag` / `persist_arctic` / `weight_mode` 参数与选股缓存
- `tests/test_pbo_cscv.py` — 10 例回归测试
