# 路线图 ⑦ 项：能力交付与接线记录（2026-09-22）

> 本文记录 7 项能力的**落地边界**：每项做了什么、**没做什么**、判据是什么、
> 以及为什么某些"看起来该默认开启"的检查被刻意做成"记数不判定"。
>
> 单一事实源是 `ops/acceptance_status.json`（见 `ROADMAP-7` 条目）；
> 本文是它的展开说明。真机证据由 `scripts/verify_roadmap7.py` 每次实跑生成
> （`data/preflight/verify_roadmap7.json`），不是一次性结论。

## 0. 一眼看全：交付物与开关

| 项 | 新模块 | 生产接线点 | 默认行为 | 开关 |
|---|---|---|---|---|
| ⑨ Regime 多场景回测 | `regime_costs.py` | `vnpy_backtest.py`（费率投影+信号延迟）、`backtest_engine.py` | **normal = 恒等**，与既有产物逐位可比 | `PAPER.regime_scenario` / `REGIME_SCENARIO` |
| ⑩ 动态止损 Trailing Stop | `trailing_stop.py` | `paper_book.py`、`realtime_engine.py`、`backtest_engine.py` | **开启** | `PAPER.trailing_stop` |
| ⑪ 多策略组合回测 | `portfolio_backtest.py` | 供 harness 调用（未接 `run_daily`） | 需显式调用 | 无开关（新增入口） |
| ⑫ TWAP/VWAP 拆单执行 | `exec_algo.py` | **未接实盘下单路径** | 离线规划/仿真 | 无开关（尚未接线） |
| ⑬ Purged K-Fold + Embargo + CPCV + DSR | `purged_cv.py` | 供研究/CI 调用（与 `pbo_cscv.py` 并列，未替换） | 需显式调用 | 无开关（新增入口） |
| ⑭ LLM 因子假设生成 | `factor_hypothesis.py` | 供研究调用（收益评估需自备回调） | 需显式调用 | 无开关（新增入口） |
| ⑮ 交易前风险清单前置 | `pretrade_gates.py` | `pretrade_compliance.gate()`（下单咽喉点） | **开启**（回撤/日亏损判定，其余记数） | `PAPER.pretrade_gates` |

合计新增：7 个模块 + 7 个测试文件 + 1 个验证脚本；**全量测试 1767 passed / 0 failed**。

---

## ⑨ Regime 多场景回测

**做了什么**：把成本/延迟从"单场景写死"改为可按场景投影。`stress` = 滑点×4、
手续费×2、信号延迟 +1 bar；`normal` = 全部恒等。

**关键设计：为什么 `normal` 必须是恒等**。若 `normal` 的倍率不是 1，既有的全部
回测产物（`data/vnpy_backtest/**`、PBO 扫描、OOS 窗口）就与今天的结果**不可比**，
而这件事不会报错 —— 只会让"回测变好了"变成一句无法归因的话。故 `stress` 只在
显式指定（参数或 `REGIME_SCENARIO=stress`）时生效。

**延迟是怎么实现的**：读 `vnpy/alpha/strategy/backtesting.py` 得知
`get_signal()` 是 `signal_df.filter(datetime == self.datetime)`，而 bar 是逐根喂入的。
所以**把信号行的日期整体后移 N 根 bar，就等价于"决策后 N 根 bar 才成交"**，
且引擎不可能看到未来 bar（`dts` 已按时间排序，`history_data` 只累积到当期）。
代价是窗口末 N 条信号被推出窗口、不成交 —— 代码**如实丢弃并计数**
（`signals_dropped_by_latency`），不补做、不假装。

**归因可分离**：`project_rates(..., slippage_share=...)` 让滑点部分只吃
`slippage_mult`、税费部分只吃 `fee_mult`。实测（本机费率）：
`滑点占比 72.9%`，stress 下买费率 +23.60bps、卖费率 +35.89bps。

**量级自检**：压力滑点约 0.242%，是一个 A 股主板跌停板（10%）的 1/41 ——
压力假设仍需落在物理可能区内，否则"压力场景"变成"不可能场景"，数字再难看也没有信息量。

**批量入口**：`run_regime_scenarios(day, ...)` 单日多场景；
`run_regime_batch(days, ...)` 多日多场景 → 按场景聚合的均值与 `worst_delta_pct`。
单日只能说明"这一天如何"，成本敏感性是**分布**性质，必须在多天上平均。
每个场景写**独立目录**（`<day>__regime_<name>/`），否则后一个场景会覆盖前一个，
让"压力场景的数字"无从辨认。

