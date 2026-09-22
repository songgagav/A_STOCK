# 数据源路由与降级机制 — 设计与摸底

> 建档 2026-09-22。**本文先纠正设计里的若干假设, 再给可落地的形态。**
> 所有"实测"结论均可复核; 凡未验证的一律标注。

---

## 0. 结论速览

| 命题(来自提议) | 实测结论 |
|---|---|
| 「所有源都通过 `_write_to_h5i()` 进入 h5i」 | **原则对, 函数名不对** —— 该函数不存在。真实落点是 `h5i_sync.append_daily_bars()` + `_df_to_h5i_table()` |
| 「复用 `_write_to_h5i(df, source=...)`」 | 写入路径**不接受 `source` 参数**, 且 h5i `daily_bars` **没有 source 列** |
| 「free-stockdb 本地 parquet: `data/daily/xxx.parquet`」 | 本机**没有这个结构**; `stockdb/data` 是 **leveldb**(664 个 `.ldb`), 且**停在 09-03** |
| 「free-stockdb 与 stockdb 是两个可分别接入的源」 | 本机它们是**同一个东西**(已登记为"三库已知状态", 引擎实际走远端) |
| 「h5i-db CLI: `create-table` / `ingest`」 | **无此 CLI**; 本仓用 Python `h5i_db.Database` API |
| AData / Baostock | **均未安装**(`.venv310`:`adata=False, baostock=False`) |
| AkShare | 已装(`.venv310` 1.18.88), 口径已部分验证 |

**最重要的一条新约束(提议未覆盖)**: 见 §3 —— `append_daily_bars` 是**单调追加**,
它**只接受 `date > h5i 现有最大日期`** 的行。这直接决定多源路由**不能**用它回填缺口。

---

## 1. 真实的写入契约(实测)

```
数据源适配器  ->  归一化(_df_to_h5i_table)  ->  data_quality_guard 结构哨兵
                                                    |
                                                    v
                                   h5i_sync.append_daily_bars(df)
                                                    |
                                                    v
                                          h5i_db.Database.append("daily_bars", ...)
```

### 归一化契约(`_df_to_h5i_table`)

| 项 | 要求 |
|---|---|
| 列名 | `symbol, date, open, high, low, close, volume, amount, change_pct, turnover` |
| `symbol` | 字符串, **零填充到 6 位** |
| `date` | 可被 `pd.to_datetime` 解析 |
| 缺列 | 自动补 **float64 NaN**(不是 None —— 否则 pyarrow 推成 Null 类型与建表 schema 冲突) |
| `ts` | 由 `date` 派生, `datetime64[us]` |

### 两道闸门(提议未提及, 但必须保留)

1. **结构哨兵** `data_quality_guard.validate_daily_bars` + `gate_decision`:
   任何来源下都不可能合法的行(OHLC 关系/非正价格/负量/核心 NaN)一律**拒绝写入**。
   实测它拦的是存量零填充占位行(open=high=low=0 仅 close 有值)。
2. **单调追加**: 见 §3。

---

## 2. 各数据源接入现状(逐个实测)

| 源 | 状态 | 接入方式 | 备注 |
|---|---|---|---|
| **stockdb SDK** | ✅ 在用 | `stock_sdk.rd.vals("日k", sym_or_prefix, day)` | 主源; 实测可返回 09-22 真实行情 |
| **free-stockdb(本地)** | ⚠️ 与本机 stockdb 同源 | `stockdb/data` 是 leveldb, mtime 停在 09-03 | **不是** parquet; 已登记"不补"决策 |
| **AData** | ❌ 未安装 | `pip install adata`; `adata.stock.market.get_market(k_type=1)` | 需先验证可用性与限流 |
| **Baostock** | ❌ 未安装 | `pip install baostock`; `bs.query_history_k_data_plus(adjustflag=...)` | 无实时行情 |
| **AkShare** | ✅ 已装(1.18.88) | `ak.stock_zh_a_hist(adjust="")` | 口径已部分验证, 见下 |

### AkShare 口径(已实测, 可直接用)

| 字段 | 换算 |
|---|---|
| `open/high/low/close`、`涨跌幅` | **1:1** |
| `成交量` | **×100**(akshare 手 -> 引擎股); 实测比值 mean **100.0001** |
| `成交额` | **1:1**(元) |

抽样 60 只对比重叠日 09-18: OHLC/pct **15/16 逐值一致**, `amount` **16/16 为 1:1**,
缺口日 09-21/09-22 **32/32 可取**。

> ⚠️ 那 1 例(600177 `pct_chg` +0.49 vs −1.93)**已定性为复权口径差异**:
> 引擎自洽(`8.14/8.10−1 = +0.4938%`, 其 `pre_close` 为除权调整后),
> akshare 不复权用 8.30。**独立复核(qfq)未完成** —— akshare 限流。
> 故 `adjust` 口径必须在接入前**逐源确认**, 不能默认。

### 解释器约束(容易被忽略, 会直接卡住实现)

