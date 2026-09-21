# P0/P1/P2 清单 18 项落地盘点（2026-09-22）

> 逐项核对「优化项 → 本仓现状 → 是否落地 → 证据」。判定原则：**以仓内可复跑的证据
> 为准**，不以"代码里有类似名字"为准（本仓已因「文档说有守卫、代码里没有」吃过教训）。
> 未落地项列在最后一节，含**为什么没做**与**要动哪里**。

## 一、P0（1-7）

| # | 优化项 | 落地 | 本仓实现 | 证据 |
|---|---|---|---|---|
| 1 | 审批门控（fail-closed）+ 人工审批队列 | ✅ **早已落地** | `pretrade_compliance.gate()` 三段式（合规→高危→人工）；高危单入 `data/pending_orders.json` 待批队列；「批准即白名单」一次性票；判定异常**不阻断**但落审计 | `src/pretrade_compliance.py`；`tests/test_pending_orders.py`；登记 `P1-PRETRADE` |
| 2 | 交易日志双轨（Agent 决策 / 人工干预分开） | ✅ **早已落地** | 订单级流水 `data/order_audit.jsonl`（含 `actor`）与闸门级事件 `data/kill_switch_ledger.jsonl` **分文件**；人工决定走 `decide(actor="human")` 单独落条目 | `pretrade_compliance.audit/decide`；`src/kill_switch.py`；`data/order_audit.jsonl` |
| 3 | 分层 Kill Switch（GLOBAL/ACCOUNT/STRATEGY） | ✅ **早已落地** | 三层齐备 + 统一留痕；纪律「只停新开仓，绝不停离场」写在模块 docstring | `src/kill_switch.py`；登记 `P1-KILLSWITCH` |
| 4 | 自动降级控制器（延迟/拒单率/滑点驱动） | ◐ **部分** | 降级状态机 `NORMAL/DEGRADED/HALTED` 已装配并发布（延迟/数据滞后/账户静态估值 → DEGRADED；L3 或盘中数据停流 → HALTED）；观测栈指标 `astock_*` + 告警规则 | `src/health_state.py`；`ops/alert_rules.yml`；登记 `P2-HEALTHSTATE`/`P2-HEALTHPUB`。**缺口**：驱动量里没有**拒单率**与**实测滑点**这两个因子（见第四节） |
| 5 | 交易前强制合规检查（资本/杠杆/敞口） | ✅ **本轮补全** | 单笔合规（整手/价格/数量/可交易/现金/可卖）+ 组合闸门（仓位/回撤/IC门控/新鲜度/日亏损）；**本轮新增卖侧账实相符金丝雀** | `src/pretrade_compliance.py`、`src/pretrade_gates.py`、**`src/live_gates.py`（新）**；`tests/test_live_gates.py`（36 例） |
| 6 | 心跳 + 看门狗 + Dead-Man's Switch | ✅ **本轮补全** | 心跳（组件自报活性）与数据流看门狗早已有；**本轮新增 Dead-Man's Switch** —— 「本该出现的 tick 没出现」即失败，覆盖"监测者自己失联"这一共同盲区（心跳/看门狗都要求监测者还活着） | **`src/deadman_switch.py`（新）**；`tests/test_deadman_switch.py`（38 例）；接线 `src/daemon.py` 每分钟 beat、`src/realtime_engine.py` 每轮 beat；`data/deadman_ticks.jsonl`（哈希链） |
| 7 | 连接器能力声明（capabilities 字段） | ✅ **本轮补全** | `agent_tools` 是 20 个工具的唯一注册表；**本轮为每个工具加能力声明**（读行情/读知识库/读模型/计算/写产物/写台账/否决/下单），并在**唯一入口** `execute()` 上加调用前能力校验（`require=/forbid=`） | **`src/agent_tools.py`（改）**；`tests/test_agent_tool_capabilities.py`（29 例）。边界不变量：`order_tools == []`（Agent 工具不得直接下单），`unclassified == []` |

## 二、P1（8-13）

