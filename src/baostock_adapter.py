# -*- coding: utf-8 -*-
"""Baostock 适配器骨架: 限流 / 标的分类 / 缺口分类 三个契约。

## 为什么先写"骨架 + 契约"而不是直接连数据

Baostock 是**逐 symbol** 接口(`query_all_stock` 只给代码不给 OHLCV), 全市场要
5481 次请求。这种"很多次小请求"的适配器, 失败模式与"一次大批量"完全不同:
**部分成功**是常态(个别代码停牌/退市/无数据/被限流), 而"部分成功"若被当成
"成功"或"失败", 都会产生错误结论:

- 当成成功 ⇒ 缺口被静默吞掉(本仓最忌讳);
- 当成失败 ⇒ 一整天的数据因个别标的而全弃(过度反应)。

故本模块**先把三个契约写死并用可注入 fetcher 测透**, 再谈接真实数据。

## 三个契约(用户指定)

**契约一 限流策略**: 默认 `200ms` 间隔; 检测到限流则**自动退避到 1.5s**。
  (实测: 无间隔连打 20 只 = 150ms/只且零限流 —— 即 200ms 有余量;
   1.5s 是**退避后**的值, 不是默认值。用户方案原写"每只 1.5s"过于保守:
   那会让全市场从 13.7 分钟变成 2.28 小时。)

**契约二 个股/ETF 区分**: 用 **h5i `symbols.parquet` 的 `market` 列**,
  **不调 `query_stock_basic`** —— 后者需要逐个请求(6897 次), 而 symbols.parquet
  已是本仓权威标的表且**已在进程内缓存**(`db._h5i_symbols_df`)。
  实测该表 `market ∈ {sh:2315, sz:2897, bj:339}`。

**契约三 缺口分类**: `status ∈ {appended, partial, unfillable_gap, failed}`。
  `partial` 是新增档: **部分标的取到、部分没取到**, 且必须带上缺口明细。
"""
from __future__ import annotations

import random
import time

#: 契约一: 默认请求间隔(毫秒)与限流退避后的间隔。
DEFAULT_INTERVAL_MS = 200
BACKOFF_INTERVAL_MS = 1500
#: 连续多少次"限流特征"才触发退避(单次抖动不退避, 与数据源门禁同思路:
#: 交替出现的偶发失败不该改变策略, 持续出现的才该)。
BACKOFF_AFTER_FAILS = 2
#: 退避后恢复默认间隔所需的连续成功次数。
RECOVER_AFTER_OKS = 20

#: 契约三: 结果档位。
APPENDED, PARTIAL, UNFILLABLE, FAILED = (
    "appended", "partial", "unfillable_gap", "failed")

# ---------------------------------------------------------------------------
# 真实 fetcher
# ---------------------------------------------------------------------------

#: 请求字段集。**必须包含价格衍生字段**, 理由见 `REQUIRED_FIELDS`。
FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,"
          "adjustflag,turn,tradestatus,pctChg")

#: **因子计算纪律**: 价格衍生指标一律**取自源**, 不得自算。
#:
#: 实测依据(600177 @ 2026-09-18, 含除权): 前一日 close=**8.30**, 当日 close=**8.14**。
#:   · 自算 `(8.14/8.30-1)` = **-1.93%**  ← 用未调整前收, **错**
#:   · 引擎 `pct_chg`        = **+0.49%**  ← 基于除权调整后 pre_close=8.10
#:   · baostock `pctChg`     = **+0.4938%** ← 与引擎一致
#: ⇒ 少取一个字段就会**静默算错**, 且错得"看起来很正常"(-1.93% 是个合理涨跌幅)。
#: 故 `pctChg` / `turn` / `preclose` 属于**必需字段**, 缺了宁可报错也不自算。
REQUIRED_FIELDS = ("pctChg", "turn", "preclose")


