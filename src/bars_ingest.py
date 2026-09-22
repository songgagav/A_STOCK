# -*- coding: utf-8 -*-
"""日K **源无关**写入层: 归一化契约 + 两道闸门 + 缺口显式报出。

## 这个模块为什么存在

在它之前, "把某天的日K写进 h5i" 这件事**只在 `engine_bars_sync.py` 里存在**,
且与库存引擎(stockdb SDK)耦合。后果: 每接一个新数据源, 都要把
「字段映射 / 单位换算 / 类型落成 / 去重 / 结构校验 / 单调追加」重新写一遍 ——
而这几步里**任何一步写错都会静默产生错数据**(单位差 100 倍、类型不一致被拒、
重复行、脏行入库)。

本模块把这套东西抽成**源无关**的一层: 每个源只提供一份**声明式的源规格**
(`SOURCE_SPECS`), 其余全走同一条路径。

## 铁律(本仓纪律的延续)

1. **所有源经同一条写入路径进 h5i** —— 不各写各的。这样
   `_data_version()` / `data_lag_days` / 降级链 / 结构哨兵**继续生效**。
2. **两道闸门必须保留**:
   · `data_quality_guard` 结构哨兵 —— 任何来源都不可能合法的行(OHLC 关系/非正价/
     负量/核心 NaN)一律**拒绝写入**;
   · **单调追加** —— `h5i_sync.append_daily_bars` 只接受 `date > 现有最大日期`。
3. **单位换算必须显式、按源声明**, 不得由各适配器自行处理 ——
   否则单位错误会以「数据看起来正常但差 100 倍」的形式潜伏。
   实例: akshare 的成交量单位是**手**, 引擎是**股**, 实测比值 100.0001。
4. **缺口必须显式报出**(`unfillable_gap`), 不得静默跳过 ——
   见 `write_bars` 的说明。
"""
from __future__ import annotations

import os
from datetime import datetime

#: h5i `daily_bars` 的列契约(与 `engine_bars_sync._H5I_COLS` 逐字一致)。
#: **顺序有意保持**: 归一化后按此顺序排列, 使写入结果逐位可比。
H5I_COLS = ["symbol", "date", "open", "high", "low", "close",
            "volume", "amount", "change_pct", "turnover"]

#: 每交易日最少行数 —— 低于此视为**残截面**, 拒绝写入。
#: 缺省沿用引擎路径的阈值语义(残截面会静默污染下游)。
DEFAULT_MIN_ROWS_PER_DAY = 1000


def _spec(field_map: dict, *, symbol_key: str = "code",
          symbol_zfill: int = 6, date_format: str = "%Y%m%d",
          volume_mult: float = 1.0, amount_mult: float = 1.0,
          min_rows_per_day: int = DEFAULT_MIN_ROWS_PER_DAY,
          note: str = "") -> dict:
    """构造一份源规格(**声明式**)。"""
    return {"field_map": dict(field_map), "symbol_key": symbol_key,
            "symbol_zfill": int(symbol_zfill), "date_format": date_format,
            "volume_mult": float(volume_mult), "amount_mult": float(amount_mult),
            "min_rows_per_day": int(min_rows_per_day), "note": note}