**未做**：`long_rate`/`short_rate` 是 vnpy 唯一能表达的成本口径，无法表达
"最低 5 元佣金"与"按笔滑点"；这是既有近似（`vnpy_backtest` 注释已声明），
本次未改。

---

## ⑩ 动态止损 Trailing Stop

**问题**：固定止损只锚定**成本**（`price/avg_cost - 1 < -3%`）。于是"赚过又跌回去"
的单与"从未赚过"的单被同等对待：一只票 +20% 后回落到 +2%，固定止损认为"还赚着，
不动"，而账户已实际吐出 18pp。

**做法**：`止损线 = max(固定线, 峰值线)`，其中
`固定线 = entry*(1-stop_loss)`、`峰值线 = peak*(1-giveback)`，
`peak` 是入场以来有利极值（做多=最高价），跨 tick/跨日单调不减。

**核心不变量：移动线恒 >= 固定线**。刚赚一点点时峰值线会**低于**固定线
（peak=1.01·entry、giveback=6% 时峰值线 = entry·0.9494，比固定线 entry·0.97 更低）；
此时若只取峰值线，止损会比原口径**更早**触发 —— 那不是"锁利润"而是"改紧了风险预算"，
属未经批准的行为变化。取 max 保证**移动止损永不比固定止损更早砍仓**。
`tests/test_trailing_stop.py` 用 9 个峰值档位锁死这条。

**参数取值依据（推导，非拍脑袋）**：

- `trailing_giveback = 0.06`：入场承受 1 个 `stop_loss`(3%) 的风险；要求锁住
  **2 倍**于该风险的利润 ⇒ 回吐 6%。等价说法：peak 需涨到 `(1-3%)/(1-6%) ≈ +3.2%`
  以上，峰值线才超过固定线、开始真正锁利。取 2 倍而非 1 倍是为了让
  "刚成交就被自己的移动线扫掉"不可能发生。
- `trailing_hard_floor = 0.09`：A 股主板单日涨跌停 ±10%，一个跌停板不足以击穿该
  地板（9% < 10%），即地板不会被单根 bar 的跳空直接打穿而变成无效约束。
- **`hard_floor` 的语义是"最多容忍跌多深"，只约束固定线**。实现时曾写成
  "两道线都取 max"（=把容忍度**放宽**）与"对峰值线也取 min"（=把已锁的利润
  重新放开），两次都被单测抓出来。最终实现：`entry_floor` 内 `min(line, entry*(1-hard_floor))`，
  `trail_line` 返回 `max(固定线, 峰值线)` —— 地板是**固定线的容忍上限**，
  而峰值线锁的是**成本之上的利润**，两者不能互相压制。

**决策/撮合分离**：`trailing_stop` 是纯函数、**只判定不卖出**；卖出由
`PaperBook` / `realtime_engine` 执行，使这两条路径各自保留 T+1 锁定、
跌停不可卖、冷却集等既有约束。归因（这一笔到底是被峰值线还是固定线砍的）由
判定返回的 `trigger` 字段直接带出，`apply_risk_controls()` 的 `trailing` 字段
汇总 —— **不靠反向解析日志文本**（那样一个措辞改动就会让归因静默失效）。

**产物留痕**：`backtest_engine` 的 `result.json` 增加
`trailing_stop: {enabled, giveback, hard_floor, trailing_exits}`，
使"这次回放里有多少笔是被峰值线砍的"可自证，而不是只能从日志里数。

**未做**：不按波动率自适应调整 `giveback`（那需要标定，属另一次改动）；
不做分批止盈（只做整笔离场，与既有固定止损同粒度）。

---

## ⑪ 多策略组合回测

**问题**：本仓至今是单策略回测 —— `run_vnpy_backtest` 读死**一份**池，
`BacktestRunner` 同样只有一个目标池。于是"fusion + gp4 + 新信号合起来会怎样"
没有可执行的回答：换掉那一份池得到的是"另一个单策略结果"，不是"组合结果"。

**做法**：把 N 条策略的**目标权重矩阵**按权重聚合成一条组合权重，
再喂给**同一个** `PaperBook` 撮合流程。因此组合结果与单策略结果**成本口径完全
一致**（同一份费率/T+1/整手/涨跌停），"加了这个策略改善了多少"才是两个可比的数。

**为什么用权重聚合而不是信号投票**：投票法丢弃强度信息（0.9 与 0.1 的看好投出
同样的票）；权重聚合可微、可算风险预算、能直接落到 target qty
（与 `target_weighting` 的 `target_mv = total * w` 同构）。

**几个刻意的边界行为**：