def make_baostock_fetcher(*, start: str, end: str, adjustflag: str = "3",
                          fields: str = FIELDS, login=True):
    """构造**真实** fetcher: `fetch(bscode) -> DataFrame`。

    设计要点(每条都有实测依据):
      1. **一次请求取整个区间**, 不在日期上再循环。逐只已经够慢, 再按天循环会让
         请求数乘上区间天数(2 天 => 10424 次而非 5212 次)。
      2. **`adjustflag='3'` 不复权** —— 除权日实测: flag=3 与引擎 OHLC 逐值一致 3/3,
         而 flag=2(前复权) 仅 2/3(历史价被复权缩放)。
      3. **请求 `pctChg`/`turn`/`preclose`** —— 见 `REQUIRED_FIELDS` 的纪律说明。
      4. 返回**原始列名的 DataFrame**(`code/date/open/.../pctChg/turn`),
         交给 `bars_ingest.normalize(..., "baostock")` 做映射 —— 换算与映射**只有一处**。

    `login=False` 供已登录的场景复用会话(避免每批重复登录)。
    """
    if adjustflag != "3":
        # 不阻断(允许显式实验), 但**必须响亮提示** —— 这是口径问题, 不是风格问题
        import warnings
        warnings.warn(
            f"baostock adjustflag={adjustflag!r} 与引擎口径不一致: 本仓对齐的是 '3' 不复权。"
            f"除权日会有差异(实测 flag=2 前复权在同一除权日与引擎 2/3 不一致)。",
            RuntimeWarning, stacklevel=2)

    import baostock as bs
    import pandas as pd

    missing = [f for f in REQUIRED_FIELDS if f not in fields.split(",")]
    if missing:
        raise ValueError(
            f"请求字段缺少 {missing} —— 这些是**必需**的: "
            f"价格衍生指标(涨跌幅/换手率)必须取自源, 自算会在除权日静默算错"
            f"(实测 600177 @ 2026-09-18: 自算 -1.93% vs 正确 +0.49%)")

    state = {"logged_in": False}

    def _login():
        if state["logged_in"]:
            return
        r = bs.login()
        if str(getattr(r, "error_code", "1")) != "0":
            raise ConnectionError(f"baostock 登录失败: {r.error_msg}")
        state["logged_in"] = True

    if login:
        _login()

    def fetch(bscode: str):
        _login()
        rs = bs.query_history_k_data_plus(
            str(bscode), fields, start_date=start, end_date=end,
            frequency="d", adjustflag=adjustflag)
        code = str(getattr(rs, "error_code", "1"))
        if code != "0":
            # 把源错误**原样抛出**, 让 fetch_batch 用 looks_throttled 判定是否限流
            raise RuntimeError(f"baostock error_code={code} msg={getattr(rs, 'error_msg', '')}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame(columns=fields.split(","))
        return pd.DataFrame(rows, columns=rs.fields)

    def close():
        if state["logged_in"]:
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
            state["logged_in"] = False

    fetch.close = close
    return fetch


def fetch_range(symbols, *, start: str, end: str, symbols_df=None,
                limiter: RateLimiter | None = None, adjustflag: str = "3",
                fetch_one=None, fetcher=None, on_row=None) -> dict:
    """**定向补数**入口: 按 symbol 列表取指定区间, 返回契约三的归类结果。

    `symbols` 为 h5i 裸代码列表(如 `["600000","000001"]`)。
    `symbols_df` 给定时用它区分市场并**显式报出北交所不可得**;
    不给则按前缀推断(可能把 `920xxx` 误判 —— 但那已被 `to_baostock_code` 处理)。

    `fetcher` / `fetch_one` 可注入: 前者是 `make_baostock_fetcher(...)` 的产物,
    后者是"裸代码 -> DataFrame"的简版。**测试用注入版, 生产用真实版。**

    `on_row(bare_code, df)` [2026-09-25 新增]: 每取到一只**立即回调**, 用于**流式聚合**。
    为什么需要它: 全市场 5481 只的 DataFrame 同时留在内存里约数百 MB, 而**补数**
    必须按"日"聚合成一个整截面才能过 `bars_ingest.normalize` 的残截面阈值
    (默认 1000 行/日)。有了回调, 调用方可以逐日累加并在取完后立刻释放,
    不必先攒齐全市场。**回调抛错不吞**: 聚合逻辑出错必须立刻暴露,
    否则会变成"部分日静默少了标的"。

    返回 `classify_outcome(...)` 的结果 + `rows` / `failed` / `limiter`。
    """
    syms = [str(s) for s in symbols]
    # 契约二: 用 symbols.parquet 区分市场; 北交所显式不可得
    unfetchable: dict = {}
    if symbols_df is not None:
        cls = classify_targets(symbols_df)
        bj = set(cls["by_market"].get("bj", []))
        if bj:
            hit = [s for s in syms if s in bj]
            if hit:
                unfetchable["bj"] = {
                    "symbols": hit, "count": len(hit),
                    "reason": ("Baostock 对北交所(`bj.*`)返回空 —— 实测 "
                               "`query_stock_basic('bj.430047')` 无数据; 覆盖仅沪深"),
                    "action": "走 h5i_ingest/h5i_rebuild 运维口径, 或改用其它源",
                }
        todo = [s for s in syms if s not in bj]
    else:
        todo = list(syms)

    market_of = {}
    if symbols_df is not None and "market" in getattr(symbols_df, "columns", []):
        market_of = {str(a): str(b) for a, b in
                     zip(symbols_df["symbol"], symbols_df["market"])}

    if fetch_one is None:
        if fetcher is None:
            fetcher = make_baostock_fetcher(start=start, end=end, adjustflag=adjustflag)
        f = fetcher

        def fetch_one(sym):
            return f(to_baostock_code(sym, market_of.get(sym)))
    else:
        f = None

    lim = limiter or RateLimiter()
    got = fetch_batch([to_baostock_code(s, market_of.get(s)) for s in todo],
                      fetch_one, limiter=lim, on_row=(
                          (lambda code, df: on_row(
                              code.split(".")[-1] if "." in code else code, df))
                          if on_row is not None else None))
    # 把结果键从 baostock 代码折回裸代码, 便于与引擎/ h5i 对齐
    rows = {}
    for k, v in got["rows"].items():
        bare = k.split(".")[-1] if "." in k else k
        rows[bare] = v
    failed = {}
    for k, v in got["failed"].items():
        bare = k.split(".")[-1] if "." in k else k
        failed[bare] = v

    out = classify_outcome(requested=len(syms), got=len(rows), unfetchable=unfetchable)
    out.update({"rows": rows, "failed": failed, "limiter": got["limiter"],
                "interval_ms": got["interval_ms"],
                "start": start, "end": end, "adjustflag": adjustflag})
    # 北交所那些"没去请求"的标的, 计入 failed 会误导 —— 单列
    out["not_requested"] = sorted(unfetchable.get("bj", {}).get("symbols", []))
    if f is not None and hasattr(f, "close"):
        try:
            f.close()
        except Exception:  # noqa: BLE001
            pass
    return out


class RateLimiter:
    """契约一: 限流策略。

    - 常态: `interval_ms`(默认 200ms);
    - 检测到**限流特征**连续 `BACKOFF_AFTER_FAILS` 次 -> 退避到 `backoff_ms`(1.5s);
    - 退避后连续 `RECOVER_AFTER_OKS` 次成功 -> 回到常态。

    为什么不用"固定 1.5s": 实测 150ms/只即可稳定跑, 固定 1.5s 会把全市场
    从 13.7 分钟拖到 2.28 小时。**用能跑的最快速度, 只在被限流时退让。**
    """

    def __init__(self, interval_ms: int = DEFAULT_INTERVAL_MS,
                 backoff_ms: int = BACKOFF_INTERVAL_MS, *, sleep=time.sleep):
        self.base_ms = int(interval_ms)
        self.backoff_ms = int(backoff_ms)
        self.interval_ms = self.base_ms
        self._sleep = sleep
        self._last = 0.0
        self._fails = 0
        self._oks = 0
        self.stats = {"requests": 0, "backoffs": 0, "recoveries": 0,
                      "throttle_signals": 0}

    def wait(self) -> None:
        gap = self.interval_ms / 1000.0
        now = time.monotonic()
        dt = now - self._last
        if dt < gap:
            self._sleep(gap - dt)
        self._last = time.monotonic()

    def note(self, ok: bool, *, throttled: bool = False) -> None:
        """报告一次请求结果; `throttled` 由调用方判定(见 `looks_throttled`)。

        **只有"限流特征"的失败才累计退避计数**。
        首版把所有失败都计入, 于是"某批标的全部停牌/退市"(空数据, 完全正常)
        会把间隔从 200ms 退避到 1.5s —— **把整批从 13.7 分钟拖到 2.28 小时**,
        而原因只是那批票当天没成交。已被 `test_empty_data_is_not_treated_as_throttle`
        立刻抓住。故: 退避只对**限流**反应, 其它失败只记录不改变节奏。
        """
        self.stats["requests"] += 1
        if ok:
            self._oks += 1
            self._fails = 0
            if self.interval_ms != self.base_ms and self._oks >= RECOVER_AFTER_OKS:
                self.interval_ms = self.base_ms
                self.stats["recoveries"] += 1
            return
        self._oks = 0
        if not throttled:
            return               # 非限流失败: 不改节奏
        self.stats["throttle_signals"] += 1
        self._fails += 1
        if self._fails >= BACKOFF_AFTER_FAILS and self.interval_ms < self.backoff_ms:
            self.interval_ms = self.backoff_ms
            self.stats["backoffs"] += 1


#: 限流/拒绝的文本特征。**保守列举**: 只认明确的限流措辞, 避免把"停牌无数据"
#: 误判成限流而白白退避(那会拖慢整批)。
_THROTTLE_HINTS = ("too many", "rate limit", "ratelimit", "频繁", "限流",
                   "flow control", "busy", "please slow", "429")


def looks_throttled(err) -> bool:
    """判断一个错误/消息是否是**限流特征**。"""
    s = str(err or "").lower()
    return any(h in s for h in _THROTTLE_HINTS)


def to_baostock_code(symbol: str, market: str | None = None) -> str:
    """h5i 裸代码 -> baostock 代码(`sh.600000` / `sz.000001` / `bj.920000`)。

    `market` 给了就用它(来自 symbols.parquet 的权威 market 列);
    否则按前缀推断。

    **判据顺序有讲究(实测踩到)**: 北交所代码是 **`920xxx`**, 而 `9` 开头在沪市是
    B 股 —— 若先判 `9* -> sh`, 就会把北交所错判成沪市(首版就是这样,
    `920000` 得到 `sh.920000`)。故 **`920` 必须先于 `9` 判定**。
    实测 h5i symbols.parquet 的 bj 标的全部是 `920xxx`。
    """
    import re
    s = str(symbol).strip()
    if "." in s:                      # 已是 `sh.600000` 形式
        return s.lower()
    m = (market or "").lower() or None
    if m not in ("sh", "sz", "bj"):
        m = None
    if m is None:
        if re.match(r"^920", s):          # 北交所(**先于** 9 开头的沪 B 判定)
            m = "bj"
        elif re.match(r"^[48]", s):       # 北交所旧段(4xxxxx/8xxxxx)
            m = "bj"
        elif s.startswith(("6", "9")):    # 沪市 A 股 + 沪 B
            m = "sh"
        elif s.startswith(("0", "3")):    # 深市主板/创业板
            m = "sz"
        else:
            m = "sh"
    return f"{m}.{s}"


def classify_targets(symbols_df) -> dict:
    """契约二: 用 h5i `symbols.parquet` 区分市场, **不调 query_stock_basic**。

    返回 {'by_market': {m: [裸代码...]}, 'fetchable': [...], 'unfetchable': {...}}
      · `fetchable`  = Baostock **能取**的(沪深);
      · `unfetchable`= Baostock **取不到**的(北交所), 按市场分组并给原因。

    实测: `bj.*` 的 `query_stock_basic` 返回**空** ⇒ 北交所是**数据源覆盖缺口**,
    必须显式报出而非静默跳过(本仓纪律)。
    """
    import pandas as pd

    if symbols_df is None or (hasattr(symbols_df, "empty") and symbols_df.empty):
        return {"by_market": {}, "fetchable": [], "unfetchable": {}}
    df = symbols_df if isinstance(symbols_df, pd.DataFrame) else pd.DataFrame(symbols_df)
    if "symbol" not in df.columns:
        return {"by_market": {}, "fetchable": [], "unfetchable": {}}
    mkt = (df["market"].astype(str).str.lower() if "market" in df.columns
           else pd.Series([""] * len(df), index=df.index))
    by: dict[str, list] = {}
    for sym, m in zip(df["symbol"].astype(str), mkt):
        m = m if m in ("sh", "sz", "bj") else to_baostock_code(sym).split(".")[0]
        by.setdefault(m, []).append(sym)
    # Baostock 实测覆盖 sh/sz; bj 无数据
    fetchable = sorted(by.get("sh", []) + by.get("sz", []))
    unfetchable = {}
    if by.get("bj"):
        unfetchable["bj"] = {
            "symbols": sorted(by["bj"]),
            "count": len(by["bj"]),
            "reason": ("Baostock 对北交所(`bj.*`)返回空 —— 实测 "
                       "`query_stock_basic('bj.430047')` 无数据; 覆盖仅沪深"),
            "action": "走 h5i_ingest/h5i_rebuild 运维口径, 或改用其它源",
        }
    return {"by_market": {k: sorted(v) for k, v in by.items()},
            "fetchable": fetchable, "unfetchable": unfetchable}


def fetch_batch(codes, fetch_one, *, limiter: RateLimiter | None = None,
                on_error=None, on_row=None) -> dict:
    """按 `codes` 逐个取数, 逐只应用限流与退避。**不抛**(逐只失败被收集)。

    `fetch_one(code)` 由调用方注入 —— 真实实现调 baostock, 测试注入假实现。
    `on_row(code, df)` 每取到一只**立即回调**(成功取数才调, 空/失败不调),
    供调用方流式聚合、及时释放内存(见 `fetch_range` 的说明)。
    返回 {'rows': {code: DataFrame}, 'failed': {code: reason}, 'limiter': stats}
    """
    lim = limiter or RateLimiter()
    rows: dict = {}
    failed: dict = {}
    for c in codes:
        lim.wait()
        try:
            d = fetch_one(c)
        except Exception as e:  # noqa: BLE001
            thr = looks_throttled(e)
            lim.note(False, throttled=thr)
            failed[c] = f"{type(e).__name__}: {e}"
            if on_error:
                try:
                    on_error(c, e)
                except Exception:  # noqa: BLE001
                    pass
            continue
        empty = d is None or (hasattr(d, "empty") and d.empty) or (
            hasattr(d, "__len__") and len(d) == 0)
        if empty:
            # 空**不算限流**: 停牌/退市/无数据都是正常空, 误判成限流会白退避
            lim.note(False, throttled=False)
            failed[c] = "空数据(停牌/退市/该日无成交)"
            continue
        lim.note(True)
        rows[c] = d
        if on_row is not None:
            # **不吞回调异常**: 聚合出错必须立刻暴露 —— 否则会变成
            # "某些日静默少了标的", 而那正是补数最危险的失败模式。
            on_row(c, d)
    return {"rows": rows, "failed": failed, "limiter": dict(lim.stats),
            "interval_ms": lim.interval_ms}


def classify_outcome(*, requested: int, got: int, unfetchable: dict | None = None,
                     error: str | None = None) -> dict:
    """契约三: 把一批取数结果归类为 `{appended, partial, unfillable_gap, failed}`。

    判据(**顺序有意义**):
      1. `error` 非空且一行未取到        -> `failed`
      2. 一行未取到                      -> `failed`
      3. `unfetchable` 非空(如北交所) 且**其余全取到** -> `unfillable_gap`
         (部分标的**原理上取不到**, 不是本次故障)
      4. 取到一部分、缺一部分            -> `partial`  (**必须带缺口明细**)
      5. 全部取到                        -> `appended`

    ⚠️ `partial` 是本契约的关键: 逐只接口下"部分成功"是常态,
    把它当"成功"会静默吞掉缺口, 当"失败"会因个别标的弃掉整天的数据。
    """
    unf = unfetchable or {}
    n_unf = sum(int(v.get("count") or len(v.get("symbols") or []))
                for v in unf.values()) if unf else 0
    missing = max(0, int(requested) - int(got))
    out = {"status": None, "requested": int(requested), "got": int(got),
           "missing": missing, "unfetchable": unf, "error": error}
    if got <= 0:
        out["status"] = FAILED
        out["reason"] = error or "一只都没取到"
        return out
    if missing <= 0:
        out["status"] = APPENDED
        return out
    # 有缺: 若缺口**全部**是"原理上取不到"的标的, 则属 unfillable_gap
    if n_unf and missing <= n_unf:
        out["status"] = UNFILLABLE
        out["reason"] = ("缺口全部来自数据源**原理上取不到**的标的("
                         + ", ".join(f"{k}:{v.get('count')}" for k, v in unf.items())
                         + ") —— 非本次故障")
        return out
    out["status"] = PARTIAL
    out["reason"] = (f"取到 {got}/{requested}, 缺 {missing} 只"
                     + (f"(其中 {n_unf} 只属原理上不可得)" if n_unf else ""))
    return out


def _main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Baostock 适配器: 真实 fetcher / 契约自检")
    ap.add_argument("--targets", action="store_true", help="用 symbols.parquet 分类标的")
    ap.add_argument("--limiter", action="store_true", help="打印限流参数")
    ap.add_argument("--fields", action="store_true", help="打印请求字段集(含纪律说明)")
    ap.add_argument("--probe", nargs="*", metavar="SYMBOL",
                    help="真实拉取若干标的(默认取少量样本)并打印首行")
    arg = ap.parse_args(argv)
    if arg.limiter:
        print(json.dumps({"default_interval_ms": DEFAULT_INTERVAL_MS,
                          "backoff_interval_ms": BACKOFF_INTERVAL_MS,
                          "backoff_after_fails": BACKOFF_AFTER_FAILS,
                          "recover_after_oks": RECOVER_AFTER_OKS,
                          "throttle_hints": list(_THROTTLE_HINTS)},
                         ensure_ascii=False, indent=2))
        return 0
    if arg.fields:
        print(json.dumps({"fields": FIELDS.split(","),
                          "required": sorted(REQUIRED_FIELDS),
                          "discipline": ("价格衍生指标**取自源**, 不得自算 —— "
                                         "除权日自算 change_pct 会得到 -1.93% 而正确答案是 "
                                         "+0.49%(实测 600177 @ 2026-09-18)")},
                         ensure_ascii=False, indent=2))
        return 0
    if arg.targets:
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from db import _h5i_symbols_df
        cls = classify_targets(_h5i_symbols_df())
        print(json.dumps({"fetchable": len(cls["fetchable"]),
                          "by_market_counts": {k: len(v) for k, v in
                                               cls["by_market"].items()},
                          "unfetchable": {k: {"count": v["count"],
                                              "reason": v["reason"][:60]}
                                          for k, v in cls["unfetchable"].items()}},
                         ensure_ascii=False, indent=2))
        return 0
    if arg.probe is not None:
        syms = arg.probe or ["600177", "600000", "000001"]
        f = make_baostock_fetcher(start="2026-09-17", end="2026-09-22")
        try:
            for s in syms:
                try:
                    df = f(to_baostock_code(s))
                except Exception as e:  # noqa: BLE001
                    print(f"  {s}: EXC {type(e).__name__}: {e}")
                    continue
                print(f"  {s}: {len(df)} 行  cols={list(df.columns)}")
                if len(df):
                    print("     ", df.iloc[-1].to_dict())
                time.sleep(DEFAULT_INTERVAL_MS / 1000.0)
        finally:
            f.close()
        return 0
    print("Baostock 适配器。用 --targets / --limiter / --fields / --probe。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
