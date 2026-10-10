# Pre-Observation Hardening 验收报告（2026-10-10）

## 基线与交付边界

本轮独立分支为 `feature/pre-observation-hardening`，精确基于 PR #17 的
`17b7bf2a8ba7b5c687215abf40957c3c48a3f777`。PR #17、main、主工作树、历史
Evidence 与运行数据均不修改；不合并、不关闭 PR、不 force-push。

候选代码 SHA：`395d99980d32e48b7f5fbb1ff004f4775dcb9e1a`。推送后仍须以 PR 同一 SHA
的 GitHub checks 为准；本地 Python 3.10 结果不冒充远端 Python 3.11/3.12 结果。

## T01–T10 对照

| 任务 | 实施内容 | 当前状态 |
|---|---|---|
| T01 | naive 按上海时间解释、aware 转换；09:25 实际持久化 ready snapshot | 实施及独立审查通过 |
| T02 | 逐代码真实来源、参考价不冒充实时、持仓降级；canonical snapshot day/schema/hash 回执，禁止 DRL fallback | 实施完成；两轮转换修补自审及定向回归通过，最终独立复审代理触及用量上限；GitHub checks 待运行 |
| T03–T05 | Epoch v2 绑定稳定 lineage/production state；日 artifact hash 分离；新建要求已实现 shadow，旧 artifact 只验证 | 实施及独立审查通过 |
| T06 | 历史 PPO 中性情绪/原始 prior；learned/version 与 inference overlay 分离；未知日期保持未知 | 实施及审查补修通过，独立复审通过 |
| T07–T09 | 三权归一化、attr 全链路、atomic config、next-session/pending、历史 future-config 防护、降级 trace | 实施完成；审查发现逐项修复并自审，最终独立复审代理触及用量上限 |
| T10 | 单一已证明购入基础按 ex_date-buy_date；aggregate/legacy/非法日期显式 unknown/approximate | 实施完成，独立复审通过 |

## 验证记录

- T01：RED 6 failed；GREEN 19 passed；相关 39 passed。
- T02：初始回归 144 passed/1 skip；实际后端 RED 14 failed/2 passed；GREEN 160 passed/1 既有运行文件 skip；状态转换 RED 3/GREEN 133；缺价候选 RED 1/GREEN 134。
- T03–T05：新 API RED 25 failed；artifact 集成 RED 14 failed；manifest 绑定 RED 2 failed；legacy 补修 RED 2 failed；最终 59 passed，独立审查通过。
- T06：隔离 RED 3 failed；CI 分区/provenance RED 2 failed；Value metadata RED 1 failed；初始 37 passed/1 既有 SB3 warning。provenance 补修 RED 2 failed/GREEN 22 passed，独立复审通过。
- T07–T09：初始 RED 14 failed；future-config RED 1 failed；审查修补 RED 7、日历身份 RED 1、parser/empty-component RED 2；最近联合 focused GREEN 44 passed。最终独立审查代理触及用量上限，controller 自审。
- T10：GREEN 76 passed，无新增 warning；独立复审通过。
- 最终稳定代码 core：**2,805 passed, 38 skipped, 4 warnings，exit 0**。
- 最终稳定代码 DRL：**173 passed, 9 warnings，exit 0**；h5i：**32 passed，exit 0**。
- Bandit blocking `-ll -iii`：**PASS**；`secret_scan.py`：**PASS**（549 files/0）；tracked detect-secrets：**PASS**（0 finding files）；敏感配置历史路径检查无命中。
- pip-audit：**PASS**，No known vulnerabilities found（requirements.txt 未改）。

## 关键裁定与限制

1. 按用户反复执行/上传授权及确认继续，不追加重复权限或设计 gate。只上传新分支/创建可审查 PR，不推断合并或部署授权；风险由隔离分支与审查控制。
2. 实施者与 controller 采用不重叠文件切片；任一时刻只有一个实施子代理。controller 完成 Epoch/reward/brief，其余独立审查；最终跨任务回归检查整合风险。
3. 使用重点 RED/GREEN，加一次最终完整矩阵；baseline 已验证，不重复全量安装/回归。远端不同解释器由 GitHub CI 验证。
4. StockDB 仅加实际 backend attrs：配置 H5i 可以实际回退 DuckDB。未知标签记录 unknown_reference，而非按 BAR_STORE 猜来源；值、查询、路由/候选范围不改变。Sina spot 保留真实名称。持仓 unknown/reference 降级，reference 的 data_ts 为 null；updated_at 仅表示写状态。
5. snapshot 回执限定 canonical 路径，通过原 schema/hash 校验；复制到非 canonical 路径的文件不自动成为权威。回执无效不退 DRL。没有扩展 watchlist 或全市场 H5i fallback。
6. 新 Epoch API 显式要求稳定 lineage；未知字段禁止，避免把 daily hash 塞入 epoch。历史 v1/pre-epoch 不迁移不覆盖；新建要求 implemented_default_shadow。
7. reward 旧文件缺生效语义时降级到归一化默认，不猜日期。日历使用命名权威来源/现有 published-future-session 不变量，历史数据推导日期不当权威。未知日历保持 pending，需权威证据后重新生成；本轮不构建完整历史 reward config ledger。
8. 当前 brief 只属于 inference；未知有效日期不填 requested day。生产 promotion gates 不变。没有发明 normal pipeline 的 PPO reward components，也未改网络/reward 算法。

## 尚未完成的外部验收

- 推送分支并创建针对 PR #17 分支的审查 PR；同一 SHA 的 GitHub CI/security 检查待运行。
- PR 整合与实际部署 SHA 选择、解释器/PID 身份验证。
- 在该部署身份下冻结 Observation Epoch v2、配置/lineage/production state。
- 冻结后至少五个连续合格真实交易日的不可变 Evidence/OOS/持仓换手/逐笔成本与差异审查。当前新 epoch 的合格天数为 **0**；旧快照、测试、回放都不计入。
- PR #13 Router、PR #15 无关 Dashboard/runtime/vendor 二进制及其来源/许可仍独立审查。
- 完整 lot ledger/历史分红权益（含除权后加仓对旧持仓权益的影响）、生产 DRL/Fusion、券商接入与自动 promotion 不在本轮。

所有模式保持 `RANK_BY_FUSION=0`、`DRL_PLAN_MODE=shadow`、
`FUSION_WEIGHT_MODE=shadow`、`TRADE_BROKER=paper`、Alpha `not_promotable`。
详见 [冻结与真实观察 runbook](pre-observation-hardening-runbook.md)。
