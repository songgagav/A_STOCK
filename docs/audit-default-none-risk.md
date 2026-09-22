# 「取不到输入 ⇒ 风控静默跳过」全仓审计（只读）

审计对象：`D:\狗屁通のA大奇妙冒险\A_stock_rotation`（下称 repo）。
审计范围：`src/` 全部 Python（含 `factor_mine/` 子包）、`scripts/`、根级 `.py`；排除 `.venv314/`。
审计方法：全量候选枚举（`getattr` / `.get()` / `param=None` / `except: pass|continue` / `if x is None: …`）→ 逐条回到定义处核对「该字段在生产路径上到底取不取得」→ 用可执行复现确认默认行为的后果。
**未修改任何文件**；本报告是本次唯一新建的文件。

---

## 0. 复现锚点（重要：审计期间仓库在被并发修改）

| 项 | 值 |
|---|---|
| `git rev-parse --short HEAD` | `82947d3`（2026-09-22 19:04:18 `chore: 清理临时文件`） |
| `git status --porcelain` | 审计开始时刻：` M src/health_state.py`、` M src/run_daily.py`、` M src/vnpy_backtest.py`；`?? src/datasource_gate.py`、`?? tests/test_datasource_gate.py`。审计结束时刻又新增 ` M ops/alert_rules.yml`、` M src/metrics_server.py`、`?? docs/impact-20260922-fills.md`、`?? _tools/register_gate.py` 等 —— 即另一个会话**整个审计期间都在写**。本轮审计自身**未修改任何已跟踪文件**，只新增了本报告（`?? docs/audit-default-none-risk.md`） |
| 审计时刻 | 2026-09-22 19:4x（工作区**正在被另一个会话改动**：`src/health_state.py` 在我读取过程中从 325 行变为 362 行） |

因此本报告的行号锚定在**下列文件哈希**对应的内容上（SHA256 前 16 位）：

```
1E1BD753D994523A  src/realtime_engine.py
554F4DFB313C73C7  src/pretrade_gates.py
B2EE0AE93DCFC44C  src/pretrade_compliance.py
FF0B4FE0AE25B6AA  src/paper_book.py
7687AFB2D54D6886  src/config.py
DFFE1309A9F617A6  src/live_gates.py
EFEDE3F13CBBE8BC  src/exec_gate.py
7578C42A1C69150C  src/health_state.py
A3496B304C14EC31  src/flow_watchdog.py
726A2358285AA603  src/run_daily.py
```

复算：`Get-FileHash -Algorithm SHA256 src\realtime_engine.py,src\pretrade_gates.py,...`
若哈希不符，请以本报告每条的**代码原文**为准重新定位行号。

---

## 1. 结论摘要

| 分类 | 数量 |
|---|---|
| **真风险**（同时满足：生产路径上确实取不到 + 默认行为是放行/跳过风控） | **7** |
| 已核查**非风险**（用过 `getattr`/`.get()`/`except: pass` 但不构成风险，逐条给理由） | **21** |
| **无法判定**（需运行期/外部事实才能定性） | **5**（另 1 条附注） |

真风险按严重度：

| 编号 | 严重度 | 位置 | 一句话 |
|---|---|---|---|
| R1 | **P0** | `src/realtime_engine.py:1119-1133` × `src/pretrade_gates.py:112-124` | 清单第 2 项「组合回撤」在生产路径**永远 skip**（ctx 从不传 `drawdown_pct`/`peak_equity`），`_pb_equity()` 修复并未救活它 |
| R2 | **P0** | `src/pretrade_gates.py:93-100, 241` | 清单第 1 项「单笔仓位上限」的阈值 `pretrade_max_position_pct` 在 `config.PAPER` 里**不存在**（只有注释），该项**永远 skip**；实测 9.8 倍权益的买单仍 `ok=True` |
| R3 | **P0** | `src/realtime_engine.py:1121-1133` × `src/pretrade_compliance.py:149-157` | ctx 不传 `max_pos`（`PAPER` 无该键），**「单笔超一个等权槽位」高危判定永不触发** ⇒ 巨量单不进人工审批队列；`_pb_equity()` 修复后唯一能兜住它的那项仍是死的 |
| R4 | **P0** | `src/paper_book.py:957-964, 1053-1060`（变体：哨兵值而非 None） | 生产主源 `_fetch_sina_spot` **硬编码** `volume=-1` + `suspended=False`，而 `suspended` 判据只在 `volume>0` 时才判 ⇒ **「停牌不可交易」在生产路径上恒放行** |
| R5 | **P1** | `src/realtime_engine.py:1129` × `src/pretrade_compliance.py:120-125` | ctx 不传 `min_cash`，`check_order` 的**现金底线合规项永不触发**（同规则另在引擎 1084-1089 兜底，故不升 P0，但订单级合规留痕缺失） |
| R6 | **P1** | `src/realtime_engine.py:1047-1120`（唯一调用点在买循环内） × `src/pretrade_compliance.py:397-402` | `gate()` 的**卖单分支在生产上没有任何调用者**（调用点硬编码 `side="buy"`，三条卖出路径 `:926/:959/:982` 都直接调 `pb.sell`）⇒ 卖出单**完全没有订单级审计**，`VIOL_SELLABLE`/`VIOL_PRICE`(卖) 是死代码 |
| R7 | **P1** | `src/pretrade_gates.py:127-136` × `src/factor_gate.py:462-463, 535-536, 640-649` × `src/realtime_engine.py:770-774, 793-796` | IC 门控的三个"未生效"档位 `unknown`/`disabled`/`unavailable` 被清单判成 **PASS**（不是 skip/unknown）⇒「门控降级/没跑起来」显示为"通过" |

> **与 2026-09-22 事故的关系（本次最重要的发现）**：R1+R2+R3 意味着——事故里被点名的「单笔仓位上限」与「组合回撤」两项，**在 `_pb_equity()` 修复之后仍然从未生效**。`_pb_equity()` 修好了 `equity`（它只是 R2 的"计数字段"与 R1 的一半输入），但：
> · R1 需要的是 `peak_equity`/`drawdown_pct`，**这两个字段仍然没进 ctx**；
> · R2 需要的是**阈值本身**，`config.PAPER` 里根本没有这个键。
> 直接后果（下方 §2 的可执行复现）：在**逐字复刻的生产 ctx** 下，一笔 **999,000 元（账户权益 101,760 元的 9.8 倍）**的买单，清单返回 `ok=True`、`failed=[]`。

---

## 2. 真风险逐条

### R1 — P0：交易前清单「组合回撤」在生产路径永远 skip