| # | 优化项 | 落地 | 本仓实现 | 证据 |
|---|---|---|---|---|
| 8 | 角色边界收紧（风控 Agent 只读，执行权集中） | ◐ **更接近了** | 第 7 项的能力声明把它从"口头约定"变成**可校验事实**：`is_read_only(name)` 可机器判定，`execute(forbid=("order",...))` 可在调用前拦截。**缺口**：还没有把「风控 Agent 只能调只读工具」这条约束**钉在具体 Agent 上**（见第四节） | `agent_tools.is_read_only` / `audit_capabilities`；`tests/test_agent_tool_capabilities.py` |
| 9 | OHLCV 数据质量校验（时间戳/缺口/OHLC 关系） | ✅ **早已落地** | 入库结构哨兵在 h5i **唯一写入点前**拦下任何来源都不合法的行：OHLC 关系（`high < max(open,close,low)`）、非正价格、负量、核心 NaN **一律拒绝写入** | `src/data_quality_guard.py`（`ohlc_relations` 等规则）；`src/h5i_sync.py`；登记 `P1-DQGUARD`/`P1-ZEROFILL` |
| 10 | 多场景（Regime）回测 + stress 标准项 | ✅ **上轮落地** | `stress` = 滑点×4/费×2/信号延迟+1bar；`normal` 恒等保证既有产物可比 | `src/regime_costs.py`；`tests/test_regime_costs.py`；真机 Δ = **−0.0932pp** |
| 11 | 动态止损（Trailing Stop） | ✅ **上轮落地** | `max(固定线, peak*(1-giveback))`；核心不变量「永不比固定止损更早砍仓」；三条生产路径同源 | `src/trailing_stop.py`；`tests/test_trailing_stop.py`；`paper_book`/`realtime_engine`/`backtest_engine` |
| 12 | 代码级审计（前视/零成本陷阱） | ✅ **早已落地** | AST 静态扫描 `lookahead_scan.py`，7 处合法未来引用显式豁免；**本轮新增模块也过了扫描**（0 命中） | `src/lookahead_scan.py`；`tests/test_lookahead_scan.py`；登记 `P2-LOOKAHEAD-SCAN` |
| 13 | 哈希链审计 | ✅ **早已落地** | `audit_chain` + 两个账本接入（`order_audit.jsonl` / `kill_switch_ledger.jsonl`）；**本轮的 deadman 账本同样走链** | `src/audit_chain.py`；`tests/test_audit_chain.py`；登记 `b3722be` 提交 |

## 三、P2（14-18）

| # | 优化项 | 落地 | 本仓实现 | 证据 |
|---|---|---|---|---|
| 14 | 标准化 CPCV（集成 purgedcv 替代自研） | ✅ **上轮落地** | `purged_cv.py`：PurgedKFold + Embargo + CPCV + Deflated Sharpe + MinTRL + PBO。**并列而非替换**既有 `pbo_cscv`，实测同块同值逐位相等 `0.528571428571` | `src/purged_cv.py`；`tests/test_purged_cv.py`（157 例） |
| 15 | LLM 驱动因子假设（假设级证据验证） | ✅ **落地并接线** | `factor_hypothesis.py`（lint→evidence→收益，未过前两步绝不进收益）+ **本轮新增收益评估接线与日更步骤** | `src/factor_hypothesis.py`；**`src/factor_hypothesis_eval.py`（新）**；`tests/test_factor_hypothesis_eval.py`（46 例）；真机：`return_eval_calls=2`，`f_mom_rev` IC=0.121/ICIR=0.565 通过、`f_vol` ICIR=0.275 判否 |
| 16 | 多策略组合回测（权重聚合） | ✅ **落地并接线** | 多腿权重聚合后走**同一个** `PaperBook`；**本轮新增取数适配层与日更步骤** | `src/portfolio_backtest.py`；**`src/portfolio_live.py`（新）**；`tests/test_portfolio_backtest.py`、`tests/test_portfolio_live.py`（30 例） |
| 17 | TWAP/VWAP 执行（大额拆单） | ◐ **算法已交付，未接实盘下单路径** | `exec_algo.py` 完整（拆单/参与率上限/滑点仿真，saving 可为负且不粉饰） | `src/exec_algo.py`；`tests/test_exec_algo.py`（178 例）。**缺口见第四节** |
| 18 | A2A 协议（Agent 间标准化通信） | ❌ **未做** | 本仓的多 Agent 协作走**进程内函数调用**（`factor_mad.AGENT_ROLES` + `agent_tools.execute_parallel`），没有跨进程/跨主机的 Agent 协议层 | `src/factor_mad.py`、`src/agent_orchestrator.py`。**建议不做**（见第四节） |