- 对齐取**并集**而非交集：某条腿只在部分日期出手是常态，取交集会把可回测区间
  压到最短那条腿上，并掩盖"这条腿经常不出手"这一事实。
- 每条腿**逐日归一化**：不归一等于让标度大的那条腿独占组合（那不是多策略，
  是单策略加了个噪声项）。全 0 的日子**保持全 0**，使"今天没意见"与"平均看好
  所有票"区分开。
- **负权重（做空腿）直接抛错**：隐式接受负权重会让 `sum(w)` 的"总暴露"语义崩掉。
- `compare_to_single()` 给出组合 vs **每一条单腿**，以及 `versus_best_single.excess_pct`
  —— 组合跑不过最好的单腿时**如实显示为负**（实测本次演示为 -0.90pp）。
- 撮合顺序**先卖后买**（卖出释放的现金才能被买入用上，与 `PaperBook.rebalance` 一致；
  反过来会在满仓时静默少买）。

**未做 / 局限（如实）**：涨停不可买、跌停不可卖需要**前一交易日**收盘价，属数据层
职责，故由调用方以 `tradable_of` 回调提供；不提供时视为"不判定，可交易"。
本模块**不自己取数**，因此未接入 `run_daily`；要接需在调用方写一个
"DB → 权重矩阵"的适配器（那会改动 `run_daily.py`，不在本次范围）。

---

## ⑫ TWAP/VWAP 拆单执行算法

**做法**：`schedule_twap` / `schedule_vwap` / `participation_cap_slices` /
`plan_execution` / `simulate_execution`。逐档滑点**复用** `slippage_model.decompose_slippage`
（有测试断言 `exec_algo.decompose_slippage is slippage_model.decompose_slippage`，不是另写一套公式）。

**三个刻意不粉饰的设计**（都来自交付报告里的实测，值得单独记住）：

1. **`saving_bps` 可以为负且不被 `max(0, ...)` 掩盖**。实测 50000 股/10 档、
   ADV=1e7、vol=5%、窗口 60 天：naive 62.5bps vs sliced 83.4bps ⇒ **saving = -20.9bps**。
   低参与率 + 高波动 + 长窗口下**拆单反而更贵**（执行风险分量主导）—— 这正是需要
   被看见的结论。
2. **`saving_bps` 与 `naive_price - slice_vwap_price` 可以反号**。
   前者只含成本模型口径的滑点差，后者还含各档参考价自身的价格路径，
   两者**不要互相校验**（`assumptions` 里已写明）。
3. **"一天吃不掉"如实返回**。ADV 太小时 `unscheduled` 给出缺口与原因
   （实测 `adv_too_small`，缺口 9,999,000 股），不假装全成。

**单位陷阱（接线必读）**：`participation_cap_slices(adv=)` 吃**股数**，
`simulate_execution(avg_daily_volume=)` 吃**成交额（元）**（与 `slippage_model` 一致）。
函数签名里没有价格参数，故无法在模块内统一 —— CLI 用 `--adv` / `--adv-shares`
两个参数显式暴露换算。

**未做**：**未接实盘下单路径**。要接需在 `realtime_engine` 的下单点把
`plan['slices']` + `sim['per_slice'][i]['horizon_days']` 映射到 `PaperBook.buy/sell`
的逐档调用，并处理"同一标的跨 tick 的剩余未成量"。这是一次独立改动（会改变
下单时序），故本次只交付离线算法与仿真。

---

## ⑬ Purged K-Fold + Embargo + CPCV + Deflated Sharpe

**做法**：`PurgedKFold` / `CombinatorialPurgedCV` / `deflated_sharpe_ratio` /
`probabilistic_sharpe_ratio` / `min_track_record_length` /
`probability_backtest_overfitting`，全部只依赖标准库 + numpy（`norm_cdf`/`norm_ppf`
用 `math.erf` 自实现，**不引入 scipy**）。

**与既有 `pbo_cscv.py` 的关系**：并列而非替换。`probability_backtest_overfitting`
内部优先调用 `pbo_cscv.cscv_pbo`（`source='pbo_cscv'`），失败才退回本地等价实现
（`source='local'`）。实测同一输入**逐位相等**：`0.528571428571` vs `0.528571428571`。

**purge 的口径**：逐对区间重叠判定（`[t0,t1]` 有交即剔），**不用"测试集跨度"简化**
—— CPCV 的测试块不相邻时，跨度法会把测试块之间的训练样本大量误剔
（实测组合 (0,5) 会把训练集剔成空集，逐对法保留 [3,4,5]）。无 `t1` 时**显式退化**
为"仅 embargo"，绝不假装做了 purge。