**位置：** `src/realtime_engine.py:1119-1133`（ctx 构造） × `src/pretrade_gates.py:112-124`（判定）

ctx 原文（`src/realtime_engine.py:1120-1133`）：

```python
_g = _PC.gate(
    {"symbol": canon, "side": "buy", "qty": qty, "price": pr},
    {"tradable": tb,
     "position_qty": (self.pb.positions.get(canon) or {}).get("qty"),
     # equity: **PaperBook 没有这个属性** ...
     "equity": self._pb_equity(),
     "cash": getattr(self.pb, "cash", None),
     "regime": (self._gate or {}).get("regime"),
     "freeze_new_buys": (self._gate or {}).get("freeze_new_buys"),
     "data_lag_days": _lag,
     "day_start_equity": getattr(self, "_day_start_eq", None)})
```

判定原文（`src/pretrade_gates.py:112-124`）：

```python
dd = _num(c.get("drawdown_pct"))
if dd is None and equity and _num(c.get("peak_equity")):
    pk = _num(c.get("peak_equity"))
    dd = (equity / pk - 1.0) * 100.0 if pk > 0 else None
if max_drawdown is None:
    add(CHK_DRAWDOWN, SKIP, "未给回撤上限(阈值缺省不判定)")
elif dd is None:
    add(CHK_DRAWDOWN, SKIP, "缺 drawdown_pct/peak_equity, 无法算组合回撤")
```

**(a) 生产路径取不到的证据**

1. ctx 的键集合是封闭的 9 个键（上面原文逐字可数）：`tradable / position_qty / equity / cash / regime / freeze_new_buys / data_lag_days / day_start_equity`。**没有 `drawdown_pct`，也没有 `peak_equity`。**
2. grep 全仓 `(pretrade_compliance|_PC|PC)\.gate\(` 只有**一个生产调用者**：`src/realtime_engine.py:1119`（另两处是 `scripts/verify_roadmap7.py:465` 的预检与 `pretrade_gates.py:10` 的注释）。没有任何别的调用方补这两个字段。
3. 字段**在账本上是存在的**，所以这是"没接线"而不是"没数据"：
   - `src/paper_book.py:41` `self.peak_equity = float(init_capital)`（属性存在）
   - `src/paper_book.py:479` `apply_risk_controls()` 返回 `"drawdown_pct": round(dd * 100, 2)`
   - `src/paper_book.py:554` `snapshot()` 里也返回 `"drawdown_pct"`
   - 引擎自己**已经拿到**了这个回撤：`src/realtime_engine.py:987` `risk = self.pb.apply_risk_controls()`、`:990` `f"组合回撤={risk['drawdown_pct']}%"` —— 也就是说，**同一个值就在同一函数的局部变量 `risk` 里，只是没有传进闸门。**

**(b) 被跳过的是哪个风控 / 后果**

- 被跳过：交易前清单第 2 项 `portfolio_drawdown`（阈值 `max_drawdown` **是有的**，=`PAPER["portfolio_drawdown"]=0.08`，见 §2 复现输出 `thresholds_from_paper() = {'max_drawdown': 0.08, ...}`）——**阈值齐备、判定逻辑齐备，唯独输入没接线，于是永远 `skip`**。
- 后果：这是"下单咽喉点上的组合级回撤闸门"。它 skip 之后，回撤保护只剩 `PaperBook.apply_risk_controls()` 的 `state=DRAW_DOWN`（它只"暂停加仓"、且发生在**本 tick 的卖出之后、买入之前**，是账户状态而非**下单前**校验）。任意一 tick 里，只要引擎判定链条没把 `state` 置成 `DRAW_DOWN`，组合层回撤对**这一笔**买单就没有任何约束。
- 该条正是 2026-09-22 事故中被认为"已由 `_pb_equity()` 修好"的两项之一 —— **实际未修好**。

**建议修法（一句话，勿在本轮实施）**：在 `realtime_engine.py:1120-1133` 的 ctx 里补 `"drawdown_pct": (self.pb.apply_risk_controls() or {}).get("drawdown_pct")`（或 `self.pb.snapshot().get("drawdown_pct")`）与 `"peak_equity": self.pb.peak_equity`，并把"ctx 缺字段导致 skip"升级为审计里显式可见的条目。

---

### R2 — P0：清单「单笔仓位上限」的阈值键不存在，永远「记数不判定」

**位置：** `src/pretrade_gates.py:241`（取阈值）、`src/pretrade_gates.py:93-100`（判定）、`src/config.py:245-331`（`PAPER` 定义）

阈值原文（`src/pretrade_gates.py:238-251`）：

```python
dd = PAPER.get("portfolio_drawdown", None)
_strict = bool(PAPER.get("pretrade_strict_freshness", False)) or \
    str(os.environ.get("PRETRADE_STRICT_FRESHNESS", "")).strip() in ("1", "true", "True")
_mp = PAPER.get("pretrade_max_position_pct", None)
```

判定原文（`src/pretrade_gates.py:93-100`）：

```python
if max_position_pct is None:
    # 记数不判定(默认): 实测占比仍写进 detail/measured ...
    add(CHK_POSITION, SKIP,
        (f"记数不判定: 单笔占权益 {_pct:.2f}% (名义 {_notional:.0f})"
         if _pct is not None else "缺 qty/price/equity, 无法算仓位占比"),
        measured=(round(_pct, 4) if _pct is not None else None),
        threshold="(未设阈值)")
```

**(a) 生产路径取不到的证据**

1. `config.PAPER` **没有** `pretrade_max_position_pct` 这个键，且**全仓没有任何一处给它赋值**。grep 该字符串的全部命中：`src/config.py:329`（**注释**）、`src/pretrade_gates.py:208`（docstring 里的示例 `PAPER["pretrade_max_position_pct"] = 0.30`，不执行）、`docs/roadmap7-capabilities.md:281`、`docs/vulnerability-register.md:1131`、`ops/acceptance_status.json:713`（文档）、`tests/test_pretrade_gates.py:76,86`（测试里的 `monkeypatch.setenv`）。实测 `PAPER.get("pretrade_max_position_pct")` → `<ABSENT>`。
2. 环境变量 `PRETRADE_MAX_POSITION_PCT` 在全仓（排除 `.venv314`）只出现在 `src/pretrade_gates.py:232`（读取处）与 `tests/test_pretrade_gates.py:76,86`（测试里 `monkeypatch.setenv`）。**没有任何启动脚本 / NSSM 配置 / `.ps1` / `.bat` 设置它**。
3. 因此 `max_position_pct` 在生产上恒为 `None`，第 93 行分支恒成立 ⇒ **SKIP**。
4. `equity` 在 `_pb_equity()` 修复后确实能取到了 —— 但它只影响 detail 字符串里的"占权益 X%"，**不改变 SKIP 这个结论**。这是"修了输入、没修阈值"的典型错位。

