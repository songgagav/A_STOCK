# -*- coding: utf-8 -*-
# ============================================================
# margin_sync.py -- 两融(融资融券)数据底座: h5i.margin_daily 建表/回补/每日同步
#
# 背景
# ----
# h5i 库 data/h5i/market.db 此前没有两融数据表。本模块按官方交易所"市场级每日汇总"
# 口径把"沪/深/京 三市场两融日序列"落成 h5i.margin_daily:
#   ts     = 信用交易日期(交易日 00:00, h5i 时间列)
#   market = 沪 | 深 | 京   (symbol 可空: 市场级行恒为 NULL)
#   rzye   = 融资余额(元)
#   rzjme  = 融资买入额(元)
#   rqyl   = 融券余量(股)
#   rqylje = 融券余量金额(元, 即"融券余额")
#   rqmcl  = 融券卖出量(股)
#   source = 数据来源腿(sse_summary | szse_summary | bse_summary)
#
# 数据源与单位(2026-09-06 实测, 均已用东财个股明细聚合交叉验证):
#   [沪] akshare.stock_margin_sse(start_date,end_date) 上交所融资融券汇总
#        元/股 原值 (09-04 当日即可见; 与东财沪市聚合差 <0.02%).
#   [深] 深交所 ShowReport 汇总 JSON (CATALOGID=1837_xxpl, tab1)
#        元/股按"亿"量级下发 -> 货币×1e8、余量/卖出量×1e8 (与东财深市聚合量价吻合,
#        汇总额含调出标的余额比个股聚合高约 0.9%).
#        * T+1 发布滞后: 最新交易日当天不发布, 次日可见(代码在下一运行自愈补齐).
#   [京] akshare.stock_margin_bse(date) 北交所融资融券汇总
#        万元/万股下发 -> ×1e4 (09-03 明细聚合 84.75亿 vs 汇总 848589.06万=84.86亿).
#        * 同样 T+1 滞后.
#
# h5i 单调追加约束(实测):
#   append 允许 ts >= 当前表最大 ts 的整批非降序数据; ts < 最大 ts 的"回填"抛
#   InvalidInputError(sort-order violation). 因此:
#     - 本模块只在每日窗口内按 (ts 升序) 逐日整批追加;
#     - 允许对"当前最大日"补缺腿(同一 ts 加行, 实测允许);
#     - 绝不在追加过更新日后回填更早日期 —— 天然把深/京 T+1 延迟纳入水位管理,
#       不会出现"先落沪腿 -> 深腿永远补不进"的死结(运行日窗口=days+1 自愈).
#
# 约束: 仅 h5i + pyarrow/pandas + requests/akshare, 不引用 DuckDB, 无常驻进程,
#       不删除/不改动同文件任何现有表。
#
# CLI:
#   python margin_sync.py create              # 建表(幂等, 已存在则跳过)
#   python margin_sync.py sync  --days 10     # 回补/增量(默认 days=1)
#   python margin_sync.py stats               # 输出 margin_daily 统计
# ============================================================
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

import pandas as pd
import pyarrow as pa

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H5I_PATH = os.path.join(_BASE, "data", "h5i", "market.db")

MARKETS = ("沪", "深", "京")

MARGIN_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us")),
    ("market", pa.string()),
    ("symbol", pa.string()),
    ("rzye", pa.float64()),
    ("rzjme", pa.float64()),
    ("rqyl", pa.float64()),
    ("rqylje", pa.float64()),
    ("rqmcl", pa.float64()),
    ("source", pa.string()),
])
NUM_COLS = ["rzye", "rzjme", "rqyl", "rqylje", "rqmcl"]

_REQ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_REQ_TIMEOUT = 20


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] [margin_sync] {msg}", flush=True)


def _open_db(read_only: bool = False):
    import h5i_db
    return h5i_db.Database(H5I_PATH, read_only=read_only)


def _tables(db) -> set:
    try:
        return {r["table_name"] for _, r in db.sql("SHOW TABLES").to_pandas().iterrows()}
    except Exception:
        return set()


def _max_ts(db, table: str):
    try:
        df = db.sql(f"SELECT MAX(ts) m FROM {table}").to_pandas()
        v = df.iloc[0, 0] if df is not None and len(df) else None
        return None if v is None else pd.Timestamp(v)
    except Exception:
        return None


def _max_day(db, table: str):
    df = db.sql(f"SELECT MAX(CAST(ts AS DATE)) m FROM {table}").to_pandas()
    v = df.iloc[0, 0] if df is not None and len(df) else None
    return None if v is None else (v if isinstance(v, dt.date) else pd.Timestamp(v).date())