#: 源名 -> 源规格。**未登记的源一律拒绝**(见 `normalize`)。
#:
#: 为什么要"显式登记"而不是给个默认值: 默认值意味着一个**没验证过的源**
#: 可以悄悄走完整条写入链路。而本仓的教训是"未验证的源不进路由表" ——
#: 单位/复权口径必须逐源确认后才登记进来。
SOURCE_SPECS: dict[str, dict] = {
    # 厂商引擎 SDK(stock_sdk.rd)。字段名与 h5i 同名, 无需换算。
    "stockdb_sdk": _spec(
        {"code": "symbol", "date": "date", "open": "open", "high": "high",
         "low": "low", "close": "close", "volume": "volume", "amount": "amount",
         "pct_chg": "change_pct", "turnover": "turnover"},
        note="厂商引擎 SDK; 已是 h5i 口径(volume=股, amount=元)"),

    # AkShare(东方财富接口)。**单位已实测**: 成交量是"手" ⇒ ×100 转股;
    # 成交额 1:1(元); OHLC/涨跌幅 1:1。见 docs/stockdb-source-status.md。
    "akshare": _spec(
        {"股票代码": "symbol", "日期": "date", "开盘": "open", "最高": "high",
         "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount",
         "涨跌幅": "change_pct", "换手率": "turnover"},
        symbol_key="股票代码", date_format="%Y-%m-%d",
        volume_mult=100.0, amount_mult=1.0,
        note="AkShare 东财接口; **成交量单位是手, 必须 ×100 转股**(实测比值 100.0001); "
             "复权口径须由调用方用 adjust 参数确认(不复权 vs 前复权)"),
}


def known_sources() -> list[str]:
    return sorted(SOURCE_SPECS)


