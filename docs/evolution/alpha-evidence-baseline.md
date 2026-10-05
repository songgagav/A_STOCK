# Alpha 证据基线

## 目的

本工具把已有研究产物汇总成一个可审计的状态，防止以下情况被误读为策略通过：

- 研究文件缺失；
- 回测窗口使用 backward 口径；
- 窗口使用简化/fallback 引擎；
- 窗口起点早于决策日；
- IC 样本量不足或方向为负；
- 归因数据没有覆盖。

它只做研究证据检查，不计算新信号、不修改因子权重、不接入 PaperBook，也不改变交易权限。

## 输入产物

默认读取以下文件。它们是研究运行的输出，不应提交到代码仓库：

| 输入 | 来源 | 要求 |
|---|---|---|
| `data/ic_term_structure.json` | `scripts/research/ic_term_structure.py` | 1/5/10/20/60/120 日视界均有 `mean`、`pos`、`n` |
| `data/vnpy_backtest_nonoverlap_fwd_results.json` | `scripts/nonoverlap_rerun.py` | `window_mode=forward`，非 fallback，窗口起点不早于决策日 |
| `data/attribution_b1.json` | `scripts/attribution_forward.py` | 至少 5 条窗口归因记录 |

注意：`ic_term_structure.json` 和 `factor_ic_forward.py` 当前读取的是
`data/pit/xsec/<day>.parquet` 中 selector 的 `signal` 字段。它不是
`factor_fusion.cross_section_scores()` 的四因子融合分，也不是最终 selector
`score`。三者必须分开解释，详见
[`alpha-pipeline-reconciliation-2026-10-05.md`](alpha-pipeline-reconciliation-2026-10-05.md)。

输入存在时会记录 SHA-256；输入缺失、读取失败和结构错误均保留在报告中。

## 默认门槛

这是保守的“进入人工复核”门槛，不是自动上线规则：

- IC：六个视界全部满足 `mean IC >= 0.02`、正 IC 比例 `>= 50%`、有效日数 `>= 10`；
- 前向窗口：至少 10 个有效窗口；
- 归因：至少 5 条记录；
- backward、fallback、窗口起点早于决策日的记录不计入有效窗口。
- 尚未达到 horizon 的最近窗口标记为 `pending_maturity`，不计入失败；成熟度判断必须带明确的 `as_of` 日期和 horizon。

阈值写入报告，未来若调整必须单独记录研究决策，不能静默修改。

## 状态语义

| 状态 | 含义 | 下一步 |
|---|---|---|
| `unavailable` | 研究产物缺失 | 重新生成产物 |
| `invalid` | 产物结构或关键字段错误 | 检查研究运行/输入 |
| `not_promotable` | 产物完整，但证据未达到门槛 | 检查信号或重新设计实验 |
| `evidence_ready` | 仅表示可以人工复核 | 人工审阅，不自动启用 |
| `pending_maturity` | 未来观察期尚未发生完 | 等待成熟后重新运行，不补造结果 |

任何状态都不会改变执行权限；报告固定包含 `execution_change=false`。

## 使用

```powershell
.venv310\Scripts\python.exe scripts\research\ic_term_structure.py
.venv310\Scripts\python.exe scripts\nonoverlap_rerun.py
.venv310\Scripts\python.exe scripts\attribution_forward.py

.venv310\Scripts\python.exe scripts\research\alpha_evidence_report.py `
  --output reports\alpha_evidence.json `
  --strict
```

`--strict` 在状态不是 `evidence_ready` 时返回退出码 2。默认不加 `--strict` 时只输出报告，便于诊断缺失输入。

## 当前基线判断

历史 PIT 研究记录在 [`docs/pit-valuation.md`](../pit-valuation.md) 中已经记录过前向 RankIC 偏弱/为负的证据。
2026-10-06 的重跑仍为 `not_promotable`；其中 2026-09-04 的 120d 窗口是
`pending_maturity`，不是失败。当前报告不会因为 vn.py 缺失而伪造窗口结果；完整执行
shadow 使用 `.venv310` 的 `vnpy==4.4.0`。

后续重新生成产物后，报告应与原始 IC/OOS/归因文件一起归档，并关联：

- 决策日和交易日历版本；
- 数据版本与 source metadata；
- 代码 SHA；
- 因子/权重配置版本；
- 研究命令及完整退出码。