def _existing_keys(db, table: str) -> set[tuple[dt.date, str]]:
    """返回 {(ts 日期, market)} 已存在键, 供幂等去重."""
    try:
        df = db.sql(
            f"SELECT DISTINCT CAST(ts AS DATE) d, market FROM {table}"
        ).to_pandas()
        out = set()
        for _, r in df.iterrows():
            d = r["d"]
            d = d if isinstance(d, dt.date) else pd.Timestamp(d).date()
            out.add((d, str(r["market"] or "")))
        return out
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# 交易日历: 以 h5i.daily_bars 已有交易日为准
# ---------------------------------------------------------------------------
def trading_days(db, end: dt.date, n: int) -> list[str]:
    df = db.sql(
        f"SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars "
        f"WHERE CAST(ts AS DATE) <= DATE '{end:%Y-%m-%d}' "
        f"ORDER BY 1 DESC LIMIT {int(n)}"
    ).to_pandas()
    days = []
    for v in df["d"]:
        d = v if isinstance(v, dt.date) else pd.Timestamp(v).date()
        days.append(d.isoformat())
    return list(reversed(days))


# ---------------------------------------------------------------------------
# 各市场"汇总腿"抓取 (返回 row dict 或 None; 单位统一到 元/股)
# ---------------------------------------------------------------------------
def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def fetch_sse_rows(day: str, sse_map: dict | None = None) -> dict | None:
    """沪腿: 上交所融资融券汇总 (stock_margin_sse), 元/股原值."""
    if sse_map is not None and day in sse_map:
        return sse_map[day]
    import akshare as ak
    ymd = day.replace("-", "")
    try:
        df = ak.stock_margin_sse(start_date=ymd, end_date=ymd)
    except Exception as e:  # noqa: BLE001
        _log(f"  沪腿 {day} akshare 失败: {type(e).__name__}: {str(e)[:120]}")
        return None
    if df is None or df.empty:
        return None
    sub = df[df["信用交易日期"].astype(str) == ymd]
    if sub.empty:
        return None
    r = sub.iloc[-1]
    row = {
        "day": day, "market": "沪",
        "rzye": _num(r.get("融资余额")), "rzjme": _num(r.get("融资买入额")),
        "rqyl": _num(r.get("融券余量")), "rqylje": _num(r.get("融券余量金额")),
        "rqmcl": _num(r.get("融券卖出量")), "source": "sse_summary",
    }
    return row if row["rzye"] is not None else None


def fetch_szse_row(day: str) -> dict | None:
    """深腿: 深交所 ShowReport 汇总 JSON (直接请求, 容忍 0 记录/未发布)."""
    url = "https://www.szse.cn/api/report/ShowReport/data"
    params = {"SHOWTYPE": "JSON", "CATALOGID": "1837_xxpl",
              "txtDate": day, "tab1PAGENO": "1", "random": "0.7425245522795993"}
    headers = {"Referer": "https://www.szse.cn/disclosure/margin/margin/index.html",
               "User-Agent": _REQ_UA, "Accept": "application/json, text/javascript, */*; q=0.01",
               "X-Requested-With": "XMLHttpRequest"}
    try:
        import requests
        r = requests.get(url, params=params, headers=headers, timeout=_REQ_TIMEOUT)
        j = r.json()
    except Exception as e:  # noqa: BLE001
        _log(f"  深腿 {day} 请求失败: {type(e).__name__}: {str(e)[:120]}")
        return None
    if not isinstance(j, list) or not j:
        return None
    data = j[0].get("data")
    if not isinstance(data, list) or not data:
        return None  # 交易所未发布(最新交易日 T+1 才可见)
    rec = data[0]
    # 字段: jrrzmr融资买入额 jrrzye融资余额 jrrjmc融券卖出量 jrrjyl融券余量
    #       jrrjye融券余额(金额) jrrzrjye融资融券余额 —— 单位 亿/亿股 -> x1e8
    f1 = _num(rec.get("jrrzmr")); f2 = _num(rec.get("jrrzye"))
    q1 = _num(rec.get("jrrjmc")); q2 = _num(rec.get("jrrjyl"))
    m = _num(rec.get("jrrjye"))
    if f2 is None:
        return None
    def _mul(x, factor=1e8):
        return None if x is None else round(x * factor, 2)
    return {"day": day, "market": "深",
            "rzye": _mul(f2), "rzjme": _mul(f1),
            "rqyl": _mul(q2), "rqylje": _mul(m), "rqmcl": _mul(q1),
            "source": "szse_summary"}