**(b) 被跳过的是哪个风控 / 后果**

- 被跳过：交易前清单第 1 项 `position_pct`（单笔仓位上限）。
- 后果：下单前**没有任何单笔规模上界**。实测（§2 复现）一笔占权益 **981.72%** 的买单得到 `position_pct skip`，清单 `ok=True`。
- 严重度为何是 P0：这与事故描述完全同型 —— "风控项因为拿不到输入而静默跳过，日志显示跳过、看起来一切正常"。且它被 docstring/登记册**描述成"故意不判定"**，正是最容易被后续读者误认为"已生效"的一种。

**建议修法（一句话）**：由运维显式设 `PAPER["pretrade_max_position_pct"]=0.30`（或 `PRETRADE_MAX_POSITION_PCT=0.30`），并把"该项未设阈值"在 `summary_line`/审计里从 `跳过` 升为独立标记（`未启用`）以免与"已判过但无数据"混淆。

---

### R3 — P0：ctx 不传 `max_pos` ⇒ 高危「超一个等权槽位」永不触发 ⇒ 巨量单不进人工审批

**位置：** `src/realtime_engine.py:1121-1133`（ctx） × `src/pretrade_compliance.py:149-157`（高危判定）

判定原文（`src/pretrade_compliance.py:147-157`）：

```python
side = str(o.get("side") or "").lower()
qty, price = _num(o.get("qty")), _num(o.get("price"))
equity, max_pos = _num(c.get("equity")), _num(c.get("max_pos"))

slot = None
if equity and max_pos and max_pos > 0:
    slot = equity / max_pos
    if qty and price and qty * price > slot:
        flags.append({"code": RISK_FULL_SLOT, ...})
```

**(a) 生产路径取不到的证据**

1. ctx 里**没有 `max_pos`**（R1 已逐字数过 9 个键）。
2. `config.PAPER` **没有** `max_pos` 键：`PAPER.get("max_pos")` → `<ABSENT>`（实测）。真正持有该数字的是模块级常量 `src/config.py:43` `MAX_STOCKS = 10`。
3. **代码自己承认了这一点**（这条注释本身就是最硬的证据）—— `src/realtime_engine.py:1108-1109`：
   ```python
   #   equity/cash 用 getattr 探, 探到即自动生效, 探不到只是少一项检查;
   #   PAPER 无 max_pos 键, 故等权槽位那一项高危检查暂不激活。
   ```
   即：作者在写这段接线时就知道这项检查在生产上是 **未激活** 的；`_pb_equity()` 修复只补上了 `equity` 这一半。
4. 生产侧唯一的旁证：`data/pending_orders.json` 里**唯一**一张待批单（`id=20260922011010-1`，2026-09-22 01:10:10）的 reasons 是 `单笔金额 30000 > 一个完整等权槽位 20000 (equity/max_pos=100000/5)` —— 分母是 `5`、权益是 `100000`，**与生产常量 `MAX_STOCKS=10`/账户 101,760 元都不符**，是测试夹具（`tests/test_pending_orders.py` 的 ctx）产出的，不是生产产出的。

**(b) 被跳过的是哪个风控 / 后果**

- 被跳过：`classify_risk` 的 `RISK_FULL_SLOT`（单笔金额 > 一个完整等权槽位 ⇒ 高危）。
- 后果链：`RISK_FULL_SLOT` 是本仓高危单唯一的**数量型**判据（另一条 `RISK_LIQUIDATE` 只对卖出、且只由 `position_qty` 决定，那一个字段 ctx 有传）。它恒不触发 ⇒ `classify_risk` 恒返回 `high_risk=False` ⇒ `gate()` 直接走到"正常放行"分支 ⇒ **本该转人工审批的巨量单被直接下单**。这是本次审计里**风险敞口最大**的一条：不是"少了一道提示"，而是"最后一道人工闸门对这一类单彻底不存在"。
- 独立复现（§2）：生产 ctx + 999,000 元买单 → `{'high_risk': False, 'flags': [], 'slot': None}`；同样一单补上 `max_pos=10` → `{'high_risk': True, 'flags': [{'code': 'notional_over_slot', ...}]}`。

**建议修法（一句话）**：ctx 补 `"max_pos": MAX_STOCKS`（或 `INIT_CAPITAL` 口径下与 `MAX_POS_RATIO` 一致的槽位数），并删除 `realtime_engine.py:1108-1109` 那句"暂不激活"的免责注释。

---

### R4 — P0（变体：哨兵值，非 None）：生产主源恒 `volume=-1` ⇒「停牌不可交易」恒放行

**位置：** `src/paper_book.py:957-964`（主源硬编码） × `src/paper_book.py:1046-1060`（suspended 推断） × `src/realtime_engine.py:634-637`（引擎再注入 `suspended=False`） × `src/realtime_engine.py:708-724`（`_tradable` 唯一消费者）

原文：

```python
# src/paper_book.py:957-965  (_fetch_sina_spot，生产 tick 的主源)
# 新浪无成交量字段, 用 -1 让 suspended 推断跳过错判
out[canon] = {
    "price": price,
    ...
    "volume": -1,
    "suspended": False,
}

# src/paper_book.py:1051-1060  (PriceFeed.get_latest)
for c, q in snap.items():
    if q.get("fallback"):
        q["suspended"] = False          # DuckDB 兜底: 没有成交量信息, 默认为非停牌
    elif q.get("volume", 0) is None or q.get("volume", 0) < 0:
        q["suspended"] = False          # 成交量数据缺失 (-1 或 None): 默认非停牌
    else:
        q["suspended"] = (q["volume"] <= 0)

# src/realtime_engine.py:634-637  (参考价补齐时再注入一次)
if c not in self.feed.quotes:
    self.feed.quotes[c] = {"price": p, "last_close": p,
                           "limit_up": None, "limit_down": None,
                           "volume": 0, "suspended": False}

# src/realtime_engine.py:710-714  (_tradable —— 唯一的消费点)
q = self.feed.quotes.get(canon)
if q is None:
    return True, ""                    # 无行情 -> 交由价格>0兜底
if q.get("suspended"):
    return False, "停牌"
```

**(a) 生产路径取不到的证据（静态可证）**