## 四、未落地项：为什么没做 + 要动哪里

### (a) 第 4 项缺口：降级驱动量里缺「拒单率」与「实测滑点」

现状：`health_state.assemble()` 的驱动量是**已有可观测量**（延迟、数据滞后、账户静态
估值、L3 事件、盘中数据停流），并把它们装配成单一状态。缺的两个因子**在本仓已经有
原料但没接**：

- **拒单率**：`data/order_audit.jsonl` 里有每条 `reject` / `pending_approval` 记录
  （含 `actor`/`reasons`）—— 按日聚合即可得到拒单率。
- **实测滑点**：`paper_book` 的成交记录里逐笔带 `impact_bps` / `exec_risk_bps`
  （`decompose_slippage` 的两个分量），已落在 `data/state.json` 的成交里。

**为什么本轮没做**：两者都需要一个**阈值**才能进状态机（"拒单率多高算降级"），而本仓
纪律是「阈值须基于下游表现」（method-1），不能拍脑袋定。正确做法是先**只采集不判定**
（与 `data_lag_days` 的处置一致）：把两个指标算出来进面板与复盘，累积分布后再标定阈值。
**要动**：`src/health_state.py`（新增两个采集项 + 指标）、`ops/alert_rules.yml`。

### (b) 第 8 项缺口：约束没钉在具体 Agent 上

现状：`is_read_only()` / `execute(forbid=...)` 已能机器判定与拦截，但**没有任何一处
代码强制**「风控角色只能用只读工具」。

**为什么本轮没做**：`factor_mad.AGENT_ROLES` 的四个角色（技术面/基本面/资金面/仲裁）
**全部**都是只读批判者 —— 也就是说，当前没有"有执行权的 Agent"可供收权，这条约束
暂时**没有可作用的对象**。强行加一层角色→权限表会是一张只有一行有效、其余靠自觉的
表，属"看起来做了安全"。

**要动**（当出现带执行权的 Agent 时）：在 Agent 构造处把 `role` 映射到允许的能力集合，
调用工具时统一走 `execute(require=..., forbid=...)`。能力表已就位，接线是几行。

### (c) 第 17 项缺口：拆单执行器未接实盘下单路径

现状：`exec_algo` 是纯规划+仿真，`realtime_engine` 仍是"一次性下单"。

**为什么没接**：接它要处理**跨 tick 的剩余未成量**（一个 5000 股的单拆 10 档，跨多个
tick 甚至跨日），这会改变下单时序与 `_to_used` 换手预算的记账口径 —— 属会改变生产
交易行为的改动。本轮已按你的要求把「虚拟盘 / run_daily / 收益评估」三处接完，拆单接线
是下一件需要你确认口径的事。

**要动**：`realtime_engine._rebalance()` 的下单点 + `PaperBook.buy/sell` 的逐档调用
（`plan['slices']` 与 `sim['per_slice'][i]['horizon_days']` 已可直接映射）+ 一个
"未成量"的持久化状态。

### (d) 第 18 项：A2A 协议 —— 建议不做

本仓的 Agent 协作是**同进程、同仓库、共享 DuckDB/h5i** 的紧耦合形态：`factor_mad` 的
辩论、`agent_orchestrator` 的并行工具调用、`hierarchical_agents` 的层级，全部靠函数
调用与共享文件系统通信。引入 A2A（跨进程/跨主机的 Agent 协议）在本仓**没有通信对象**
—— 它解决的是"多个独立部署的 Agent 互相发现与委派"的问题，而本仓只有一个部署单元。

