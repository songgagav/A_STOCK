# P0 跟进执行状态

记录日期：2026-10-02  
分支：`evolution/p0-regime-position`  
本轮基线：`f548738`  
本轮状态文档生成前最新提交：`2b5c4a2`

## 本轮已完成

| 项目 | 结果 | 证据 |
|---|---|---|
| 跨日回退年龄观察 | 已完成 | `signal_freeze_watch.trading_day_distance`；`stale_fallback` 只读告警；交易日历不可用时不猜测 |
| 目标池来源接线 | 已完成 | `realtime_engine._trace_targets` 将来源日/消费日/交易日历传入观察器，不改变选池顺序 |
| P0-2 诊断脚本 | 已完成 | `scripts/research/p0_2_diagnose.py`；兼容当前字典交易记录与旧二元组记录 |
| P0-2 诊断结论 | 未闭环 | 当前无 09-02 独立成交台账；聚合成交与候选池 0 重合，不能反推历史实际池 |
| 安全扫描记录 | 已完成 | [`docs/cleanup/env-blockers.md`](../cleanup/env-blockers.md) 已更新为本轮真实结果 |
| 文档/诊断回归 | 已完成 | `95 passed`；Task 1 的目标池/冻结回归 `50 passed` |

## 仍未完成与卡点

### P0 / 生产行为

- **P0-2 回测-模拟盘一致性**：仍有 1.02pp 历史偏差；`fidelity_compare --explain 2026-09-02`
  正常退出但没有该日可用解释点。卡点是原始 09-02 成交/来源档位已不可恢复，不能靠当前聚合台账拟合。
- **P0-3 实盘风控接线**：−8% 仍是暂停加仓，不是清仓；CVaR/波动率熔断尚未接入
  `realtime_engine`。卡点是需要确定降仓/清仓策略和阈值后才能改变交易行为。
- **09:25 硬冻结**：当前只有纯告警版本；硬截止会改变信号路径，等待明确策略决策。
- **真实券商对账**：当前没有真实券商通道，只能证明 PaperBook/vnpy 模拟层逻辑。

### 数据、运行与验收

- h5i `drop_table → append` rebuild kill 窗口尚未验证。
- PaperBook snapshot 的现金恢复仍有 `1.05e-4` 元精度差异。
- Baostock 自动回填已接线，但 `run_daily → 自动触发 → 写入 → 留痕` 尚未真实跑通。
- DRL 的完整真实训练/target plan 生成仍需同时具备 `h5i_db` 与 `torch` 的解释器；经验回放、灾难性遗忘和奖励黑客防护仍未完成。
- E2E 门控状态记录仍为 7/10 断言通过。

### 清理与安全

- Phase 12 配置删除尚未执行；`config_usage.txt` 当前不存在，不能安全分级删除。
- Bandit：0 high，但 `-ll` 扫描仍有 126 medium/343 low，需逐项分级，不能标记为全通过。
- detect-secrets：工具可用但本轮扫描超过 90 秒无输出，中断，无结论。
- pip-audit：工具可用但等待外部漏洞源超过 90 秒，中断，无结论。
- `docs/vulnerability-register.md` 仍需根据本轮新证据重新生成；不要直接手改生成文件。

### 进化路线

- regime 路由和动态仓位仍是 shadow/research，缺长期、多状态、样本外验证，尚未生产启用。
- MCTS 因子挖掘、多 Agent 因子研发、MoE 因子路由尚未实现。
- DRL 换手/跟踪误差约束、滚动训练、Banach 自融资投影尚未完成或验收。
- 统一 `compute(data) -> signal/weight` 契约和完整决策审计链尚未完成。
- 财报季因子切换和分钟级高频因子体系尚未建设。

## 下一步安全顺序

1. 继续收集带完整 `targets_source` 留痕的新一轮回放/模拟数据，关闭 P0-2。
2. 明确 P0-3 的 −8% 行为和 CVaR/波动率阈值，再单独建变更批次。
3. 将 detect-secrets 拆分为可完成的工作树扫描与 Git 历史扫描；准备离线/镜像漏洞源后重跑 pip-audit。
4. 生成并审阅 Phase 12 配置候选清单，逐项测试后再删除。
5. 通过上述验收后，再推进 regime 生产路由和 P1 因子挖掘。

本轮没有 push、合并 PR 或修改真实行情/持仓数据。