**单位口径（接线必读）**：`sr` / `benchmark` / `target_sr` / `sr_variance` 全部按
**年化**解释，内部除以 `sqrt(trading_days)`；返回 dict 里 `sr0`/`sigma_sr`/`sigma_trials`/
`var_sr_trials` 是**单期**口径，`sr`/`e_max_sr` 是年化，`scale` 字段写明。
`dsr == psr` 恒成立（DSR 的定义就是把基准换成 `sr0` 的 PSR）；`n_trials=1` 时
`sr0=0`、DSR 精确退化为 `PSR(0)`（原式含 `Z^-1(1-1/1) = -inf`，必须特判）。

**如实说明两条被推翻的直觉**（来自交付时的自查）：

- **"单次纯噪声矩阵的 PBO ≈ 0.5"不成立**：跨种子 std ≈ 0.21、单次值域 0.13–0.86，
  因为 `C(S, S/2)` 个组合高度重叠，有效独立样本远少于组合数。故测试断言的是
  **多种子均值 ∈ [0.40, 0.70]**，而不是单次落在 0.5 附近。
- **"每块太短会抛错"不成立**：每块 1 天时 IS 仍有 S/2 天、Sharpe 算得出来，
  函数合法返回。真正的报错条件是"**所有组合的 IS 度量都是 NaN**"。

---

## ⑭ LLM 因子假设生成（FaVOR 式假设级证据）

**设计意图**：围绕"**假设级证据**"而非"收益结果"组织因子挖掘 —— 一个因子在被看
收益之前，必须先通过"经济逻辑能否成立、能否被独立证据支持"的检验。
这是为了对抗纯收益导向挖掘必然导致的过拟合。

**流水线纪律（测试用 mock 计数锁死）**：`validate_batch` 的次序是
`lint → evidence_check → 收益评估`，**没通过前两步的假设绝不进入收益评估**。

**lint 的前视偏差静态防线**：表达式含 `shift(-n)`、`future_*`、`next_*`、
`t+` 开头的字段等模式一律拒绝。这条防线"宁可错杀"（字段名里含 `future` 的合法
字段也会被拒）—— 这是刻意的：误杀看得见，漏杀看不见。

**表达式语法与 `gp_mine_daily` 同形**：规范形态 = gplearn 打印的前缀函数式
（即 `gp_mine_daily._to_readable()` 的输出），算子名是 `GP_FUNCTIONS` 的超集，
另加 7 个 `ts_*` 时序算子。**三条如实差距**：
① 不 import `gp_mine_daily`（它会拉起 pandas/scipy/gplearn/DB），属"同形兼容"
而非复用同一求值器；② 退化输入的数值语义**刻意不同**：gp 的
`_safe_div/_safe_log/_safe_inv` 返回 1.0/0.0 以保证 gplearn closure 永不 NaN，
本模块返回 **NaN** —— 因为 evidence 阶段要用覆盖率/唯一取值数判退化，
伪造的常量会把"算不出来"掩盖成"算出来了"；③ 残留的 `X0` 占位符会被当成字段名，
调用前需先做 `_to_readable` 式替换。

**未接线的部分（重要）**：收益评估只留了 `return_evaluator` 回调接口，
**尚未接** `factor_mine.evaluator` / `ai_factor_lab`。接它需要在调用方写适配器
并显式给出自己的 IC/ICIR 门槛（属别的文件改动）。`--selftest` 用的是**恒通过的
STUB 回调**并在输出里明确标注 —— 不要把自检输出读成"真实收益已验证"。

---

## ⑮ 交易前风险检查清单前置

**与既有 `pretrade_compliance` 的分工**：后者校验**这一笔单本身**是否成立
（整手/价格/数量/可交易/现金底线/可卖数量），拿不到也不需要组合状态；
`pretrade_gates` 校验**组合层面此刻是否允许加仓**（单笔仓位/组合回撤/IC 门控态/
数据新鲜度/单日亏损）。两者输入不同、失效模式不同，故不合并成一个函数 ——
但**在同一个咽喉点被依次调用**（`pretrade_compliance.gate()`），所以"清单"是一份。

**四条纪律**：

1. **缺字段 => skip，不是 fail**。若拿不到字段时默认拒单，任何一次上游字段改名
   都会让系统**静默停手** —— 比不设闸门危险得多。
2. **只拦买入**。离场单全部 skip：把卖出也闸住 = 把风险锁在仓里
   （与 `kill_switch`「只停新开仓，绝不停离场」同一条纪律）。