**什么时候该做**：当真出现"外部的、独立部署的 Agent 需要调用本仓能力"时 —— 那时
**应该先接第 7 项的能力声明**（已就位）：把 `get_tool_schemas()` + `capabilities`
直接作为对外暴露的契约，比新造一层协议更贴合本仓。

## 五、本轮（2026-09-22 第二轮）新增交付

| 项 | 模块 | 测试 | 真机证据 |
|---|---|---|---|
| 虚拟盘卖侧闸门 | `src/live_gates.py` | 36 例 | 引擎接线 + 订单审计留痕 |
| 组合回测取数层 | `src/portfolio_live.py` | 30 例 | 129,421 行 frame / 9.2s |
| 因子收益评估接线 | `src/factor_hypothesis_eval.py` | 46 例 | `return_eval_calls=2`；IC=0.121/ICIR=0.565 |
| Dead-Man's Switch | `src/deadman_switch.py` | 38 例 | 空账本 exit 1 / 登记后 exit 0 / 超时 OVERDUE |
| 连接器能力声明 | `src/agent_tools.py`（改） | 29 例 | 20 工具全分类、0 个可下单工具 |
| run_daily 接线 | `src/run_daily.py`（改） | — | 三步：`portfolio_backtest` / `regime_scenarios` / `factor_hypotheses` |

**全量测试 1926 passed / 15 skipped / 0 failed**（本轮前 1767）。

### 接线时抓到并修掉的 6 个真 bug（都不是"测试写错了"）

1. **`factor_mine.evaluator` 指标读取路径错** —— ic_mean/icir/n_days 在**报告根层**，
   不在 `report['ic']` 里。按嵌套读 => 三个门槛全读到 `None` => **所有假设被静默判否**，
   看起来像"没有因子有效"。
2. **截面取数退化成上万次查询** —— 逐标的调 `close_prices_for`（它为单标的取全史）在
   "1000 只 × 25 天"下直接超时。改为一次 SQL 取矩阵后 **9.2 秒**。
3. **`change_pct` 口径判别在边界上判反** —— 用中位数 `> 1` 判断"百分数 vs 小数"，
   当数据恰好集中在 `1.0`（即 1%）时判成小数口径，复权序列被放大 100 倍
   （实测 fwd5 算出 **+3100%**）。改用**尾部量级**（p99.9，受涨跌停约束的物理量）。
4. **`data_lag_days` 是自然日不是交易日** —— 按 `== 0` 判定会让**每个周一/节后第一天
   拒绝一切买入**（静默停手）。改为默认"记数不判定"+ 显式严格开关。
5. **`max_single_weight`(8%) 是压回线不是下单前硬上限** —— 用它拒单会在目标池不足
   9 只时拒掉全部买单，且与 `target_weighting` 的 11.5% 单票目标自相矛盾。
   被 `tests/test_pending_orders.py` 的 9 个用例抓出（30% 大单本应转人工却被直接拒）。
6. **死手开关的 `level` 恒为 OVERDUE** —— `overdue if led else unknown` 里 `led` 是
   dict，只要账本有任何条目就为真，于是"从未 tick 过的组件"也被塞进 overdue，
   `UNKNOWN` 永不出现（空账本被读成"有组件失联"而不是"监控未生效"）。

## 六、复跑方式

```powershell
# 全量测试
.venv314\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp\run

# 路线图 ⑦ 项真机验证(只读)
.venv314\Scripts\python.exe scripts\verify_roadmap7.py

# 死手开关状态
.venv314\Scripts\python.exe src\deadman_switch.py --status
.venv314\Scripts\python.exe src\deadman_switch.py --flush-percent 1   # 首次上线登记起点

# 连接器能力审计
.venv314\Scripts\python.exe -c "import sys;sys.path.insert(0,'src');import agent_tools as a,json;print(json.dumps(a.audit_capabilities(),ensure_ascii=False,indent=2))"
```
