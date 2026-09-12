# Point-in-Time 估值数据说明

> 2026-09-12 建立。对应"优先补齐 point-in-time 数据，再做滚动样本外验证"的第一步。

## 实现

- 新增 `db._valuation_asof_h5i(as_of)`：从 `valuation` 表用窗口函数
  `ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC)` 取 **`ts <= as_of`** 的每
  symbol 最新一行（窗口 400 天），即严格 as-of 取值，**无前视**。
- `db._universe_asof_h5i(as_of)` 现把上述 PIT 估值 left join 进历史宇宙，新增列：
  `pe_ttm` / `pb` / `ps_ttm` / `total_mv` / `float_mv` / `float_shares` / `is_st`。
- 单位：`market_cap`/`free_cap` 原为"元"，统一转"亿"输出为 `total_mv`/`float_mv`。
- 消费方 `selector.RotationSelector._select_hist`（历史选股/回放路径）由此获得估值特征，
  使 `p08_signal.governance_score` 的 PB/PE 负向过滤与 `db.filter_universe` 的
  **流通市值过滤在历史回测中首次生效**，与实盘读 `valuation_snapshot` 的行为对齐。

### 此前的问题

| 路径 | 估值来源 | 问题 |
|---|---|---|
| 实盘选股 `get_universe()` | `valuation_snapshot` 最新完整快照 | 有估值，但 **`MAX(ts)` 属前视** |
| 历史选股 `get_universe_asof(d)` | 仅 `daily_bars`（价/额） | **无估值字段** → PB/PE 过滤与市值过滤失效 |

结果：回测与实盘存在特征分叉（同一策略在两边看到的信息不同）。现历史路径改为
PIT 正确的 `valuation` 逐日数据，分叉消除。

## 数据覆盖现状（实测）

| 字段 | 覆盖 | 说明 |
|---|---|---|
| `pb` | ~100%（含 2019） | 全期可用 |
| `free_cap` → `float_mv` | 2025-08 起 92~99%；**2025-06/07 为 0%**；2019 为 0% | 市值过滤在缺段不可用 |
| `pe_ttm` | 2019 ~86.6%；**2025-08 起骤降至 4~5%**；2026-09 回升 40.7% | 近一年 PE 过滤基本失效 |
| `market_cap` → `total_mv` | 近全缺（2026-08 起少量） | 已用流通市值兜底（近似） |
| `valuation` 逐日交易日 | 近 3 年 730 日，日均覆盖 5399 只 | PIT 骨架完整 |
| `valuation_snapshot` | 仅 3 天（08-31 / 09-04 / 09-08） | 当日快照用途，不用于历史 |

## 待补齐（下一步优先级）

1. ~~**`pe_ttm` 近一年缺口**~~ **已回补（2026-09-12）**：`scripts/backfill_pe_ttm.py`
   用东财个股历史估值接口（`ak.stock_value_em`）逐只拉取 PE(TTM)/市净率/市销率/市值，
   仅对缺 `pe_ttm` 的 (ts, symbol) 生成补丁，落 `data/pit/pe_patch/*.parquet`；
   读取端 `db._pe_patch_asof()` 按 `<= as_of` 合并（无前视）。
   原因：h5i `valuation` 主表 append 受"时间单调且 min(ts) ≥ 表 max(ts)"约束，且无公开
   建表 API，历史行无法回填，故采用"独立补丁 + 读取合并"。缺口规模：5320 只 / 135.7 万行。
2. ~~**`free_cap` 2025-06/07 与 2019 段缺口**~~ **已缓解（2026-09-12）**：①读取端
   已用 `float_shares × price` 复原流通市值（覆盖 0% → 99.8%，见 `_universe_asof_h5i`）；
   ②PE 补丁 parquet 同时携带 `free_cap`（东财历史估值，非空率 100%），
   `db._valuation_asof_h5i` 已合并补齐其覆盖区间。更早年份仍靠 ① 的兜底估算。
3. ~~**ST/名称过滤**~~ **已部分补齐（2026-09-12）**：历史宇宙无名称列（`valuation`
   无 `name`，symbols parquet 亦无 `name`），而 `valuation.is_st` 覆盖率仅 **6.2%**
   （近一年实测），故在 `filter_universe` 增加 `is_st` 分支——**有值且为 True 即剔除**
   （当日 PIT 状态，无偏、不引入后视），NaN 保留。完整历史 ST 名单需逐日名称历史，
   当前数据源不可得，属已知局限。
4. 补齐后再执行滚动样本外验证（`scripts/nonoverlap_rerun.py` + `src/overfitting_test.py`），
   产出 OOS 序列作为资金可用性判断依据。