def normalize(df, source: str, *, min_rows_per_day: int | None = None):
    """把**任意源**的日K DataFrame 归一化到 h5i 契约。返回 (out, meta)。

    - 只认 `SOURCE_SPECS` 里登记过的源; 未登记 -> `ValueError`(**响亮失败**)。
    - 缺列补 `None`, 再统一 `pd.to_numeric(...).astype("float64")`。
      **必须显式落成 float64**: 引擎/akshare 都可能把 volume/amount 返回成 int,
      而 h5i 建表类型是 Float64 —— 直接透传会得到
      `schema mismatch: expected field volume Float64, got volume Int64`。
      这个坑**只有换源才会踩到**(存量 parquet 天然 float64), 首次 `--apply` 即被拒。
    - 去重策略保持原样: `dropna(date)` 后按 symbol **保留最后一条**。
    """
    import pandas as pd

    spec = SOURCE_SPECS.get(source)
    if spec is None:
        raise ValueError(
            f"未登记的数据源 {source!r}; 已知源={known_sources()}。"
            f"**新增源必须先声明单位与复权口径再登记** —— 未验证的源不进写入路径")

    if df is None or (hasattr(df, "__len__") and len(df) == 0):
        raise ValueError(f"源 {source!r} 返回空表; 拒绝把空截面当成功")

    # 接受 DataFrame **或** 记录列表 —— 适配器两种都给得出来, 没必要强制其一
    # (引擎路径原本就是先 `pd.DataFrame(records)`)。
    work = df if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
    if work.empty:
        raise ValueError(f"源 {source!r} 返回空表; 拒绝把空截面当成功")

    fm = spec["field_map"]
    sym_key = spec.get("symbol_key")
    # 从 field_map 反查"哪个源列装载 date" —— 映射是 {源列: 目标列}, 不能反过来查。
    # (实测踩到: 首版写成 `fm.get("date")`, 对 stockdb_sdk 恰好成立(其源列名就叫 date),
    #  但对 akshare 直接失败 —— 它的源列名是 `日期`。这种"只在一个源上碰巧对"的写法
    #  正是本模块要消灭的那类耦合。)
    date_key = next((s for s, d in fm.items() if d == "date"), None)

    # ---- 幂等入口 ----
    # [2026-09-22 实测修] `normalize` 必须**既能吃原始记录, 也能吃已归一化的表**。
    #
    # 为什么: 各源适配器(如 `engine_bars_sync.fetch_day`)**先把自己的原始记录归一化**
    # 再交出来, 于是 `write_bars(fetch_day(...))` 拿到的是**已归一化**的
    # `symbol/date/open/...` 表。而本函数的 `field_map["code"] -> "symbol"` 期望源列叫
    # `code` —— 已归一化的表里**没有** `code`, 于是:
    #   code 列补 None -> `.astype(str)` 得到字面量 "None" -> 选出的 symbol 是 "None"
    #   -> `drop_duplicates` 只剩 1 行 -> 归一化出 **0 行**(日期列不在映射里)
    #   -> 被残截面闸门拒为 "仅 0 行"。
    # 实测: `write_bars(fetch_day('20260922'), 'stockdb_sdk')` 报
    # `归一化后仅 0 行 (< 阈值 1000)`, 而 fetch_day 明明返回 5481 行。
    #
    # 判据: 若表里**已有全部目标列**(除 symbol/date 外)且**缺该源的符号列**,
    # 视为已归一化, 直接用目标列, 不再做字段映射。
    _already = (sym_key not in work.columns
                and "symbol" in work.columns and "date" in work.columns
                and all(c in work.columns for c in H5I_COLS
                        if c not in ("symbol", "date")))
    if _already:
        norm_src = work.copy()
        norm_src["symbol"] = work["symbol"].astype(str).str.zfill(spec["symbol_zfill"])
        norm_src["date"] = pd.to_datetime(work["date"], errors="coerce")
        mult = {"volume": spec["volume_mult"], "amount": spec["amount_mult"]}
        for c in H5I_COLS:
            if c in ("symbol", "date"):
                continue
            norm_src[c] = pd.to_numeric(work[c], errors="coerce").astype("float64") \
                * mult.get(c, 1.0)
        out = norm_src.dropna(subset=["date"]).drop_duplicates("symbol", keep="last")
        out = out[H5I_COLS].sort_values("symbol").reset_index(drop=True)
        thr0 = spec["min_rows_per_day"] if min_rows_per_day is None else int(min_rows_per_day)
        meta0 = {"source": source, "rows": int(len(out)),
                 "symbols": int(out["symbol"].nunique()), "min_rows_per_day": thr0,
                 "input": "already_normalized",
                 "date_min": (str(out["date"].min().date()) if len(out) else None),
                 "date_max": (str(out["date"].max().date()) if len(out) else None)}
        if meta0["rows"] < thr0:
            raise ValueError(
                f"源 {source!r} 归一化后仅 {meta0['rows']} 行 (< 阈值 {thr0}) —— "
                f"残截面会静默污染下游, 拒绝写入")
        return out, meta0

    src_cols = set(work.columns)
    work = work.copy()
    for c in fm:
        if c not in src_cols:
            work[c] = None

    sym_key = sym_key or next(
        (s for s, d in fm.items() if d == "symbol"), None)
    if date_key is None:
        raise ValueError(f"源 {source!r} 的规格缺 date 映射 —— 无法归一化")
    if sym_key is None:
        raise ValueError(f"源 {source!r} 的规格缺 symbol 来源 —— 无法归一化")
    out = pd.DataFrame({
        "symbol": work[sym_key].astype(str).str.zfill(spec["symbol_zfill"]),
        "date": pd.to_datetime(work[date_key].astype(str),
                               format=spec["date_format"], errors="coerce"),
    })
    mult = {"volume": spec["volume_mult"], "amount": spec["amount_mult"]}
    for src, dst in fm.items():
        if dst in ("symbol", "date"):
            continue
        out[dst] = pd.to_numeric(work[src], errors="coerce").astype("float64") * mult.get(dst, 1.0)
    for c in H5I_COLS:
        if c not in out.columns:
            out[c] = float("nan")

    out = out.dropna(subset=["date"]).drop_duplicates("symbol", keep="last")
    out = out[H5I_COLS].sort_values("symbol").reset_index(drop=True)

    thr = spec["min_rows_per_day"] if min_rows_per_day is None else int(min_rows_per_day)
    meta = {"source": source, "rows": int(len(out)),
            "symbols": int(out["symbol"].nunique()),
            "min_rows_per_day": thr,
            "date_min": (str(out["date"].min().date()) if len(out) else None),
            "date_max": (str(out["date"].max().date()) if len(out) else None)}
    if meta["rows"] < thr:
        raise ValueError(
            f"源 {source!r} 归一化后仅 {meta['rows']} 行 (< 阈值 {thr}) —— "
            f"残截面会静默污染下游, 拒绝写入")
    return out, meta


