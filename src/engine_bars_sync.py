# -*- coding: utf-8 -*-
"""厂商引擎 SDK 直连的日K摄入（h5i 主写）—— 取代已冻结的离线镜像 kline_parts.

为什么要有这个模块
------------------
生产摄入原本读 `<湖根>/kline_parts/{sh,sz,bj}_*.parquet`。实测（2026-09-20）该镜像：

  · **自 2026-09-04 起冻结**：5548 个分片中 5197 片最后交易日=09-04、341 片=08-28、
    仅 8 片到过 09-03 —— 逐股新鲜度极不均匀；
  · **生成器在本机已不存在**：全仓 + 湖根 + 桌面/文档/下载均无命中，厂商文档也从未
    提及该产物（文档只承认 `/data/`、`/mydb/` 与 SDK/HTTP）；
  · **写入单位曾整体写错**：09-04 那次 5197 片大重写中，405 只 `00xxxx` 深市主板的
    `volume` 被写成「手」（比「股」小 100 倍）而 `amount` 正确 —— 行内自相矛盾；
    h5i **忠实继承了该错误**（见登记册 P1-H5IUNIT-0904）。

而厂商**真正承诺并文档化**的读取接口是 `pybao/stock_sdk.rd`（引擎 `127.0.0.1:7899`），
`local_pull.py` 的快通道 B 早已在用它取估值字段，只是从未用于日K摄入。

单位/口径映射（**已逐项实测，不是照抄**）
----------------------------------------
对 `000001` 在 2026-09-04..09-08 与引擎同日逐字段比对：
  · `volume`  引擎=股，h5i 近年=股（09-04 那批除外）=> **直通，不换算**
  · `amount`  引擎=元，与 h5i 一致（引擎侧有数量化舍入，故 amount 比对需放宽容差）
  · `pct_chg` 引擎=百分数(0.68)，h5i 存 0.684 => 同为百分数 => **映射为 change_pct**
  · `turnover` 引擎=百分数(0.42)；仓库明文约定即「%」
      （`market_panel.py`「turnover(换手率%)」、`db.py`「turnover 单位=%」）
      => **直通**；09-04 存量里那批 0.0042 是**违反自家约定**，不是本模块引入的口径

设计约束（照本项目一贯纪律）
----------------------------
1. **响亮失败**：引擎不可达 / SDK 缺失 / 某交易日取回行数低于 `min_rows_per_day`
   => 抛 `EngineUnavailable` 或返回 `ok=False` 并写明原因，**绝不静默 0 行**；
2. **不破坏 h5i 单调性**：写入一律走 `h5i_sync.append_daily_bars`，它只追加
   `date > 现有最大` 的行（回填旧日期会跳过并告警）；
3. **可注入**：`rd` 可显式传入，使 CI（无引擎、无 stockdb.exe）也能测全部逻辑；
4. **留痕**：返回体含来源、引擎前缀分布、逐日行数、h5i 追加结果。

用法
  python src/engine_bars_sync.py --days 2026-09-09 2026-09-10          # 预演
  python src/engine_bars_sync.py --from 2026-09-09 --to 2026-09-18 --apply
退出码: 0=成功; 1=引擎/SDK 不可用或数据异常; 2=参数错误
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

_APP = os.path.dirname(os.path.abspath(__file__))
if _APP not in sys.path:
    sys.path.insert(0, _APP)

# 引擎侧按前缀取数。北交所两端都是 92xxxx（43/83/87 实测两边皆 0）。
PREFIXES = ("0*", "3*", "6*", "9*")

# 一个正常交易日的全市场行数量级（实测 5480±10）。低于此值视为"引擎给了残数据"，
# 必须响亮失败而不是把残截面写进 h5i —— 残截面会静默污染因子/权重。
MIN_ROWS_PER_DAY = 4000

# 引擎行 -> duck 风格(h5i append_daily_bars 消费) 的字段映射
_FIELD_MAP = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",      # 股
    "amount": "amount",      # 元
    "pct_chg": "change_pct",  # 百分数
    "turnover": "turnover",   # 百分数(仓库明文约定=%)
}
_H5I_COLS = ["symbol", "date", "open", "high", "low", "close",
             "volume", "amount", "change_pct", "turnover"]


class EngineUnavailable(RuntimeError):
    """SDK 缺失或引擎(127.0.0.1:7899)不可达 —— 调用方必须显式处理, 不得吞掉."""


def _lake_root() -> str:
    """本地行情湖根（可移植: 由 STOCKDB_ROOT 指定）。

    与 `free_stockdb_sync._lake_root()` 保持同一纪律: 解析结果**是否真的存在**必须校验,
    并且要**响亮**。否则一旦 STOCKDB_ROOT 丢失(换终端/换任务/重启), 本模块会去一个
    不存在的目录找 pybao, 报出来的是一句 ModuleNotFoundError —— 看起来像"SDK 没装",
    实际是"湖根没配对"。这正是登记册 P2-LAKEROOT 那类病症的第二次现身。
    """
    r = os.environ.get("STOCKDB_ROOT", "").strip()
    src = "env:STOCKDB_ROOT" if r else "默认(<repo>/data/stockdb)"
    root = r or os.path.join(os.path.dirname(_APP), "data", "stockdb")
    try:
        if not os.path.isdir(root):
            from dataguard import warn_once
            warn_once("engine_lake_root_missing",
                      f"[WARN] 行情湖根不存在: {root} (来源={src}) ⇒ pybao/SDK 必然找不到, "
                      f"摄入会失败; 请设置 STOCKDB_ROOT (实测本机在 E:\\A_stockDB), "
                      f"见登记册 P2-LAKEROOT")
    except Exception:  # noqa: BLE001  诊断绝不影响主链路
        pass
    return root


PYBAO_DIR = os.environ.get("PYBAO_DIR", "").strip() or os.path.join(_lake_root(), "pybao")
ENGINE_ENDPOINT = os.environ.get("STOCKDB_ENGINE", "127.0.0.1:7899")


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [engine_bars] {msg}", flush=True)


def load_rd():
    """把 pybao 加入 sys.path 并返回厂商 SDK 的 rd 句柄。失败一律抛 EngineUnavailable。"""
    if PYBAO_DIR not in sys.path:
        sys.path.insert(0, PYBAO_DIR)
    try:
        from stock_sdk import rd  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise EngineUnavailable(
            f"厂商 SDK 不可用: {type(e).__name__}: {e} (PYBAO_DIR={PYBAO_DIR})") from e
    return rd


# 健康探针/交易日历用的参考股票: 均为长期活跃的大盘股, 取并集可规避单只停牌缺口。
REF_SYMBOLS = ("000001", "600000", "600519", "300750")


def engine_available(rd=None, refs=REF_SYMBOLS) -> dict:
    """引擎健康探针（供 daemon 启动闸门与运维自检）。

    返回 {ok, endpoint, day, rows, trading_days, error}。

    **为什么不用"查昨天有没有行"**: 周末与长假必然 0 行, 那只证明"连得上",
    不证明"读得到数据"; 而且春节/国庆有 8–9 天连休, 任何"N 天回溯"的写法都会在
    长假里**误报不健康**, 从而让启动闸门在完全正常的情况下拒绝启动。
    故改用**参考股票的全历史**: 引擎只要在线就必然返回历史, 与今天是否交易日无关,
    并且顺带给出**数据到哪一天**(这正是运维最需要知道的)。
    """
    out = {"ok": False, "endpoint": ENGINE_ENDPOINT, "pybao_dir": PYBAO_DIR,
           "day": None, "rows": 0, "trading_days": 0, "error": None}
    try:
        r = rd if rd is not None else load_rd()
    except EngineUnavailable as e:
        out["error"] = str(e)
        return out
    try:
        tds = engine_trading_days(refs=refs, rd=r)
    except EngineUnavailable as e:
        out["error"] = str(e)
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"引擎连接失败({ENGINE_ENDPOINT}): {type(e).__name__}: {e}"
        return out
    out.update({"ok": True, "day": tds[-1], "trading_days": len(tds), "rows": len(tds)})
    return out


def engine_trading_days(refs=REF_SYMBOLS, rd=None) -> list:
    """从引擎自身的**参考股票全历史**导出权威交易日列表（并集）。

    为什么不靠本地 `trade_calendar.json`: 它是**从数据派生的**, 数据停在哪它就停在哪
    —— 实测 2026-09-19 生成的那份 `last=2026-09-08`, 恰好等于我们最后有数据的日子,
    故它无法告诉我们 09-09..09-18 里哪些是交易日（鸡生蛋）。
    而活跃股票的全历史日期**就是**权威交易日集合; 取若干只并集可规避单只停牌缺口。

    这同时消除了 `fetch_day` 的一处固有歧义: 有了权威交易日, "已知是交易日却取回 0 行"
    才能被判为**硬错误**, 而周末/节假日可以名正言顺地跳过。
    """
    if rd is None:
        rd = load_rd()
    days = set()
    for s in refs:
        try:
            rows = list(rd.vals("日k", s, "*"))
        except Exception as e:  # noqa: BLE001
            raise EngineUnavailable(
                f"取交易日历失败({ENGINE_ENDPOINT}) ref={s}: {type(e).__name__}: {e}") from e
        for r in rows:
            d = str(r.get("date") or "")
            if len(d) == 8 and d.isdigit():
                days.add(d)
    if not days:
        raise EngineUnavailable(
            f"参考股票全历史为空({refs}) —— 引擎在线但无数据, 拒绝据此推断交易日")
    return sorted(days)


def fetch_day(day: str, prefixes=PREFIXES, rd=None):
    """取某交易日的全市场日K, 返回 (DataFrame[duck 风格], meta)。

    `rd` 可注入以便测试。引擎不可达 -> EngineUnavailable;
    取回行数 < MIN_ROWS_PER_DAY -> 同样抛异常(残截面绝不写库)。
    """
    import pandas as pd

    if rd is None:
        rd = load_rd()
    day8 = str(day).replace("-", "")
    if len(day8) != 8 or not day8.isdigit():
        raise ValueError(f"日期非法: {day!r}")

    records, per = [], {}
    for pfx in prefixes:
        try:
            rows = list(rd.vals("日k", pfx, day8))
        except Exception as e:  # noqa: BLE001
            raise EngineUnavailable(
                f"引擎取数失败({ENGINE_ENDPOINT}) day={day8} prefix={pfx}: "
                f"{type(e).__name__}: {e}") from e
        per[pfx] = len(rows)
        records.extend(rows)

    meta = {"day": day8, "endpoint": ENGINE_ENDPOINT,
            "prefix_counts": per, "raw_rows": len(records)}

    if not records:
        raise EngineUnavailable(
            f"day={day8} 取回 0 行 —— 可能是非交易日, 也可能是引擎无数据; "
            f"拒绝把空截面当作成功 (prefixes={list(prefixes)})")

    df = pd.DataFrame(records)
    for src in _FIELD_MAP:
        if src not in df.columns:
            df[src] = None
    out = pd.DataFrame({
        "symbol": df["code"].astype(str).str.zfill(6),
        "date": pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce"),
    })
    for src, dst in _FIELD_MAP.items():
        # 必须显式落成 float64: 引擎把 volume/amount 返回成 Python int, 而 h5i
        # daily_bars 的这几列建表类型是 Float64 —— 直接透传会得到
        # `schema mismatch: expected field volume Float64, got volume Int64`。
        # 现有生产路径从 parquet 读出天然是 float64, 故这个坑**只有换源才会踩到**;
        # 实测首次 --apply 即被 h5i 拒绝(appended=0, 未写入)。
        out[dst] = pd.to_numeric(df[src], errors="coerce").astype("float64")
    out = out.dropna(subset=["date"]).drop_duplicates("symbol", keep="last")
    out = out[_H5I_COLS].sort_values("symbol").reset_index(drop=True)

    meta["rows"] = int(len(out))
    meta["symbols"] = int(out["symbol"].nunique())
    if meta["rows"] < MIN_ROWS_PER_DAY:
        raise EngineUnavailable(
            f"day={day8} 仅取回 {meta['rows']} 行 (< 阈值 {MIN_ROWS_PER_DAY}) "
            f"—— 残截面会静默污染下游, 拒绝写入 (prefix_counts={per})")
    return out, meta


def sync_days(days, prefixes=PREFIXES, rd=None, dry_run: bool = True) -> dict:
    """把若干交易日的日K追加进 h5i daily_bars。

    h5i 侧走 `h5i_sync.append_daily_bars`, 它只追加 `date > 现有最大` 的行 ——
    因此**幂等**(重复跑同一批会被跳过并告警), 且**绝不会**回改历史。
    """
    res = {"ok": True, "dry_run": bool(dry_run), "endpoint": ENGINE_ENDPOINT,
           "days": [], "total_rows": 0, "h5i_max_before": None,
           "h5i_max_after": None, "appended": 0, "skipped_rows": 0, "errors": []}
    try:
        import h5i_sync
    except Exception as e:  # noqa: BLE001
        res.update({"ok": False, "errors": [f"h5i_sync 不可用: {type(e).__name__}: {e}"]})
        return res

    # 先探针: 引擎不可达就不要逐日去撞
    if rd is None:
        try:
            rd = load_rd()
        except EngineUnavailable as e:
            res.update({"ok": False, "errors": [str(e)]})
            return res
    probe = engine_available(rd=rd)
    res["engine_probe"] = probe
    if not probe.get("ok"):
        res.update({"ok": False, "errors": [probe.get("error") or "引擎探针失败"]})
        return res

    res["h5i_max_before"] = str(h5i_sync.max_bar_date(force=True))
    for day in days:
        try:
            df, meta = fetch_day(day, prefixes=prefixes, rd=rd)
        except (EngineUnavailable, ValueError) as e:
            res["days"].append({"day": str(day), "ok": False, "error": str(e)})
            res["errors"].append(f"{day}: {e}")
            res["ok"] = False
            continue
        item = {"day": meta["day"], "ok": True, "rows": meta["rows"],
                "symbols": meta["symbols"], "prefix_counts": meta["prefix_counts"]}
        if dry_run:
            item["h5i_write"] = "skipped (dry-run)"
        else:
            r = h5i_sync.append_daily_bars(df)
            item["h5i_write"] = {k: r.get(k) for k in
                                 ("enabled", "appended", "skipped_rows", "dates", "ok", "error")}
            if not r.get("ok") or r.get("error"):
                res["ok"] = False
                res["errors"].append(f"{day}: h5i 写入异常 {r.get('error')}")
            res["appended"] += int(r.get("appended") or 0)
            res["skipped_rows"] += int(r.get("skipped_rows") or 0)
        res["total_rows"] += meta["rows"]
        res["days"].append(item)
    if not dry_run:
        h5i_sync._reset_cache()
        res["h5i_max_after"] = str(h5i_sync.max_bar_date(force=True))
    return res


def sync_to_latest(rd=None, apply: bool = True, max_days: int = 30,
                   prefixes=PREFIXES) -> dict:
    """把 h5i 追平到引擎当前可用的最新交易日（日常 pipeline 的入口）。

    这是生产日更该调用的形态: 调用方不需要知道"缺哪几天" —— 水线由 h5i 自己给,
    交易日由引擎自己给(见 `engine_trading_days` 里为什么不信本地 trade_calendar)。

    `max_days` 是**防呆上限**: h5i 若落后很久(例如首次接入), 一次拉太多会长时间占住
    日更; 超出时**明确报告被截断**, 而不是悄悄只补一部分。
    """
    import h5i_sync
    before = h5i_sync.max_bar_date(force=True)
    before8 = before.strftime("%Y%m%d") if before else None
    tds = engine_trading_days(rd=rd)
    days = [d for d in tds if before8 is None or d > before8]
    truncated = None
    if len(days) > max_days:
        truncated = len(days) - max_days
        days = days[-max_days:]

    res = {"used": "engine", "h5i_max_before": str(before), "engine_last_day": tds[-1],
           "missing_trading_days": len(days) + (truncated or 0),
           "truncated_days": truncated, "planned_days": days}
    if not days:
        res.update({"ok": True, "appended": 0, "note": "h5i 已追平引擎最新交易日"})
        return res
    sub = sync_days(days, prefixes=prefixes, rd=rd, dry_run=not apply)
    # 注意: sync_days 也返回一个 "days" 键, 但那是**逐日结果字典列表**, 与上面
    # "计划日期字符串列表"语义不同 —— 早期版本用同名键, 结果被 update 覆盖,
    # 使 `len(res["days"]) == 5` 之类的断言读到 0 条。故此处刻意分名。
    res.update(sub)
    res["used"] = "engine"
    res["h5i_max_before"] = str(before)
    res["planned_days"] = days
    return res


def freshness(engine_day: str | None = None, today=None) -> dict:
    """引擎数据是否追平"最后一个**已收盘**的交易日"。

    **为什么不是与 `trade_calendar.days[-1]` 比**（闸门初版的实际故障）:
    那是**官方日历的年尾**（实测 `20261231`），与"引擎此刻该有多少数据"根本不是一回事 ——
    拿它比会把**每一个正常交易日**都判成落后（因为年尾永远远大于今天）。
    实测: 恢复官方日历后, 闸门立刻把一次**完全正常**的启动误拒为
    `引擎数据(20260918) 落后于本仓交易日历末条(20261231)`。
    这正是本项目 METHOD-1 说的: 判据要基于"该值发生时的下游表现",
    而不是拿一个语义不同的字段硬比。

    正确判据: 引擎至少应覆盖**今天之前的最后一个交易日**。
    （今天若已收盘, 引擎通常会覆盖今天, 那也满足 >= 该下界。）
    """
    import datetime as _dt
    out = {"ok": False, "engine_day": str(engine_day) if engine_day else None,
           "expected_day": None, "lag_trading_days": None, "today": None, "error": None}
    today = today or _dt.date.today()
    if isinstance(today, str):
        today = _dt.date.fromisoformat(today.replace("/", "-"))
    out["today"] = today.isoformat()

    try:
        import trading_calendar as TC
        expected = TC.latest_calendar_day(today - _dt.timedelta(days=1))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"交易日历不可用: {type(e).__name__}: {e}"
        return out
    if expected is None:
        out["error"] = "交易日历里找不到今天之前的交易日"
        return out
    out["expected_day"] = expected.strftime("%Y%m%d")

    if not engine_day:
        out["error"] = "未提供引擎数据日"
        return out
    if str(engine_day) < out["expected_day"]:
        out["error"] = (f"引擎数据({engine_day}) 落后于最后已收盘交易日"
                        f"({out['expected_day']}) —— 摄入会滞后")
        try:
            cal = TC._calendar_days() or set()
            out["lag_trading_days"] = sum(
                1 for d in cal if str(engine_day) < d <= out["expected_day"])
        except Exception:  # noqa: BLE001
            pass
        return out
    out["ok"] = True
    return out


def _calendar_days(lo: str, hi: str) -> list:
    a = dt.date.fromisoformat(lo.replace("/", "-"))
    b = dt.date.fromisoformat(hi.replace("/", "-"))
    out, d = [], a
    while d <= b:
        out.append(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return out


def main() -> int:
    import json
    ap = argparse.ArgumentParser(description="厂商引擎 SDK 直连日K -> h5i")
    ap.add_argument("--days", nargs="+", help="显式交易日列表(严格: 取回 0 行即算失败)")
    ap.add_argument("--from", dest="from_", help="起始日 YYYY-MM-DD")
    ap.add_argument("--to", help="结束日 YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="真正写 h5i（默认预演）")
    ap.add_argument("--probe", action="store_true", help="只做引擎健康探针")
    ap.add_argument("--trading-days", action="store_true",
                    help="只打印引擎给出的权威交易日（可配 --from/--to）")
    args = ap.parse_args()

    if args.probe:
        # 探针输出里**一并给出新鲜度判定**, 使 ops/start_daemon.ps1 的闸门只需读 JSON,
        # 不必在 PowerShell 里重做日期比较(那里做不出可测的逻辑)。
        # 退出码固定 0: 这是**诊断**, 放行/拒绝由调用方(闸门)决定。
        p = engine_available()
        p["freshness"] = freshness(p.get("day"))
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return 0

    days, non_trading = [], []
    if args.days:
        days = [str(d) for d in args.days]
    elif args.from_ and args.to:
        lo = args.from_.replace("/", "-")
        hi = args.to.replace("/", "-")
        try:
            all_td = engine_trading_days()
        except EngineUnavailable as e:
            print(json.dumps({"ok": False, "errors": [str(e)]}, ensure_ascii=False, indent=2))
            return 1
        cand = _calendar_days(lo, hi)
        tset = set(all_td)
        days = [d for d in cand if d in tset]
        non_trading = [d for d in cand if d not in tset]
        if not days:
            print(json.dumps({"ok": False, "non_trading_in_range": non_trading,
                              "errors": [f"{lo}..{hi} 内没有任何引擎交易日"]},
                             ensure_ascii=False, indent=2))
            return 1
    else:
        print("[FAIL] 需要 --days 或 --from/--to")
        return 2

    if args.trading_days:
        print(json.dumps({"trading_days": days, "non_trading": non_trading},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"[计划] 交易日 {len(days)} 天: {days}")
    if non_trading:
        print(f"[计划] 区间内非交易日 {len(non_trading)} 天(跳过): {non_trading}")
    res = sync_days(days, dry_run=not args.apply)
    res["non_trading_in_range"] = non_trading
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