1. tick 路径走的是 `_fetch_spot(fetch_all=False)`，其**首选源**是新浪单点：`src/paper_book.py:796-800`
   ```python
   if watch:
       codes_sina = [self._canon_to_sina(c) for c in watch if c]
       out.update(self._fetch_sina_spot(codes_sina))
       if out: ...
   ```
   而 `_fetch_sina_spot` 对**每一条**结果都写死 `"volume": -1`（`:963`）与 `"suspended": False`（`:964`）。这是一个**常量**，不是"可能取不到"。
2. 于是 `get_latest` 的 `volume < 0` 分支（`:1056-1058`）对**所有**新浪来源的标的成立 ⇒ `suspended` 恒为 `False`。
3. 新浪返回里 `price <= 0` 的标的会被丢掉（`:947-948`），随后由 `:823-829` 的 DuckDB 兜底补齐 —— 兜底记录同样写死 `"volume": 0, "suspended": False, "fallback": True`（`:995-1003`），在 `get_latest:1053-1055` 又走"fallback ⇒ False"分支。**三条路都通向 `suspended=False`。**
4. 唯一会真的计算 `volume` 的是 akshare 路径 `_ak_to_dict`（`:879-899`，读 `成交量`），但它**只在新浪返回空集时才会被调用**（`:813-820 if not out and watch`），且它自己那一行也写死 `"suspended": False`（`:897`）——即 `suspended` 字段从来不是由数据算出来的，`get_latest` 之后才重算，而重算的输入（`volume`）在生产源上是常量 `-1`。
5. 全仓 `suspended` 的**唯一读取点**是 `src/realtime_engine.py:713`（`_tradable`），`_tradable` 的唯一作用是"涨停不可买 / 跌停不可卖 / 停牌跳过"。

**(b) 被跳过的是哪个风控 / 后果**

- 被跳过：A 股「停牌/无成交不可交易」这道撮合前闸门。
- 后果：停牌股（通常根本不出现在实时快照里，且其"最新价"就是停牌前收盘价）会被 `_disk_ref_prices` 用**上一根日线 close** 补齐价（`realtime_engine.py:626-637`），拿到 `suspended=False`，再经 `_tradable → True`、`market_price>0`，最终以**陈旧价**成交一笔虚构的买入。同时 `limit_up/limit_down` 也是按这个陈旧 close 重算的（`paper_book.py:807-808`），所以涨跌停判定同样不会拦住它。
- 诚实说明：本条的"取不到"是**哨兵值 `-1`**而不是 `None`，且 `:957` 的注释表明 `-1` 是**刻意**选的（为了让 akshare 路径不误判停牌）。但按本次判据，(a) 静态可证、(b) 默认行为是放行一道风控，故计入真风险；只不过它的机制是"哨兵默认值"，与 R1~R3 的"键缺失"不同源，故单列。

**建议修法（一句话）**：把 `volume` 的"未知"与"停牌"分开表达（例如 `volume=None` ⇒ `suspended=None` 三态），并让 `_tradable` 对 `suspended is None` 且来源为兜底价时**拒绝开新仓**（或至少记一条审计），而不是把未知压成 False。

---

### R5 — P1：ctx 不传 `min_cash` ⇒`check_order` 现金底线合规项永不触发

**位置：** `src/realtime_engine.py:1129`（ctx 只给 `cash`） × `src/pretrade_compliance.py:120-125`

原文（`src/pretrade_compliance.py:120-125`）：

```python
if side == "buy" and qty is not None and price is not None and price > 0:
    cash, min_cash = _num(c.get("cash")), _num(c.get("min_cash"))
    if cash is not None and min_cash is not None:
        if cash - qty * price < min_cash:
            v.append({"code": VIOL_CASH, ...})
```

**(a) 证据**

1. ctx 只有 `"cash"`，**没有 `min_cash`**（`realtime_engine.py:1129`）。
2. `PAPER` 无 `min_cash` 键（实测 `<ABSENT>`）；该数字在引擎里是**局部变量**：`realtime_engine.py:847` `min_cash = INIT_CAPITAL * (1 - MAX_POS_RATIO)   # 5%现金底线` —— 存在于同一函数作用域，却没被传进同函数内的闸门调用。
3. 独立复现：`check_order({...buy 100@10...}, {"tradable": True, "cash": 0.5, "min_cash": None})` → `{'ok': True, 'violations': [], 'level': 'OK'}`（现金 0.5 元也判合规）。

**(b) 后果**

- 被跳过：订单级合规项 `cash_floor`（买入后现金跌破底线）。
- 为何是 P1 而不是 P0：**同一条规则在引擎里有独立且生效的执行点** —— `realtime_engine.py:1084-1089`（缩档到现金底线允许的最大整手，且 `:1089` 再确认一次）。所以钱不会真的越线；缺的是"订单级合规流水里的这一项"，属于留痕/冗余防线失效。
- 附带影响：`VIOL_SELLABLE`（`:127-131`）同理恒不触发（ctx 无 `sellable_qty`），但它只作用于卖单，而卖单根本不走 `gate()`（见 R6）。

**建议修法（一句话）**：ctx 补 `"min_cash": min_cash`（把 `:847` 的局部量透传进来）与 `"sellable_qty"`，使合规项与引擎内联检查互为校验。

---

### R6 — P1：`gate()` 的卖单分支在生产上没有调用者 ⇒ 卖出单零订单审计

**位置：** `src/realtime_engine.py:1047-1165`（买循环，闸门唯一调用点） × `src/pretrade_compliance.py:397-402`（卖单分支）

原文（`src/pretrade_compliance.py:397-402`）：

```python
if side == "sell":                      # 离场不排队(见 docstring)
    audit({"action": "execute", ..., "reasons": ["离场单: 仅合规校验, 不进高危队列"], ...})
    return {"decision": "execute", "reasons": []}
```

**(a) 证据**

1. `_PC.gate(` 全仓只有一个生产调用点：`src/realtime_engine.py:1119`。它位于 `if gate_open:` → `for t in self.targets:` 循环内（`:1047-1048`），并且 `side` 是**字面量** `"buy"`（`:1120`）。所以 `gate()` 从不会收到 `side="sell"`。
2. 引擎的三条卖出路径都直接调账本、不过闸门：`realtime_engine.py:926`（离池卖出）、`:959`（移动止损）、`:982`（固定止损）。
3. 卖侧唯一的留痕是 `live_gates.apply_to_engine`，而它**只在账实不一致时**写审计（`src/live_gates.py:158-164`，注释明确写了"每个 tick 都写会把订单流水淹掉"）。
4. 实测产物：`data/order_audit.jsonl` 在仓库内**不存在**（`Get-ChildItem -Recurse -Filter order_audit*` 无命中），即订单级流水至今没有一行 —— 与事故报道 #2 一致。

