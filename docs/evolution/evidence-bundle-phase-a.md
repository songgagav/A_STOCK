# Evidence Bundle Phase A

本阶段只增加离线旁路证据层，不改变选股、排序、目标权重、撮合、交易、PaperBook、broker、realtime 或 DRL production 行为。

实现入口：

- `src/evidence_bundle.py`：显式输入、原子构建、不可变 finalize、完整性验证；
- `src/evidence_turnover.py`：四种换手口径；
- `src/evidence_cost.py`：逐笔成本回放与成本 provenance。

## Explicit input contract

下列内容保留 Phase A 的历史设计背景。当前新建 bundle 还必须显式传入
`data_lineage_identity`（source/schema/routing/universe/calendar_source/calendar_version/lineage_sha）
与完整 shadow/paper production state，使用 Observation Epoch v2；每日
`data_identity.data_sha` 与 artifact hash 保留在当天 bundle，不得进入稳定 lineage。
新建要求 `drl_plan_mode_contract=implemented_default_shadow`；旧 `not_implemented`
bundle 只走历史验证路径，不被覆盖或改写。详见
[当前 epoch 契约](oos-and-execution-evidence-phase-c.md#observation-epoch-binding)。

`EvidenceBundleRequest` 必须显式提供：

- `trade_day`、`generated_at`、`run_id`、`code_sha`；
- `data_identity.data_sha`、`config_identity.config_sha`、`experiment_identity.experiment_hash`；
- snapshot 和 `market_data`、`positions_before`、`positions_after`、`target_positions`、`orders`、`fills` 的路径、格式和 SHA-256；
- 固定的 `reference_equity` 和 `reference_timestamp`；
- `cost_evidence_level` 与当前生产状态。

builder 不扫描目录、不寻找最新文件、不调用系统日期，也不修改输入 artifact。

## Bundle lifecycle

构建顺序是：

1. 校验所有显式输入文件存在且匹配期望 SHA-256；
2. 在输出目录的临时目录写入 `raw/` 与 `derived/`；
3. 从同一份 raw evidence 计算 turnover 和 cost records；
4. 校验所有 constituent hash；
5. 生成 `manifest.json` 及其 `manifest_hash`；
6. 以目录 rename 原子 finalize。

finalized bundle 的目录名是由显式身份和 raw artifact hash 派生的 `bundle_id`。已有目录只允许完整性验证后幂等返回，禁止原地覆盖。相同交易日的重建必须使用新的 `run_id`，输入变化会产生新的 `bundle_id`。

目录结构：

```text
<output_root>/<bundle_id>/
├── manifest.json
├── raw/
│   ├── snapshot.json
│   ├── market_data.json
│   ├── positions_before.json
│   ├── positions_after.json
│   ├── target_positions.json
│   ├── orders.json
│   └── fills.json
└── derived/
    ├── turnover.json
    ├── cost_records.jsonl
    ├── cost_summary.json
    └── status.json
```

manifest 至少保存 schema/bundle/run identity、交易日、生成时间、代码/数据/配置/snapshot/experiment identity、constituent hash、builder version、evidence status、blocked reasons，以及：

```text
RANK_BY_FUSION=0
alpha_evidence_status=not_promotable
drl_plan_mode_contract=not_implemented
```

这里的 `drl_plan_mode_contract=not_implemented` 是事实记录，不代表本阶段实现了 `off|shadow|enforce` production gate。

## Status taxonomy

所有状态使用以下集合：

```text
available
pending_maturity
not_applicable
missing
blocked
invalid
tampered
```

例如尚未成熟的 120 日 forward observation 必须由调用方显式标成 `pending_maturity`，不能转写成 missing 或 failed。哈希不匹配属于 `tampered`；结构错误属于 `invalid`；缺少必需 artifact 属于 `missing`。

## Turnover contract

- `name_turnover`：只表示标的成员替换率，不能称为真实换手；分母为 `max_member_count`。
- `target_weight_turnover`：`0.5 * Σ|previous_actual_weight - new_target_weight|`，必须包含 `CASH`。
- `planned_turnover`：计划订单名义金额绝对值之和除以固定 `reference_equity`。
- `executed_turnover`：成交 fill 名义金额绝对值之和除以固定 `reference_equity`；没有 fills 时为 `null/not_applicable`，不写 0。

每个 metric 都保存 `denominator_type`、`reference_equity`、`reference_timestamp`。调用方不能替换 denominator。

## Cost contract

每个 cost record 保存 order/fill identity、decision/order/fill 时间和价格、数量、名义金额、佣金、印花税、过户费、滑点、market impact、opportunity cost、total cost、evidence level 和 source artifact。

证据等级只有：

- `estimated`：模型估算，没有实际 fill；
- `simulated`：来自 Paper/vn.py 等模拟 fill；
- `realized`：必须由每笔记录显式声明 `realized_fill=true`，否则整个 replay 为 `blocked`，不生成 realized 数字。

`opportunity_cost` 只有在显式 benchmark（decision/arrival/VWAP/TWAP/close/execution-window mark）及时间窗口存在时计算，否则为 `null` 并给出 reason code。

滑点和 market impact 的每个 component 都有 `embedded_in_fill_price` provenance。若已内嵌，则只用一次成交价相对 decision price 的 execution-price delta；若未内嵌，才计入显式 component。`opportunity_cost` 单独归因，不并入 `total_cost`，防止与成交价损耗重复计费。

## Deliberate non-scope

本阶段不接入 `run_daily`、selector、target plan、PaperBook、broker、realtime 或 DRL reward。下一步先用真实交易日验证 raw evidence 能稳定捕获连续 OOS、目标/实际持仓差异、planned/executed turnover 和 simulated/realized cost，再进入 Phase B。
