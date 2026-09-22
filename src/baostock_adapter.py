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
                on_error=None) -> dict:
    """按 `codes` 逐个取数, 逐只应用限流与退避。**不抛**(逐只失败被收集)。

    `fetch_one(code)` 由调用方注入 —— 真实实现调 baostock, 测试注入假实现。
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
    ap = argparse.ArgumentParser(description="Baostock 适配器契约: 标的分类与限流参数")
    ap.add_argument("--targets", action="store_true", help="用 symbols.parquet 分类标的")
    ap.add_argument("--limiter", action="store_true", help="打印限流参数")
    args = ap.parse_args(argv)
    if args.limiter:
        print(json.dumps({"default_interval_ms": DEFAULT_INTERVAL_MS,
                          "backoff_interval_ms": BACKOFF_INTERVAL_MS,
                          "backoff_after_fails": BACKOFF_AFTER_FAILS,
                          "recover_after_oks": RECOVER_AFTER_OKS,
                          "throttle_hints": list(_THROTTLE_HINTS)},
                         ensure_ascii=False, indent=2))
        return 0
    if args.targets:
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
    print("契约: 限流 / 标的分类 / 缺口分类。用 --targets 或 --limiter。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