def fetch_bse_row(day: str) -> dict | None:
    """京腿: 北交所融资融券汇总 (akshare.stock_margin_bse), 万元/万股 -> x1e4."""
    import akshare as ak
    ymd = day.replace("-", "")
    try:
        df = ak.stock_margin_bse(date=ymd)
    except Exception as e:  # noqa: BLE001
        _log(f"  京腿 {day} akshare 失败: {type(e).__name__}: {str(e)[:120]}")
        return None
    if df is None or df.empty:
        return None
    r = df.iloc[-1]
    def _mul(v):
        x = _num(v)
        return None if x is None else round(x * 1e4, 2)
    row = {"day": day, "market": "京",
           "rzye": _mul(r.get("融资余额")), "rzjme": _mul(r.get("融资买入额")),
           "rqyl": _mul(r.get("融券余量")), "rqylje": _mul(r.get("融券余额")),
           "rqmcl": _mul(r.get("融券卖出量")), "source": "bse_summary"}
    return row if row["rzye"] is not None else None


# ---------------------------------------------------------------------------
# 窗口采集: 交易日升序, 逐日取可用市场腿
# ---------------------------------------------------------------------------
def _sse_batch_map(days: list[str]) -> dict[str, dict]:
    """一次 akshare 调用取 [first..last] 全部沪腿(交易日升序), 供窗口复用."""
    out: dict[str, dict] = {}
    if not days:
        return out
    try:
        import akshare as ak
        s = days[0].replace("-", "")
        e = days[-1].replace("-", "")
        df = ak.stock_margin_sse(start_date=s, end_date=e)
    except Exception as ex:  # noqa: BLE001
        _log(f"沪腿批量 {days[0]}..{days[-1]} 失败: {type(ex).__name__}: {str(ex)[:120]}")
        return out
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        d0 = str(r.get("信用交易日期") or "")
        if len(d0) == 8:
            d0 = f"{d0[:4]}-{d0[4:6]}-{d0[6:]}"
        if d0 not in days:
            continue
        out[d0] = {"day": d0, "market": "沪",
                   "rzye": _num(r.get("融资余额")), "rzjme": _num(r.get("融资买入额")),
                   "rqyl": _num(r.get("融券余量")), "rqylje": _num(r.get("融券余量金额")),
                   "rqmcl": _num(r.get("融券卖出量")), "source": "sse_summary"}
    return out


def _collect(days: list[str], sse_map: dict) -> tuple[list[dict], dict]:
    """逐日拉取沪/深/京三腿, 返回 (可行行列表(升序), 逐日缺失记录)."""
    rows: list[dict] = []
    missing: dict[str, list] = {}
    for day in days:
        got: list[dict] = []
        sh = sse_map.get(day)
        if sh is not None and sh.get("rzye") is not None:
            got.append(sh)
        sz = fetch_szse_row(day)
        if sz is not None:
            got.append(sz)
        bj = fetch_bse_row(day)
        if bj is not None:
            got.append(bj)
        if got:
            rows.extend(got)
        for m in MARKETS:
            if not any(g["market"] == m for g in got):
                missing.setdefault(m, []).append(day)
    return rows, missing


# ---------------------------------------------------------------------------
# 建表 / 追加
# ---------------------------------------------------------------------------
def create_table() -> dict:
    db = _open_db()
    try:
        if "margin_daily" in _tables(db):
            return {"ok": True, "exists": True, "note": "margin_daily 已存在, 跳过建表"}
        db.create_table("margin_daily", MARGIN_SCHEMA, time_column="ts")
        return {"ok": True, "exists": False, "table": "margin_daily",
                "schema": MARGIN_SCHEMA.names}
    finally:
        db.close()


def _frame_from_rows(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["day"]).dt.normalize().dt.as_unit("us")
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["market"] = df["market"].astype(str)
    df["symbol"] = None
    df["source"] = df["source"].astype(str)
    return df[["ts", "market", "symbol", "rzye", "rzjme", "rqyl", "rqylje",
               "rqmcl", "source"]]


