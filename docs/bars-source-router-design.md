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

---

## 7. 实施进度

### ✅ 阶段一已完成(2026-09-22)

`src/bars_ingest.py` 已落地, **纯重构、行为等价**(等价性由测试锁住, 见下)。

| 交付物 | 说明 |
|---|---|
| `SOURCE_SPECS` | **声明式**源规格: 字段映射 / 符号列 / 日期格式 / **单位换算** / 残截面阈值。**未登记的源一律拒绝** |
| `normalize(df, source)` | 任意源 -> h5i 十列契约。**幂等**(原始记录与已归一化表都能吃) |
| `write_bars(df, source)` | 统一写入: 归一化 -> 两道闸门 -> h5i; 返回 `unfillable_gap` |
| `account_gap(days, h5i_max)` | 缺口分类: `appended_days` vs `unfillable_gap`(**带运维口径 action**) |
| `fetch_and_ingest(date, source, fetcher)` | 路由层契约: `status ∈ {appended, unfillable_gap, failed}` |
| `engine_bars_sync.fetch_day` | 已改为**委托** `bars_ingest.normalize`(残截面闸门仍留适配器, 见下) |

**等价性验收(用户给定标准)**: `tests/test_bars_ingest.py` 把重构**前**的归一化逻辑
逐行抄录为 `_legacy_normalize`(基准), 与 `bars_ingest.normalize` 对同一批数据比对,
`assert_frame_equal(check_exact=True, check_dtype=True)` **逐位一致**;
并用**真实引擎数据**(5481 行)再验一遍, 且断言比对**非平凡**(行数 > 1000,
且故意篡改一个值必须能比对失败)。

### 重构中实测踩到并修掉的三个问题(留档)

1. **`normalize` 非幂等** —— 适配器(`fetch_day`)**先归一化**再交出,
   而 `field_map` 期望源列叫 `code`; 已归一化的表里没有 `code`, 于是
   `code -> None -> astype(str) -> "None"` -> 只剩 1 行 -> **归一化出 0 行**被闸门拒。
   实测 `write_bars(fetch_day('20260922'), 'stockdb_sdk')` 报「仅 0 行」,
   而 `fetch_day` 明明返回 **5481** 行。**只有测"适配器产物 -> write_bars"才能发现**,
   只用原始记录的单测永远测不出来。已修(幂等入口)+ 加回归。
2. **异常类型被改** —— 首版让 `normalize` 用引擎阈值校验, 使 `fetch_day` 的异常从
   `EngineUnavailable` 变成 `ValueError`, 弄红两个既有用例。
   修法: **残截面闸门留在各源适配器**(失败语义属于适配器, 不属于共享归一化层),
   `normalize` 只做归一化。
   ⇒ **"行为等价"含异常类型** —— 这是"重构不得改 API"的具体形态。
3. **`field_map` 方向搞反** —— 映射是 `{源列: 目标列}`, 首版写成 `fm.get("date")`,
   对 `stockdb_sdk` 恰好成立(其源列名就叫 `date`), 对 akshare 直接失败(源列名是 `日期`)。
   这种"只在一个源上碰巧对"的写法正是本模块要消灭的耦合。

### 顺带: 全仓语法编译守卫

用户要求把「生成含大段中文的脚本时先跑 `ast.parse`」固化成规范。已做成
`tests/test_python_syntax_guard.py`(**跑全仓每个 .py** 的编译检查), 而不是只写进文档 ——
"规范"要靠人记得执行, 而本次会话**连续四次**都说明记不住。

它立刻抓到两个真问题:
- `scripts/_tmp_fork_safe.py`(2026-09-18 的一次性脚本, 未跟踪)**语法错误**
  (docstring 用 `*/` 收尾) —— 顺带发现 `scripts/` 下堆积了 **25 个** 未清理的
  `_tmp_*.py`, 已整体归档到 `data/_quarantine/tmp_scripts_20260922/`;