**(b) 后果**

- 被跳过：卖出单的订单级审计（`audit()`），以及 `check_order` 对卖单的 `price_invalid`/`exceeds_sellable`（T+1 可卖量）两项合规校验。
- 后果：`gate()` docstring 承诺的"四类裁决全部留痕"对**卖出侧完全不成立**；`VIOL_SELLABLE`（T+1）在生产上是死代码（清单话术上却写着"本模块不假装知道 T+1"）。排查"今天卖了什么"时只有 `paper_book` 的成交记录，没有订单级裁决流水。
- 为何不升 P0：卖出被拦会把风险锁在仓里（本仓红线），所以**不接闸门在风控方向上是对的**；问题只在"连留痕的那一段也没接"。

**建议修法（一句话）**：在三条卖出路径调用 `_PC.gate(...)`（或新增 `live_gates.record_sell(...)`）**只为留痕**，不据其结论取消交易。

---

### R7 — P1：IC 门控"没跑起来"`regime="unavailable"` 被清单判成 PASS

**位置：** `src/pretrade_gates.py:127-136` × `src/realtime_engine.py:768-796`

原文（`src/pretrade_gates.py:127-136`）：

```python
regime = c.get("regime")
if regime is None:
    add(CHK_IC_GATE, SKIP, "缺 regime(IC 门控未产出), 不据此拒单")
else:
    r = str(regime).strip().lower()
    frozen = bool(c.get("freeze_new_buys"))
    blocked = r in IC_BLOCKING_STATES or frozen
    ...
    add(CHK_IC_GATE, FAIL if blocked else PASS, detail, ...)
```

**(a) 证据**

1. `regime` 的取值空间里有**三个"门控未生效"的档位**，它们都不在 `IC_BLOCKING_STATES = ("risk",)` 里、也不置 `freeze_new_buys`，因此在 `pretrade_gates.py:135` 一律走 PASS：
   - **`unknown`** —— `src/factor_gate.py:462-463` `if ic_mean is None or ic_neg_share is None: raw = "unknown"`；`:535-536` 的原因文本是 `"IC 缓存缺失/过期, 门控降级为原节奏"`。**这是正常生产可达到的状态**（IC 缓存缺失/过期时），不是异常路径。
   - **`disabled`** —— `src/factor_gate.py:640-649`（`FACTOR_GATE_ENABLED=0` 时直接返回 `regime="disabled"`；该开关默认为开，`:149` `os.environ.get("FACTOR_GATE_ENABLED", "1") != "0"`）。
   - **`unavailable`** —— 引擎自己的两条异常路径：`src/realtime_engine.py:768-774`（`from factor_gate import build_plan_from_cache` 失败）与 `:793-796`（`build_plan_from_cache` 抛异常）都 `return {"regime": "unavailable", "exposure_mult": 1.0, "freeze_new_buys": False, ...}`。
2. 独立复现（§5 命令），实测四种输入的判决：
   ```
   regime='normal'       -> ic_gate_state pass
   regime='risk'         -> ic_gate_state fail
   regime='unknown'      -> ic_gate_state pass      <-- 门控降级，却记"通过"
   regime='unavailable'  -> ic_gate_state pass      <-- 门控没跑起来，也记"通过"
   regime=None           -> ic_gate_state skip
   ```
   即：**"没有信息"（None）记 skip，"有信息说没生效"（unknown/unavailable/disabled）反而记 pass** —— 后者更危险却更"好看"。

**(b) 后果**

- 被跳过/被误报的：清单第 3 项 `ic_gate_state`。IC 门控是唯一会在盘中"冻结新买入"的策略层闸门；它降级/失效时，`pretrade` 清单显示 `pass`，`kill_switch` 的 STRATEGY 层也拿到 `ic_freeze=False`（`realtime_engine.py:1030`）⇒ **两层同时放行**，而列表/面板上看到的是"通过"。
- 为何是 P1 而不是 P0：① `unknown` 在 `factor_gate` 里是**有意的**降级（"降级为原节奏"，即刻意不冻结），所以这里有产品取舍的成分；② `realtime_engine.py:799-809` 每个交易日会打一次 `[GATE_INACTIVE]` 日志（`_active = plan["regime"] not in ("unavailable","disabled","unknown")`）。缺陷在于**判决口径**：清单把"未生效"写成"通过"，读者无法与"真的判过且通过"区分，正是本次事故的形态。

**建议修法（一句话）**：把 `unavailable`/`disabled`/`unknown` 归入 SKIP（或新增 `UNKNOWN` 档），并在 `_finish` 的 `skipped`/审计里显式带上"门控未生效"。

---

## 3. 已核查非风险清单（21 项）

判定口径：**只要有一条不成立就不算风险** —— (i) 该值在生产路径上其实取得到；或 (ii) 默认分支是"记录为缺失/告警/失败"而不是"放行"；或 (iii) 它根本不参与任何风控/校验，只是显示或记账。