def sync_margin(days: int = 1, day: str | None = None) -> dict:
    """两融增量/回补同步 (h5i 单调整日追加, 幂等).

    day  : 参照日 YYYY-MM-DD (默认 h5i.daily_bars 最新交易日).
    days : 扫描最近 days 个交易日; 单日增量默认 1, 内部自动向更早扩展 1 个交易日
           (覆盖深/京交易所数据 T+1 发布滞后), 实际窗口 = 最近 min(days+1, 可用) 日.

    幂等: 已存在 (ts, market) 的日/腿自动跳过; 仅对"当前水位起(含同 ts 缺腿)"追加.
    """
    t0 = time.time()
    db = _open_db()
    try:
        if "margin_daily" not in _tables(db):
            db.create_table("margin_daily", MARGIN_SCHEMA, time_column="ts")

        # 锚定参照日
        if day:
            end = pd.Timestamp(str(day)[:10]).date()
        else:
            mx = _max_day(db, "daily_bars")
            end = mx or dt.date.today()
        n = max(1, int(days)) + 1  # +1 交易日: 深/京 T+1 滞后自愈窗
        window = trading_days(db, end, n)
        if not window:
            return {"ok": True, "appended": 0, "note": "daily_bars 无交易日窗口"}

        cur_max = _max_day(db, "margin_daily")
        have = _existing_keys(db, "margin_daily")

        sse_map = _sse_batch_map(window)
        rows, missing = _collect(window, sse_map)

        # 幂等 + 水位过滤: 只追加 ts >= 当前水位(含同 ts 缺腿), 更早视为不可回填
        cand: list[dict] = []
        skipped_dup = 0
        water_skip = 0
        for r in rows:
            k = (dt.date.fromisoformat(r["day"]), r["market"])
            if k in have:
                skipped_dup += 1
                continue
            if cur_max is not None and dt.date.fromisoformat(r["day"]) < cur_max:
                water_skip += 1
                continue
            cand.append(r)
        cand.sort(key=lambda r: (r["day"], r["market"]))

        appended = 0
        appended_days: list[str] = []
        if cand:
            # 按 (ts 升序) 整日分批追加: 同日可能多行(沪/深/京)一个 batch
            by_day: dict[str, list[dict]] = {}
            for r in cand:
                by_day.setdefault(r["day"], []).append(r)
            for d0 in sorted(by_day):
                grp = by_day[d0]
                df = _frame_from_rows(grp)
                tbl = pa.Table.from_pandas(df, schema=MARGIN_SCHEMA,
                                           preserve_index=False)
                db.append("margin_daily", tbl)
                appended += len(grp)
                for r_ in grp:
                    have.add((dt.date.fromisoformat(r_["day"]), r_["market"]))
                appended_days.append(d0)
        return {
            "ok": True,
            "table": "margin_daily",
            "ref_day": end.isoformat(),
            "window_days": window,
            "fetched_rows": len(rows),
            "appended": int(appended),
            "appended_days": appended_days,
            "skipped_existing": int(skipped_dup),
            "watermark_skip_older": int(water_skip),
            "h5i_max_day_before": (cur_max.isoformat() if cur_max else None),
            "missing_legs": {m: missing[m] for m in MARKETS if missing.get(m)},
            "note": ("来源: 上交所汇总(沪,即时)/深交所汇总(深,T+1)/北交所汇总(京,T+1); "
                     "货币单位=元, 余量/卖出量单位=股; 深/京最新交易日缺腿由次日运行补齐"),
            "elapsed_s": round(time.time() - t0, 1),
        }
    finally:
        db.close()


def stats() -> dict:
    db = _open_db(read_only=True)
    try:
        if "margin_daily" not in _tables(db):
            return {"ok": False, "error": "margin_daily 尚未建表"}
        df = db.sql(
            "SELECT CAST(ts AS DATE) d, market, COUNT(*) n, "
            "MIN(rzye) min_rzye, MAX(rzye) max_rzye, "
            "COUNT(rzye) rzye_ok, COUNT(rqylje) rqylje_ok "
            "FROM margin_daily GROUP BY 1, 2 ORDER BY 1, 2"
        ).to_pandas()
        agg = db.sql(
            "SELECT COUNT(*) n, MIN(ts) mn, MAX(ts) mx, "
            "COUNT(DISTINCT market) mk FROM margin_daily"
        ).to_pandas().iloc[0]
        return {
            "ok": True,
            "rows": int(agg["n"]),
            "min_day": str(agg["mn"])[:10],
            "max_day": str(agg["mx"])[:10],
            "markets": int(agg["mk"]),
            "per_day_market": df.to_dict(orient="records"),
        }
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("create")
    p = sub.add_parser("sync")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--day", default=None)
    sub.add_parser("stats")
    args = ap.parse_args()
    if args.cmd == "create":
        print(json.dumps(create_table(), ensure_ascii=False, default=str, indent=1))
    elif args.cmd == "sync":
        print(json.dumps(sync_margin(days=args.days, day=args.day),
                         ensure_ascii=False, default=str, indent=1))
    elif args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, default=str, indent=1))
    else:
        ap.print_help()