- `export_astock_bars.py` / `export_trade_calendar.py` 的 `SyntaxWarning`:
  docstring 里的 PowerShell 续行 `\` 被当成 Python 转义 —— 已改为 raw string。

### ⏳ 阶段二 / 阶段三(待 akshare 限流恢复)

阶段二: 接 akshare(唯一已验证备源)做**双源交叉校验**;
阶段三: 评估 AData / 引入 Baostock。**未验证的源不进路由表。**

---

## 8. Baostock 验证结果(2026-09-22)—— 结论: 定向补数, 不做每日主源

用户提议引入 Baostock 做历史补数。**已逐项实测**, 详见
`docs/stockdb-source-status.md` §6。要点:

### ✅ 口径比 akshare 更好

8 只标的 × 3 日与引擎比对: **OHLC 逐值一致**; **volume 比值 = 1.0000**
⇒ 单位是**股**, 与引擎同口径, **不需要换算**(akshare 是手, 必须 ×100)。
⇒ **它是比 akshare 更合适的交叉校验源**: 口径一致, 差异即真异常。

### ⚠️ 纠正提议两处

1. **`adjustflag`: 实测 `1=后复权 / 2=前复权 / 3=不复权`**。提议写"2=前复权(推荐)",
   而**本仓对齐口径是 `3` 不复权**(引擎 09-18 浦发 close=9.07 与 flag=3 一致)。
   小样本上 flag=2 与 3 **恰好相同**(区间内无除权), 所以这条错误**只有在大样本/
   含分红送转的区间才会暴露** —— 典型的"小样本验证通过、上线才炸"。
2. **它是逐 symbol 接口, 无法按日批量**: `query_all_stock` 返回 7393 行但**只有
   `code/tradeStatus/code_name`, 不含 OHLCV 也不含 `type`**。
   ⇒ 全市场每日 = **逐只 5481 次请求**。

### 吞吐实测(无间隔连打 20 只)

**20/20 成功, 150 ms/只, 零限流**。推算全市场: 无间隔 **13.7 分钟** /
1.5s 间隔 **2.28 小时**。识别个股还需再花 6897 次 `query_stock_basic`(排除 ETF/指数)。

⇒ **每日全市场摄入不可行**(收盘管道 19:10~22:00 还要跑选股/回测/训练);
**定向补数与交叉校验完全可行**。

### ⚠️ 覆盖缺口: 北交所无数据

`bj.*` 代码 `query_stock_basic` 返回空 ⇒ **只覆盖沪深**。h5i 里的 `4*`/`8*` 标的补不了。

### 分工(修订版)

| 场景 | 推荐 |
|---|---|
| 定向历史补数 / 缺口回填 | **Baostock**(口径逐值一致, 无需换算) |
| 双源交叉校验 | **Baostock**(优于 akshare: 无单位/复权干扰) |
| 每日全市场摄入 | **维持引擎**(Baostock 逐只太慢) |
| 实时 / 另类数据 | 引擎 / AkShare(Baostock 无实时、不覆盖龙虎榜) |

### 已落地

`baostock` 已登记进 `SOURCE_SPECS`(`volume_mult=1.0`, `symbol_strip_prefix=True`),
并有 `TestEveryRegisteredSourceSatisfiesTheContract` 对**每个已登记源**自动验契约。

---

## 9. ⚠️ 因子计算纪律: 价格衍生指标**取自源, 不得自算**(强制)

### 为什么这是一条**纪律**而不是一个建议

实测 600177 @ 2026-09-18(**含除权**): 前一日 close = **8.30**, 当日 close = **8.14**。

| 来源 | 值 | |
|---|---|---|
| **自算** `(8.14/8.30-1)` | **−1.93%** | 用**未调整**前收 ⇒ **错** |
| **引擎** `pct_chg` | **+0.49%** | 基于除权调整后 `pre_close = 8.10` |
| **baostock** `pctChg` | **+0.4938%** | 与引擎一致 |

**最危险之处**: `−1.93%` 是一个**完全合理的涨跌幅** —— 不触发异常、不看起来可疑,
只会让当天所有价格衍生因子悄悄偏掉。而且**只在除权日发生**(平时两者相同),
**用近端样本测不出来**。

### 强制手段(三层, 已全部落地)

1. **请求层**: `baostock_adapter.REQUIRED_FIELDS = (pctChg, turn, preclose)`;
   字段集缺任何一个 ⇒ `make_baostock_fetcher` **直接 `ValueError`** 并说明原因
   (宁可响亮失败, 也不自算)。
2. **映射层**: 每个源的 `field_map` 必须把源的涨跌幅列映射到 `change_pct`
   (baostock `pctChg` / akshare `涨跌幅` / 引擎 `pct_chg`);
   且 `normalize` 内**禁止**出现任何价格推导(`pct_change(` / `shift(1)` / `pct_chg =`),
   由 `test_normalize_never_derives_change_pct` 静态扫描锁住。
3. **校验层**: `cross_validate` 逐值比对 `change_pct`;
   `test_selfcomputed_value_would_be_caught` 反证 —— 把自算的 −1.93 喂进去
   **必须被判为不一致**(判据真的有效, 而不是形同虚设)。

### 推广

这条纪律**不限于涨跌幅**: 换手率(`turn`)、复权因子、任何"可由价格推导"的量,
都应**取自源**。理由一致 —— 只有源知道**除权/停牌/复牌**这些事件,
而"从价格反推"永远缺这部分信息。

---

## 10. 北交所 339 只: 补数路径待定(独立议题)

`symbols.parquet` 里 `market='bj'` 共 **339 只**(代码段 `920xxx`, 实测)。

| 事实 | 依据 |
|---|---|
| Baostock **不覆盖**北交所 | `query_stock_basic('bj.430047')` 无数据; `query_history_k_data_plus('bj.920000', ...)` 返回 **`error_code=10004011 股票代码未标识sh或sz`** |
| AkShare 是否覆盖 | **未验证** |
| 引擎是否覆盖 | 部分 —— h5i 09-22 全市场 5481 行里含 339 只 `920xxx`(即引擎**有**北交所数据) |

⇒ **不需要为北交所换主源**: 引擎已覆盖。Baostock 作为**补数/校验**源时,
北交所是它**原理上的缺口** —— 已由 `classify_targets` / `fetch_range` 显式报为
`unfetchable`/`not_requested`(**不得**混进 `failed`, 否则每天误报故障)。

**待决策**: 北交所若真需要 Baostock 之外的补数源, 走 AkShare(需先验证覆盖)
还是 ops 口径(`h5i_ingest`/`h5i_rebuild`)。已登记为独立议题。