3. **判定异常不阻断交易，但必须响亮留痕**（在 `order_audit.jsonl` 里明写
   "交易前清单执行异常(不阻断, 需排查)"）—— 一个 bug 不能让系统静默停手。
4. **不抢高危单的裁决权**。本仓红线是"高危单**不执行，转人工**"，不是"直接拒"。
   故 30% 权益的大单仍返回 `pending_approval` 进待批队列（已实测），
   清单只拒"组合状态不允许加仓"这一类。

**两项刻意做成"记数不判定"（默认只记录，不据此拒单）—— 这是本次最重要的判断**：

**(a) 数据新鲜度**。用户清单口径是 `data_lag_days == 0`。但本仓产物里的
`data_lag_days` 是**自然日**差，不是交易日差 —— 依据是仓内脚本的原话：
`scripts/check_daemon_first_day.py:257`「**自然日**差(09-18→09-21 = 3)，**不是**
交易日差，故不会是 0/1」；`scripts/record_upstream_lag.py:16` 同口径。
实测 2026-09-21 的 `target_plan.json`：`section_as_of=2026-09-18, data_lag_days=3,
source=h5i_view`，且上游账本记着「厂商当日数据尚未发布（实测 20:10 仍
engine_day=2026-09-18）」。

于是"== 0"的直接后果是：**每个周一、每个节后第一个交易日、以及厂商当日数据
尚未发布时，清单都会 fail 并拒绝一切买入**。在默认开启的情况下这是一次
**静默停手**。故默认 `max_data_lag_days = None`（记数不判定，但实测滞后值仍写进
`detail`/`measured`，面板与日志里看得见），严格模式用
`PAPER.pretrade_strict_freshness=True` 或 `PRETRADE_STRICT_FRESHNESS=1` 显式开启
—— 届时周一/节后会拦单，那是**知情选择**。

**(b) 单笔仓位上限**。直觉是复用 `max_single_weight`(8%)，但那是**集中度压回线**
（`PaperBook._apply_concentration_cap`：超过 `8%×1.55=12.4%` 才把超配部分卖回 8%），
**不是**"单笔不得超过"的下单前硬上限。用它当拒单线有两个后果：
① 目标池不足 9 只时等权 band 就 >8%，会把**全部**买单拒掉；
② `target_weighting` 给单票的目标上限本就是 **11.5%**，用 8% 拒单等于让策略层的
目标永远无法达成。

这条是**被测试抓出来的**：`tests/test_pending_orders.py` 的 9 个用例原先全部失败
（`assert 'reject' == 'pending_approval'`），因为那组用例构造的 3000 股@10元
（=30% 权益）本应进人工队列，却被 8% 的仓位闸门直接拒了。修法：默认
`max_position_pct = None`（只记数），要真正启用由运维显式给一个**同范畴**的上限
`PAPER.pretrade_max_position_pct` / `PRETRADE_MAX_POSITION_PCT`。

---

## 数据新鲜度的接线增量（顺带修的一处"字段被丢弃"）

`pretrade_gates` 的数据新鲜度项需要 `data_lag_days`，而它在 `target_plan.json` 里
**早已存在**（`drl_train` 生成 payload 时落盘），只是被
`realtime_engine._plan_to_targets()` 在构造 `info` 时丢掉了，使该项永远只能 skip。
本次把它（连同 `section_as_of`）透传出来 —— **纯增量，不改任何选股/权重结果**，
只是不再丢弃已有字段。

---

## 复跑方式

```powershell
# 只读项（不写任何正式产物；报告落 data/preflight/verify_roadmap7.json）
.venv314\Scripts\python.exe scripts\verify_roadmap7.py

# 单项
.venv314\Scripts\python.exe scripts\verify_roadmap7.py --only 10,15

# 额外实跑一段真实多场景回测（需持有 vnpy 的解释器，慢）
& "$env:APPDATA\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe" `
    scripts\verify_roadmap7.py --only 9 --with-backtest
```

全量测试（本机 `.venv314`，需给 pytest 一个可写的 basetemp）：

```powershell
.venv314\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp\run
```

## 相关文档

- `ops/acceptance_status.json` → `ROADMAP-7` 条目（单一事实源）
- `docs/vulnerability-register.md`（由 `scripts/render_vulnerability_register.py` 生成）
- `docs/pbo-cscv.md` —— 既有 CSCV-PBO 的口径与"替换原实现"的原因
- `docs/units.md` —— 单位约定（本次多处 `_pct` / bps / 自然日 vs 交易日 的口径引用它）