| # | 位置 | 写法 | 为什么不是风险 |
|---|---|---|---|
| N1 | `src/realtime_engine.py:776-777` | `if getattr(self, "_gate_day", None) != self.pb.trade_date:` → `self._gate_day = self.pb.trade_date` | 惰性初始化属性。默认值只在首次调用时为 None，而首次调用该 `if` **必为真**（`None != 交易日字符串`），`:777` 随即赋值；不是"可能一直取不到" |
| N2 | `src/realtime_engine.py:787-788` | `if getattr(self, "_gate_hyst", None) is None:` → 建默认字典 | 同上：`:788` 在**同一次调用内**立即赋值；`_compute_gate` 的每次调用都会先走到这一段 |
| N3 | `src/realtime_engine.py:799-800` | `getattr(self, "_gate_logged_day", None)` | 只控制"每天打一次门控日志"，不影响任何判定；缺失只会多打一次日志 |
| N4 | `src/realtime_engine.py:1034` | `getattr(self, "_ks_last_sig", None) != _sig` | 仅用于**告警去重**（同一拦截签名不重复刷屏）；`:1043` 有显式复位 |
| N5 | `src/realtime_engine.py:1031` | `getattr(self.feed, "last_error", "")` | `last_error` 是真实存在的 property（`src/paper_book.py:1096`），不是编造属性 |
| N6 | `src/realtime_engine.py:1071` | `getattr(self, "_tw", {}).get(canon)` | `_tw` 在 `_rebalance` 开头 `:844-845` 必定重建；即使缺失，回退到等权 `band` 是**有文档的**仓位口径回退（`:835`），不是风控闸门 |
| N7 | `src/realtime_engine.py:1355` | `if getattr(self, "degraded", None):` | 仅决定是否往 `live_state` 多写一个显示字段；缺失 = 不显示，不影响状态/判定 |
| N8 | `src/realtime_engine.py:708-712` | `if q is None: return True, ""`（`_tradable` 无行情分支） | 买入侧被价格兜底拦住：`d_price` 每 tick **整体重建**（`:650`，缺价标的被剔除）⇒ `market_price()` 返回 0 ⇒ `:1064 if pr <= 0: continue`、止损/离场侧同样 `:954/:976 if pr <= 0: continue`。即"放行"后面紧跟一道独立且生效的价格闸门（R4 是**另一条**路径：那里价格被参考价补齐了，所以这条不适用） |
| N9 | `src/pretrade_compliance.py:367-372` | `_use_gates = bool(_P.get("pretrade_gates", True))` / `except Exception: _use_gates = True` | 默认值是**开启**清单（fail-toward-checking），方向正确；且开关键 `pretrade_gates` 在 `config.py:331` 确实存在 |
| N10 | `src/pretrade_compliance.py:429-430` | 外层 `except Exception as e: return {"decision":"execute", ..., "error": ...}` | 异常被**写进返回值**，且调用方 `realtime_engine.py:1138-1140` 会 `log("下单前合规判定异常(不阻断, 需排查)")` —— 属"记录并告警"，不是静默 |
| N11 | `src/pretrade_compliance.py:377-386` | 清单执行异常 ⇒ `audit(action="execute", reasons=["...清单执行异常(不阻断, 需排查)..."])` | 同上：落审计 + 明示，符合本仓"判定异常不阻断但必须响亮"的既定纪律 |
| N12 | `src/live_gates.py:158-164` | `except Exception: pass`（审计写入失败） | 外层 `:174-178` 把闸门自身异常写进 `out["error"]` 并由调用方 `realtime_engine.py:1011-1012` 打日志；且该闸门**从不取消交易**，失败无风险放大 |
| N13 | `src/live_gates.py:82-84` | `positions.get(canon)` 为空则报"账本可卖 0 股" | 走 `ACT_SELL_EMPTY` + `reasons`，是**明确报告**而非静默放行 |
| N14 | `src/kill_switch.py:113-130` | `read_global` 读不出 ⇒ `unreadable=True` | 明确的 **fail-closed**：`evaluate` 把 unreadable 判成"按最严处理"并响亮报警，语义是"当已拉闸" |
| N15 | `src/flow_watchdog.py:94-96` | `if age is None: return level="WARN", cause="unknown"` | 未知被判 **WARN（可见）**，不是 OK；且文本明确"不冒充停流"，符合归因纪律 |
| N16 | `src/deadman_switch.py:193-197` | 日历不可用 ⇒ `_in_trading_hours` 返回 True | 失败方向是**多报**（会多判一次 OVERDUE），不是漏报；与本条纪律一致 |
| N17 | `src/deadman_switch.py:95-110` | 注册表文件损坏 ⇒ 退回 `DEFAULT_REGISTRY` | 注释显式讨论过"空注册表会让 verdict 报一切正常"，故**刻意**退缺省而非退空 |
| N18 | `src/premarket_healthcheck.py:500-522, 1057-1142` | `_freshness_verdict` 取不到期望交易日 ⇒ FAIL；`check_position_gap` 读不到 live_state ⇒ 记 `live_state_read_error` 并判 FAIL（仓位对账） | 对账/新鲜度路径在缺输入时**fail-closed 且给出归因文本**，正是本次审计希望看到的写法 |
| N19 | `src/dataguard.py:43-49` | `_empty(x)`: `if x is None: return True` | 这是重试原语里的"空结果"判定辅助函数（`with_retry(empty_is_failure=)` 的入参），不参与任何风控裁决 |
| N20 | `src/exec_gate.py:51-60, 70-71` | `fetch_adv` 失败返回 `{}` ⇒ `throttle` 不拆单 | 拆单闸门是**成本/冲击**控制而非风控闸门，且行情源配置齐备（`PAPER["exec_split"]=True`、`PAPER["participation_cap"]=0.10`，`src/config.py:359,365`）；引擎在 `realtime_engine.py:895-897` 对 ADV 取数异常**打日志** |
| N21 | `src/paper_book.py:477, 623` | `getattr(self, "_last_trailing_hits", [])`、`getattr(self, "_legacy_fee_backfill", False)` | 前者是移动止损的**展示/归因**字段（真实赋值在 `:408`），后者是一次性迁移标志；均不参与撮合与闸门 |

> 补充说明（同属"查过、不构成风险"但值得记一笔，故不单列条目）：
> · `src/realtime_engine.py:302-309` 的 `load_targets` **5 级回退梯子**每级都是 `except Exception: pass`（`:327/:396/:412/:431/:433`）。它确实会静默换池，但本仓已经为它接了留痕与告警：落 `data/targets_source.jsonl`（`:316-324`，实测文件存在）且跨日回退**打日志**；`signal_freeze_watch.observe()` 还会对非当日同源档位发 `pool_fallback` 告警 —— 属"记录并告警"，不是本次判据下的风险。
> · `src/health_state.py:180-208, 261-269` 的 `snap["datasource"]` 采集失败会写成 `level="UNKNOWN"`，而 `assemble:165-166` 把 UNKNOWN 明确翻译成"门禁未生效(不等于健康)"并计入 reasons —— 方向正确。

---

## 4. 无法判定（5 项 + 1 条附注，如实列出）

以下各条我**无法用仓内静态证据证明 (a)**（"生产路径上确实取不到"），因此按判据不计入真风险；但它们同属"缺输入 ⇒ 看起来正常"的形状，建议运行期核对。