def account_gap(days, h5i_max=None, *, write_result: dict | None = None) -> dict:
    """算出"这次写入里哪些天**没能进库**", 并**显式**报出。

    ## 为什么必须有这个函数

    `h5i_sync.append_daily_bars` 是**单调追加**: 它只写 `date > 现有最大日期` 的行,
    其余(`<= max`)一律**跳过并只留一条告警日志**。对日常增量这没问题, 但对**多源路由**
    会产生一个危险假象:

        主源已把水位推到 D, 它漏了 D 之前的某天 X;
        备源能取到 X -> 交给 append -> **被静默跳过** ->
        调用方以为"备源补上了", 实际**什么都没写**。

    故本函数把被跳过的天**显式**分类:
      · `unfillable_gap` —— 天数 <= 水位, **append 原理上无法写入** ⇒ 需走
        `h5i_ingest.py` / `h5i_rebuild.py` 的**运维口径**(手工动作, 不是日常路由动作);
      · `appended_days` —— 真正写入的天。
    调用方**必须**在 `unfillable_gap` 非空时视为**需人介入**, 不得当成成功。
    """
    import pandas as pd

    norm = []
    for d in days or []:
        try:
            norm.append(pd.Timestamp(str(d).replace("/", "-")).normalize())
        except Exception:  # noqa: BLE001
            continue
    mx = None
    if h5i_max is not None:
        try:
            mx = pd.Timestamp(str(h5i_max)).normalize()
        except Exception:  # noqa: BLE001
            mx = None
    if mx is None:
        return {"h5i_max": None, "appended_days": [str(d.date()) for d in sorted(norm)],
                "unfillable_gap": [], "note": "水位未知(库为空) => 全部可写"}
    app, gap = [], []
    for d in sorted(set(norm)):
        (app if d > mx else gap).append(str(d.date()))
    out = {"h5i_max": str(mx.date()), "appended_days": app, "unfillable_gap": gap}
    if gap:
        out["action"] = ("存在 append 原理上无法回填的缺口 ⇒ 走 "
                         "h5i_ingest.py / h5i_rebuild.py 运维口径; "
                         "**不得**把它当成写入成功")
    if write_result is not None:
        out["skipped_rows"] = write_result.get("skipped_rows")
    return out


def write_bars(df, source: str, *, dry_run: bool = True,
               min_rows_per_day: int | None = None,
               h5i_max=None, _append=None) -> dict:
    """**统一写入路径**: 归一化 -> 两道闸门 -> h5i。

    两道闸门都在下游 `h5i_sync.append_daily_bars` 里(它做结构校验 + 单调追加),
    本函数**不重复实现**, 以免两处判据漂移。

    `h5i_max` / `_append` 可注入: 让"缺口核算"这段逻辑**不依赖 h5i 运行时**也能测
    (本仓 `.venv314` 跑测试时没有 `h5i_db`, 不注入就只能靠跳过 —— 而那段逻辑
    恰恰是最该被锁住的: 它决定"缺口是被静默吞掉还是被显式报出")。

    返回 dict:
      {ok, source, dry_run, h5i_max_before/after, appended, skipped_rows,
       days, appended_days, unfillable_gap, meta, error?}
    """
    out = {"ok": False, "source": source, "dry_run": bool(dry_run),
           "h5i_max_before": None, "h5i_max_after": None,
           "appended": 0, "skipped_rows": 0, "days": [],
           "appended_days": [], "unfillable_gap": [], "meta": None, "error": None}
    try:
        import h5i_sync
    except Exception as e:  # noqa: BLE001
        out["error"] = f"h5i_sync 不可用: {type(e).__name__}: {e}"
        return out

    try:
        norm, meta = normalize(df, source, min_rows_per_day=min_rows_per_day)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["meta"] = meta
    days = sorted({str(d.date()) for d in norm["date"]})

    if h5i_max is None:
        try:
            h5i_max = h5i_sync.max_bar_date()
        except Exception:  # noqa: BLE001
            h5i_max = None
    out["h5i_max_before"] = str(h5i_max) if h5i_max else None

    # 缺口分类**先于写入**算好 —— 这样即使写入本身失败, 缺口信息也不丢
    gap = account_gap(days, h5i_max)
    out["appended_days"] = gap["appended_days"]
    out["unfillable_gap"] = gap["unfillable_gap"]
    if gap.get("action"):
        out["action"] = gap["action"]
    out["days"] = days

    if dry_run:
        out.update({"ok": True, "appended": 0, "skipped_rows": 0,
                    "note": "预演(未写入)"})
        return out

    append = _append if _append is not None else h5i_sync.append_daily_bars
    r = append(norm) or {}
    out["appended"] = int(r.get("appended") or 0)
    out["skipped_rows"] = int(r.get("skipped_rows") or 0)
    out["ok"] = bool(r.get("ok", True)) and not r.get("error")
    if r.get("error"):
        out["error"] = str(r["error"])
    if r.get("structural_bad_rows") is not None:
        out["structural_rejected"] = r.get("structural_bad_rows")
    try:
        after = h5i_sync.max_bar_date()
    except Exception:  # noqa: BLE001
        after = None
    out["h5i_max_after"] = str(after) if after else None
    return out


