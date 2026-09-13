# 标的代码形态约定（canon / symbol）

> 2026-09-13 统一。根因：`canon` 这个列名在仓库里**同时指代两种不同形态**，
> 跨表 join 时极易静默返回空结果。曾因此让 IC 计算只对齐到 `60/1563` 个标的
> （见 `docs/pit-valuation.md` 第 8 条），数值完全失真且不报错。

## 两种形态

| 形态 | 样例 | 出现位置 |
|---|---|---|
| **带后缀 canon** | `600519.SH` | `StockDB.universe()`、`selector` 输出、`realtime_engine`、`vnpy_backtest` 目标池 |
| **纯 6 位 canon** | `600519` | `build_factor_views` 写入的 h5i views（`v_factor_scores_daily` 实测 **0% 带后缀**） |
| **db `symbol`** | `600519` | h5i 各表（`daily_bars` / `valuation` / `financials` …） |

后缀取值：`SH` / `SZ` / `BSE`（北交所，由 `_db_to_canon` 按 `symbols.market` 判定）。

## 规则

1. **db 表的 `symbol` 一律纯 6 位**；任何带后缀的代码在查库前必须转换。
2. **跨表 join 不得依赖列名相同**：只要两侧可能来自不同形态，一律显式转换。
   统一入口：`db.to_sym6(x)`（任意形态 → 纯 6 位，含补零）或 `db._canon_to_db(x)`。
3. 新增字段若承载带后缀代码，**必须命名为 `canon`**；若承载纯 6 位，**必须命名为 `symbol` / `sym6`**。
   不要再用 `canon` 指代纯 6 位（历史遗留，`build_factor_views` 是唯一例外）。

## 全链路审计结果（2026-09-13）

| 位置 | 形态 | 结论 |
|---|---|---|
| `build_factor_views.py:620/779/810` `left_on="canon", right_on="symbol"` | `canon` 为**纯 6 位** | ✅ 安全（实测 views 的 canon 0% 带后缀） |
| `db.py:117/235/262/269`、`factor_fusion.py:196`、`market_panel.py:116` | 两侧均纯 6 位 | ✅ 安全 |
| `StockDB.universe()` → 消费方 | 带后缀 | ✅ 查库前经 `_canon_to_db` |
| `scripts/` 下新增脚本 | — | ⚠️ 曾违规 1 处：`factor_ic_forward.py` 直接 join（60/1563 对齐），已于 2026-09-13 修正为 `sym6` |
| 其他 `scripts/*.py` | — | ✅ 全量 grep 无 `canon` 跨形态 join |

守卫用例：`tests/test_symbols_contract.py`（round-trip、BSE 处理、补零、混合形态归一）。