| # | 位置 | 疑点 | 为什么无法判定 |
|---|---|---|---|
| U1 | `src/health_state.py:145-150` vs `:261-269` | `gather()` 里 `flow_watchdog.gather()` 抛异常时把 `snap["flow"]=None`、`snap["flow_error"]=<err>`；而 `assemble` 只读 `snap["flow"]`，**`flow_error` 不影响 state** ⇒ 看门狗失效时健康状态不升档 | 需要运行期事实：`flow_watchdog.gather()` 本身把文件读/存活探测都 try 住了（`:152-158`、`:184-188`、`:206-207`），我无法在不实跑的情况下证明它在生产上真的会抛。**建议**：`assemble` 增加 `if snap.get("flow_error"): reasons.append(...)` —— 与 freshness 的三态同构 |
| U2 | `src/degradation.py:262-277` | 五个 `parts` 全部拿不到数据时（`perf` 空 / `trades` 空 / `reward_df` 空），循环后 `total_w=0` ⇒ `overall_score=0.0` 而 `worst_level="OK"`（`:270` 初值 `OK` 无人改写）⇒ 面板/`incremental_learn` 读到"最差维度 = OK" | 需要真实的 ArcticDB/DuckDB 内容才能确定 `perf` 是否为空（`_load_perf_series` 的 `except` 吞掉了失败，`:40-45`）。**建议**：`parts` 为空时把 `worst_level` 置为 `UNKNOWN`/`P3` 并写明"无数据" |
| U3 | `src/pretrade_gates.py:233-237` | `from config import PAPER` 失败 ⇒ `thresholds_from_paper()` 返回**四个 None** ⇒ 五项检查**全部 skip**，且**不落审计、不打日志**（对比 `pretrade_compliance` 有响亮的异常分支） | `config` 是仓内模块，我无法证明它在生产上会 import 失败。**建议**：把该 `except` 分支改成"返回 None 但由调用方落一条审计/日志" |
| U4 | `src/realtime_engine.py:768-778` | `_day_start_eq` 只在 `_compute_gate` 内、且在 `from factor_gate import build_plan_from_cache` 的 try **之后**才赋值（`:776-778`）。若该 import 失败，函数在 `:770-774` 提前 return ⇒ `getattr(self, "_day_start_eq", None)`（`:1133`）为 None ⇒ 清单第 5 项「单日亏损」也退化为 skip | 需要 `factor_gate` 在生产上 import 失败才有实际后果，无法静态证明。**建议**：把日初权益的记录移到 `_compute_gate` 的**最前面**（早于任何可能失败的 import） |
| U5 | `src/realtime_engine.py:1112-1118` × `src/pretrade_gates.py:139-153` | `_lag = (self.sel or {}).get("data_lag_days")`：只有 DRL 档位（`_plan_to_targets` 透传，`:256-271`）才带该字段；`load_targets` 的其它 4 级档位没有 ⇒ 该项随档位而变 | 当前**无实际后果**（`max_data_lag_days` 默认为 None，该项本来就 skip）；但一旦运维开启 `PRETRADE_STRICT_FRESHNESS=1`，"该字段是否恒存在"就变成关键，而那取决于当日落到 5 级梯子的哪一档 —— 需运行期读 `data/targets_source.jsonl` 才能定性 |
| U6（附注） | `src/paper_book.py:1056-1060` | R4 的**另一半前提**：到底哪些"停牌股"会落在 `volume=-1` 那一桶里 | R4 中"`volume` 在生产主源上是常量 -1"**已静态证明**；但"某只真实停牌股是否出现在新浪返回里、以什么价位出现"需要一次盘中实测才能确认。若要闭环 R4，建议在盘中记录一次 `feed.quotes` 中 `suspended=True` 的计数（预期恒为 0） |

---

## 5. 复跑方式（可复现）

全部命令以 repo 根为工作目录；`.venv314\Scripts\python.exe` 为本仓解释器。

**(1) 候选枚举（本次用的 4 组）**

```powershell
# a) 所有 getattr(obj,"attr",<default>) 形态
Get-ChildItem -Path src,scripts -Recurse -File -Filter *.py |
  Select-String -Pattern 'getattr\(' | ForEach-Object { "$($_.Filename):$($_.LineNumber): $($_.Line.Trim())" }

# b) 单参数 .get("key")（无默认值 = 返回 None），在风控/闸门模块里逐条核对键是否存在
$targets = "src\pretrade_gates.py","src\pretrade_compliance.py","src\live_gates.py",
           "src\exec_gate.py","src\kill_switch.py","src\health_state.py",
           "src\flow_watchdog.py","src\degradation.py","src\deadman_switch.py",
           "src\datasource_gate.py","src\run_daily.py"
foreach ($f in $targets) {
  Select-String -Path $f -Pattern '\.get\("([^"]+)"\)(?!\s*,)' -AllMatches |
    ForEach-Object { foreach ($m in $_.Matches) { "$f`:$($_.LineNumber): .get(`"$($m.Groups[1].Value)`")" } }
}

# c) "except ...: pass/continue/return None/[]/{}" —— 吞异常点（跨行匹配）
$pat = 'except[^\r\n]*:\s*(#[^\r\n]*)?\r?\n(\s+)(pass|continue|return None|return \{\}|return \[\]|return 0)\s*(\r?\n|$)'
Get-ChildItem -Path src,scripts -Recurse -File -Filter *.py | ForEach-Object {
  $t = Get-Content $_.FullName -Raw
  foreach ($m in [regex]::Matches($t, $pat)) {
    "{0}:{1}: {2}" -f $_.Name, (($t.Substring(0,$m.Index) -split "`n").Count), (($m.Value -replace '\s+',' ').Trim())
  }
}

# d) "if x is None: return/continue/pass/break" —— 缺信息即放行
$pat2 = 'if [^\r\n]*?is None[^\r\n]*:\s*(#[^\r\n]*)?\r?\n(\s+)(return True|return\s|continue|pass|break)[^\r\n]*'
Get-ChildItem -Path src -Recurse -File -Filter *.py | ForEach-Object {
  $t = Get-Content $_.FullName -Raw
  foreach ($m in [regex]::Matches($t, $pat2)) {
    "{0}:{1}: {2}" -f $_.Name, (($t.Substring(0,$m.Index) -split "`n").Count), (($m.Value -replace '\s+',' ').Trim())
  }
}
```

**(2) 判定分水岭：属性/键到底存不存在**

```powershell
# PaperBook 有没有 equity 属性？(答案是"没有", 只有 peak_equity)
Select-String -Path src\paper_book.py -Pattern 'self\.equity|"equity"|self\.peak_equity|"drawdown_pct"'

# PAPER 里有没有这些键？
Select-String -Path src\config.py -Pattern '"max_pos"|"min_cash"|"pretrade_max_position_pct"|"pretrade_strict_freshness"'
Select-String -Path src\config.py -Pattern '^MAX_STOCKS|^MAX_POS_RATIO|"portfolio_drawdown"'

# 生产上唯一的下单咽喉点调用者
Get-ChildItem -Path src,scripts -Recurse -File -Filter *.py |
  Select-String -Pattern '(pretrade_compliance|_PC|PC)\.gate\('