| 能力 | `.venv314` | `.venv310` |
|---|---|---|
| `akshare` | ❌ | ✅ |
| `h5i_db` | ❌ | ✅ |
| `stock_sdk` | ❌ | ⚠️ 需把 `stockdb/pybao` 加进 `sys.path` |

**⇒ 数据摄入必须跑在 `.venv310`**(或生产解释器)。任何新源适配器都受这条约束。

---

## 3. ⚠️ 关键约束: append 是单调追加, 不能回填缺口

`h5i_sync.append_daily_bars` 的实现:

```python
old = work[work["_d"] <= m]   # m = h5i 现有最大日期
new = work[work["_d"] >  m]
# old 被跳过并告警("跳过回填日期 ... 不破坏 h5i 单调性")
```

**后果(直接影响路由设计)**:

- 若主源已把水位推到 D, 而它**漏了 D 之前的某天**, 备源**无法**通过 append 补那一天;
- 故"主源失败 -> 备源兜底"这个降级链**只在"水位还没推过去"时成立**;
- 真正的历史缺口回填必须走 **`h5i_ingest.py` / `h5i_rebuild.py`**(存在, 见 §1 表格),
  这是**运维动作**, 不是日常路由动作。

**这带来一个必须显式设计的行为**:

> 降级到备源时, 若备源**缺**主源已有的水位之前的日期, 应**明确记录为"不可回填缺口"**,
> 而不是静默 append 失败/跳过。否则会出现"以为备源补上了, 实际什么都没写"。

建议: 路由层返回 `{source, appended, skipped_rows, unfillable_gap: [days]}`,
并把 `unfillable_gap` 非空视为**需要人介入**的信号。

---

## 4. 建议的落地形态(与提议的差异)

提议的 `bars_source_router.py` 骨架方向正确, 但建议按"**先契约, 后源**"的顺序做,
因为**每个源都要单独验证归一化与复权口径** —— 一次性接 5 个源会把风险乘 5。

### 阶段一(建议先做): 抽出源无关的写入契约

把 `engine_bars_sync` 里与"引擎"无关的部分抽成 `bars_ingest.py`:

```python
def normalize(df, *, source: str) -> pd.DataFrame:
    """任何源 -> h5i 契约(10 列)。含单位换算与复权口径的**显式声明**。"""

def write_bars(df, *, source: str, dry_run: bool = True) -> dict:
    """经 _df_to_h5i_table + 结构哨兵 + append_daily_bars 统一写入。

    返回 {source, appended, skipped_rows, unfillable_gap, structural_rejected}
    """
```

**关键**: `normalize` 必须**显式接收源名**并据此做换算(如 akshare 的 ×100),
而不是让每个适配器各自处理 —— 否则单位错误会以"数据看起来正常但差 100 倍"的形式潜伏。

### 阶段二: 接入**已有验证**的源

只接 **akshare**(口径已验证)。用它与引擎做**双源交叉校验**:
同一天两源都取, 比对 OHLC/量额; 不一致则**不写入**并告警。
这一步的价值是**证明路由框架本身可用**, 而不是急着扩源。

### 阶段三: 按需扩源

AData / Baostock 需先: ① 安装并确认可用; ② 单独验证复权口径与单位;
③ 用阶段二的交叉校验框架验收。**未验证的源不进路由表** ——
否则等于把"数据正确性"赌在未验证的第三方上。

---

## 5. 与既有机制的关系(提议提到, 逐条确认)

| 机制 | 是否受影响 | 说明 |
|---|---|---|
| `_data_version()` | 否 | 读的是 h5i 水位, 路由不改水位语义 |
| `data_lag_days` | 否 | 由 `freshness()` 算, 与写入路径无关 |
| 降级链 | **会变强** | 多源后 `freshness` 的 `calendar_strength` 仍独立于源 |
| `degrade_inputs` | 否 | 读 `order_audit.jsonl` 与 `trades_history` |
| 结构哨兵 | **必须保留** | 新源更容易带脏行, 哨兵是唯一防线 |

> 注: `source` 字段建议**落在 h5i 之外的旁路台账**(如 `data/bars_ingest.jsonl`),
> 而**不要**加进 `daily_bars` 表 —— 改表 schema 会牵动 `h5i_ingest`/`h5i_rebuild`/
> 奇偶校验与既有建表脚本, 风险远大于收益。

---

## 6. 建议的下一步(需用户确认)

1. **确认阶段一的范围**: 抽 `bars_ingest.py` 时不改任何现有行为(纯重构 + 测试锁住等价);
2. **确认是否接 akshare 做交叉校验**(它是唯一已验证的备源);
3. **确认 AData / Baostock 是否值得引入** —— 二者都需要新装依赖,
   且 `Baostock` 无实时行情(对本仓盘中链路无用, 只对历史补数有用);
4. **确认缺口回填走 `h5i_ingest`/`h5i_rebuild` 的运维口径**(它是手工动作, 不是路由动作)。