def fetch_and_ingest(date, source: str, fetcher, *, dry_run: bool = True) -> dict:
    """**路由层契约**: 取数 -> 写入, 并显式回答"水位之前的缺口怎么办"。

    `fetcher(date)` 由各源适配器提供, 返回该源的原始 DataFrame。

    返回值的 `status` 是**唯一**该被调用方分支的字段:
      · `"appended"`        —— 有行写入(或预演可写);
      · `"unfillable_gap"`  —— 该日 <= h5i 水位, append **原理上无法回填** ⇒
                               需走 h5i_ingest/h5i_rebuild 运维口径;
      · `"failed"`          —— 取数或写入失败(带 `error`)。
    容器留给调用方的判据: `unfillable_gap` 非空时**不得**当成成功。
    """
    res = {"date": str(date), "source": source, "status": None,
           "appended": 0, "unfillable_gap": [], "error": None}
    try:
        raw = fetcher(date)
    except Exception as e:  # noqa: BLE001
        res.update({"status": "failed", "error": f"取数失败: {type(e).__name__}: {e}"})
        return res
    w = write_bars(raw, source, dry_run=dry_run)
    res["write"] = {k: v for k, v in w.items() if k != "meta"}
    res["appended"] = w.get("appended", 0)
    res["unfillable_gap"] = w.get("unfillable_gap") or []
    if w.get("error"):
        res.update({"status": "failed", "error": w["error"]})
        return res
    if res["unfillable_gap"] and not w.get("appended_days"):
        res["status"] = "unfillable_gap"
        res["action"] = ("该日在水位之前, append 无法回填 ⇒ "
                         "走 h5i_ingest.py / h5i_rebuild.py 运维口径")
        return res
    res["status"] = "appended"
    if res["unfillable_gap"]:
        # 部分可写: 写进去一部分, 剩下的缺口仍需人介入 —— 两者都要说清
        res["action"] = "部分写入; 仍有缺口需走运维口径"
    return res


def _main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="日K源无关写入层: 源规格与缺口核算")
    ap.add_argument("--sources", action="store_true", help="列出已登记的源")
    ap.add_argument("--h5i-max", action="store_true", help="打印 h5i 现有水位")
    args = ap.parse_args(argv)
    if args.sources:
        for name in known_sources():
            s = SOURCE_SPECS[name]
            print(f"  {name:16s} min_rows={s['min_rows_per_day']:6d} "
                  f"vol×{s['volume_mult']:g} amt×{s['amount_mult']:g}")
            print(f"      {s['note']}")
        return 0
    if args.h5i_max:
        try:
            import h5i_sync
            print(json.dumps({"h5i_max_date": str(h5i_sync.max_bar_date())},
                             ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
            return 1
        return 0
    print(f"已登记源: {known_sources()}")
    print(f"h5i 列契约: {H5I_COLS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