```

**(3) 决定性复现：在"逐字复刻的生产 ctx"上跑一遍闸门**

```powershell
.venv314\Scripts\python.exe -   # 然后把下面这段贴进 stdin（或保存为临时脚本执行, 不落正式产物）
```
```python
import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
import pretrade_gates as PG, pretrade_compliance as PC
print(PG.thresholds_from_paper())
# realtime_engine.py:1120-1133 的 ctx 键集合（equity 已由 _pb_equity() 现算）
ctx = {"tradable": True, "position_qty": 0, "equity": 101760.31, "cash": 80689.31,
       "regime": "normal", "freeze_new_buys": False,
       "data_lag_days": 3, "day_start_equity": 101760.31}
order = {"symbol": "600000", "side": "buy", "qty": 99900, "price": 10.0}   # 999,000 元 = 9.8×权益
g = PG.evaluate(order, ctx, **PG.thresholds_from_paper())
for c in g["checks"]:
    print("   %-20s %-5s %s" % (c["name"], c["status"], c["detail"][:70]))
print("   ok=%s failed=%s skipped=%s" % (g["ok"], g["failed"], g["skipped"]))
print(PC.check_order({"symbol":"600000","side":"buy","qty":100,"price":10.0},
                     {"tradable": True, "cash": 0.5, "min_cash": None}))
print(PC.classify_risk(order, ctx))                 # 期望: high_risk=False, slot=None  <-- R3
print(PC.classify_risk(order, dict(ctx, max_pos=10)))  # 期望: notional_over_slot      <-- 有 max_pos 就对了
```

本次实际输出（2026-09-22，上表哈希对应版本）：

```
thresholds_from_paper() = {'max_position_pct': None, 'max_drawdown': 0.08, 'max_daily_loss': 0.08, 'max_data_lag_days': None}
--- pretrade_gates.evaluate, PRODUCTION ctx, notional=999000 (9.8x equity) ---
   position_pct         skip  记数不判定: 单笔占权益 981.72% (名义 999000)
   portfolio_drawdown   skip  缺 drawdown_pct/peak_equity, 无法算组合回撤
   ic_gate_state        pass  门控档位=normal
   data_freshness       skip  记数不判定: 数据滞后 3 自然日(data_lag_days, 非交易日差)
   daily_loss           pass  当日盈亏 0.00%
   ok=True failed=[] skipped=['position_pct', 'portfolio_drawdown', 'data_freshness']
--- check_order, production ctx, cash=0.5 CNY ---
   {'ok': True, 'violations': [], 'level': 'OK'}
--- classify_risk, production ctx, 999000 CNY single order ---
   {'high_risk': False, 'flags': [], 'slot': None}
--- classify_risk, same order + max_pos=10 ---
   {'high_risk': True, 'flags': [{'code': 'notional_over_slot', 'detail': '单笔金额 999000 > 一个完整等权槽位 10176 (equity/max_pos=101760/10)'}], 'slot': 10176.031}
```

`regime` 四态对照（R7 的复现）：

```python
for reg in ("normal", "risk", "unknown", "unavailable", "disabled", None):
    g = PG.evaluate({"symbol":"600000","side":"buy","qty":100,"price":10.0},
                    dict(ctx, regime=reg), **PG.thresholds_from_paper())
    c = [x for x in g["checks"] if x["name"] == "ic_gate_state"][0]
    print("regime=%-14r -> %s" % (reg, c["status"]))
# 实测: normal->pass ; risk->fail ; unknown->pass ; unavailable->pass ; disabled->pass ; None->skip
```

**(4) 旁证：生产产物**

```powershell
Get-ChildItem -Path . -Recurse -Filter "order_audit*"        # 无输出 ⇒ 订单审计从未落盘 (R6)
Get-Content data\pending_orders.json -Raw                    # 唯一一张票的 max_pos=5 ⇒ 来自测试夹具 (R3)
Get-Content data\health\state.json -Raw                       # datasource/flow 的实际形态
Test-Path data\targets_source.jsonl                           # load_targets 档位留痕存在 (N 补充说明)
```

**(5) 已有的互补工具（本轮未新增脚本）**

```powershell
.venv314\Scripts\python.exe scripts\audit_silent_failures.py    # 仓库自带的 AST 扫描: 只覆盖"吞异常"
```
说明：该工具产出 `data/silent_failure_audit.json`（567 条 findings，按决策链/数据链分级），它覆盖的是**异常被吞**这一类；本次事故的类（**字段取不到 ⇒ 默认值 ⇒ 静默放行**）不在它的判据里，故本报告与其互补而非重复。

---

## 6. 方法与边界（如实声明）

1. **判据执行情况**：每条真风险都给出了 (a) 生产路径取不到的证据（grep 结果 / 类定义 / `__init__` / 常量 / 可执行复现）与 (b) "默认行为是放行而非记录告警"的证据。任何一条 (a) 只能"理论上可能缺"的，我都移到了 §4 无法判定，而不是塞进真风险凑数。
2. **没有看函数名猜结论**：所有候选都回到定义处核对了属性/键是否真的存在（例如 `PaperBook` 只有 `cash`/`peak_equity`，`equity` 只是 `snapshot()` 的返回键；`PAPER` 有 `portfolio_drawdown` 但**没有** `max_pos`/`min_cash`/`pretrade_max_position_pct`）。
3. **未修改任何代码**。本轮只新建了本文件。
4. **审计期间仓库在变动**：`src/health_state.py`（325→362 行）、`src/run_daily.py`、`src/vnpy_backtest.py` 在我审计过程中被另一个会话修改，`src/datasource_gate.py` 为新增未跟踪文件。因此：① 行号锚定在 §0 的文件哈希上；② 我**没有**把 `datasource_gate` 这条新接线（它已经在 `run_daily.py:369-404` 与 `health_state.py:152-166, 189-208` 接上，方向正确）计入本次结论；③ 若在别的时间点复跑，请以每条的**代码原文**为准定位。
5. **`scripts/` 的覆盖程度**：`scripts/` 下 4 处 `getattr` 全部核对为非风险（`preflight_circuit_breaker.py:34` 读 `PAPER` 配置、`preflight_paperbook.py:94-96` 数成交条数、`record_data_outage.py:56` 读第三方 `ak.__version__`），均为预检/诊断用途，不在下单或风控链路上。`scripts/` 的其它大量 `except Exception: pass` 属回测/研究/预检脚本，未逐条展开（它们不改变实盘风控结论）；如需同样口径的逐条判定，建议限定文件清单后另开一轮。
6. **明确没做的事**：没有实跑引擎、没有连厂商 SDK、没有触发任何交易或写盘产物；所有复现都是纯函数级（`pretrade_gates.evaluate` / `pretrade_compliance.check_order|classify_risk`）与静态阅读。
