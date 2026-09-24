# -*- coding: utf-8 -*-
"""Baostock **按需回填** (2026-09-25, 用户决策: 「做, 但限定为按需回填」)。

## 为什么需要它

2026-09-25 实测: **厂商引擎没有发布 2026-09-23 / 09-24 的日线**
(`fetch_day` 这两天均 `EngineUnavailable: 取回 0 行`, 而 09-22 取回全市场 5481 行)。
`h5i.daily_bars` 水位因此停在 09-22, 落后 **2 个交易日**; 选股仍能跑, 但用的是旧截面
(`target_plan.section_as_of=2026-09-22`, `data_lag_days` 逐日 1 -> 2)。

Baostock 实测**有**这两天(见 `docs/stockdb-source-status.md` §6), 故它是唯一已验证的补数路径。

## 为什么**不能**沿用 `bars_ingest.write_bars` 的追加路径

`append` 是**单调**的(`sort_order_violation`): 水位 09-22 之后只能写 09-23 及以后 ——
听起来刚好够用, 但**它要求写入区间严格从水位之后开始且连续**, 而真实缺口常常落在
水位**之前**(例如水位已到 09-24 而 09-20 缺)。本仓 `bars_ingest.account_gap` 早已
把这类日子命名为 `unfillable_gap` 并注明「需走 h5i_ingest/h5i_rebuild 等 ops 路径」。

实测(隔离临时库, 未碰生产)确认了正确的原语:

```
append(更早的日期) -> InvalidInputError [sort_order_violation]
write(同名表)      -> **整表替换**(行数从 4 掉回 2) —— 绝不可用于补数
plan_replace_range -> 只替换给定 ts 区间, 其余段保留; 且**对同一区间幂等**
```

故本模块用 `h5i_db.Database.plan_replace_range(...).apply()`。

## 三道安全闸门(缺一不可)

1. **默认 dry-run**: 不带 `--write` 时**一个字节都不写**。补数会改动生产行情库,
   必须是一次显式动作。
2. **逐日结构哨兵**: 复用 `bars_ingest.normalize`(源无关归一化 + 单位换算契约)
   与 `data_quality_guard` —— OHLC 关系/非正价/负量/核心 NaN 的日**整日拒写**。
3. **来源留痕(`provenance`)**: `daily_bars` **没有 `source` 列**, 混入的行从表内
   **无法区分来源**。故每次写入都把「哪天、来自 Baostock、多少行、覆盖哪个市场区间」
   追加到 `data/backfill_provenance.jsonl`。不这么做就是本仓明令禁止的**口径漂移**:
   数据看着正常, 只是你永远不知道哪几行不是引擎给的。

## 北交所

**显式不支持**: Baostock 对 `bj.*` 返回空(实测 `query_stock_basic('bj.430047')` 无数据),
覆盖仅沪深。北交所 339 只由 `classify_targets` 归为 `not_requested` 并**原样报出** ——
不静默少写, 也不当成失败。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

SOURCE = "baostock"
PROVENANCE_FP = os.path.join(os.path.dirname(_HERE), "data", "backfill_provenance.jsonl")


def _us(day: str) -> int:
    """`YYYY-MM-DD` -> 该日 00:00 的 epoch 微秒(h5i 的 ts 单位)。"""
    return int(dt.datetime.fromisoformat(str(day)).timestamp() * 1_000_000)


def missing_days(days, present) -> list:
    """在 `days` 里挑出**不在** `present` 中的交易日(纯函数, 便于测试)。

    为什么不用水位比较: 缺口可能**在水位之前**(见模块 docstring) ——
    只比"水位之后"会漏掉中间空洞。这里按"该日到底有没有行"判。
    """
    have = {str(x)[:10] for x in (present or [])}
    return [str(d)[:10] for d in (days or []) if str(d)[:10] not in have]


def backfill_days(days, *, write: bool = False, symbols_df=None,
                  fetch_range=None, normalize=None, guard=None,
                  db=None, max_days: int | None = None,
                  progress=None) -> dict:
    """把 `days` 从 Baostock 补进 `h5i.daily_bars`。

    **依赖可注入**(`fetch_range` / `normalize` / `guard` / `db`): 让"归一化 -> 哨兵 ->
    区间替换 -> 留痕"这条链路能在**不联网、不碰生产库**的条件下被测试。
    生产调用传 None 即走真实实现。

    返回 `{ok, source, written, days: {day: {...}}, bj_not_requested, provenance_fp}`。
    """
    import bars_ingest as _BI
    import data_quality_guard as _DQ
    import baostock_adapter as _BA

    fetch_range = fetch_range or _BA.fetch_range
    normalize = normalize or _BI.normalize
    guard = guard or _DQ.validate_daily_bars

    out = {"ok": False, "source": SOURCE, "write": bool(write), "written": [],
           "days": {}, "bj_not_requested": [], "error": None,
           "provenance_fp": PROVENANCE_FP}
    todo = [str(d)[:10] for d in (days or [])]
    if max_days:
        todo = todo[:int(max_days)]
    if not todo:
        out["error"] = "没有要补的交易日"
        return out

    start, end = min(todo), max(todo)
    # **流式聚合**: 每取到一只立刻折成记录按日累加, 不把 5481 只 DataFrame
    # 同时留在内存里(见 baostock_adapter.fetch_range 的 on_row 说明)。
    per_day: dict[str, list] = {d: [] for d in todo}
    counters = {"symbols": 0, "records": 0}

    def _on_row(bare, df):
        counters["symbols"] += 1
        try:
            recs = df.to_dict("records")
        except Exception:  # noqa: BLE001
            return
        for r in recs:
            d = str(r.get("date") or "")[:10]
            if d in per_day:
                per_day[d].append(r)
                counters["records"] += 1

    fr = fetch_range(sorted(set(_symbols(symbols_df))), start=start, end=end,
                     symbols_df=symbols_df, on_row=_on_row)
    out["bj_not_requested"] = fr.get("not_requested") or []
    out["fetch_status"] = fr.get("status")
    out["fetch_failed_n"] = len(fr.get("failed") or {})
    out["limiter"] = fr.get("limiter")
    out["symbols_with_data"] = counters["symbols"]

    if not counters["symbols"]:
        out["error"] = ("Baostock 一行未取到 —— 拒绝把「没取到」当成「补好了」; "
                        f"failed={list((fr.get('failed') or {}).items())[:3]}")
        return out

    _db = db
    for d in todo:
        recs = per_day.get(d) or []
        info = {"n_symbols": len(recs)}
        if not recs:
            info["status"] = "empty"
            info["note"] = "该日 Baostock 无行(停牌/非交易日/未取到)"
            out["days"][d] = info
            continue
        try:
            # 复用**源无关**归一化: 单位换算/字段映射/符号规范化都在那里。
            # ⚠️ `normalize` 返回 **`(df, meta)` 元组**(与 `engine_bars_sync.fetch_day` 同约定),
            # 不是裸 DataFrame —— 我第一次就写错了, 于是 guard 收到 tuple 而报
            # `AttributeError: 'tuple' object has no attribute ...`, 被包装成 guard_error。
            norm, nmeta = normalize(recs, SOURCE)
            info["normalize"] = {"rows": nmeta.get("rows"),
                                 "symbols": nmeta.get("symbols"),
                                 "min_rows_per_day": nmeta.get("min_rows_per_day")}
        except Exception as e:  # noqa: BLE001
            info["status"] = "normalize_failed"
            info["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            out["days"][d] = info
            continue
        try:
            g = guard(norm)
            info["guard"] = {"ok": bool(g.get("ok")), "reasons": (g.get("reasons") or [])[:3]}
        except Exception as e:  # noqa: BLE001
            info["status"] = "guard_error"
            info["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            out["days"][d] = info
            continue
        if not info["guard"]["ok"]:
            # 结构哨兵不通过 => **整日拒写**(不写半截, 那正是残截面污染)
            info["status"] = "rejected_by_guard"
            out["days"][d] = info
            continue
        info["status"] = "ready"
        if write:
            try:
                info["write"] = _write_day(d, norm, db=_db)
                info["status"] = "written"
                out["written"].append(d)
                _append_provenance(d, len(norm), out)
            except Exception as e:  # noqa: BLE001
                info["status"] = "write_failed"
                info["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["days"][d] = info
        if progress:
            try:
                progress(d, info)
            except Exception:  # noqa: BLE001
                pass

    out["ok"] = bool(out["written"]) or (not write and all(
        (v.get("status") == "ready") for v in out["days"].values()))
    return out


def _symbols(symbols_df) -> list:
    if symbols_df is None:
        return []
    try:
        return [str(s) for s in symbols_df["symbol"].tolist()]
    except Exception:  # noqa: BLE001
        return []


def _write_day(day: str, frame, *, db=None) -> dict:
    """把某一天的归一化帧**按 ts 区间替换**写进 h5i.daily_bars。

    用 `plan_replace_range` 而不是 `append`: 见模块 docstring —— append 对水位之前的
    日期抛 `sort_order_violation`, 而缺口恰恰可能落在水位之前; `write()` 则是**整表替换**,
    用它等于把全市场历史删掉。
    """
    import h5i_db
    import pyarrow as pa

    if db is None:
        db = h5i_db.Database(os.path.join(os.path.dirname(_HERE), "data", "h5i", "market.db"))
    tbl = pa.Table.from_pandas(frame, preserve_index=False)
    # h5i 的 time_column 是 ts; 只替换这一天 [00:00, 次日 00:00) 的区间
    start = _us(day)
    end = _us((dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()) - 1
    plan = db.plan_replace_range("daily_bars", start, end, data=tbl,
                                 note=f"backfill {SOURCE} {day}")
    res = plan.apply()
    return {"rows": int(tbl.num_rows), "summary": plan.summary, "apply": res}


def _append_provenance(day: str, rows: int, ctx: dict) -> None:
    """把"这天来自 Baostock"写进留痕文件。**失败不阻断写入**(但会记进返回)。"""
    rec = {"at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "day": day,
           "source": SOURCE, "rows": int(rows),
           "adjustflag": "3", "endpoint": "baostock",
           "note": "厂商引擎未发布该日; 本行来自 Baostock, 非引擎口径",
           "bj_not_requested": len(ctx.get("bj_not_requested") or [])}
    try:
        os.makedirs(os.path.dirname(PROVENANCE_FP), exist_ok=True)
        with open(PROVENANCE_FP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        rec["_write_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        ctx.setdefault("provenance_errors", []).append(rec)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Baostock 按需回填(默认 dry-run)")
    ap.add_argument("--days", nargs="+", required=True,
                    help="要补的交易日 YYYY-MM-DD(可多个)")
    ap.add_argument("--write", action="store_true",
                    help="**真正写入** h5i; 不给则只做 dry-run(归一化+哨兵, 不落盘)")
    ap.add_argument("--max-days", type=int, default=None)
    a = ap.parse_args(argv)

    # symbols.parquet: 用于区分市场并显式报出北交所不可得
    symbols_df = None
    try:
        import pandas as pd
        p = os.path.join(os.path.dirname(_HERE), "data", "h5i", "static", "symbols.parquet")
        if os.path.isfile(p):
            symbols_df = pd.read_parquet(p)
    except Exception as e:  # noqa: BLE001
        print(f"[backfill] symbols.parquet 读取失败(北交所将无法显式归类): {e}", flush=True)

    def _prog(d, info):
        print(f"  {d}: {info.get('status')} {json.dumps({k: v for k, v in info.items() if k != 'guard'}, ensure_ascii=False)[:200]}", flush=True)

    res = backfill_days(a.days, write=a.write, symbols_df=symbols_df, progress=_prog)
    print(json.dumps({k: v for k, v in res.items() if k != "days"}, ensure_ascii=False, indent=2)[:2000])
    if res.get("bj_not_requested"):
        print(f"\n北交所 {len(res['bj_not_requested'])} 只**不在此路覆盖**: Baostock 仅沪深 "
              f"(显式报出, 不静默少写)", flush=True)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
