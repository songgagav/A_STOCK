# ============================================================
# trading_calendar.py -- A股交易日历权威判断 (含节假日)
#
# 背景: 原调度(daemon/scheduler_entry/realtime_engine)只用 `weekday() < 5`
#       判断交易日, 导致周四/周日之外的"工作日节假日"(春节/国庆/中秋等)
#       被误判为交易日, 会错误地跑盘前决策 + 盘中执行。
#
# 本模块以 AKShare `tool_trade_date_hist_sina()` 的官方交易日历为准,
#   本地缓存到 data/trade_calendar.json, 并做多层回退:
#     官方日历可用 -> 用日历判断(最准, 覆盖节假日)
#     日历获取失败 -> 用历史缓存(上次抓到的交易日集合)
#     无任何缓存   -> 回退 weekday()<5 (与旧行为一致, 保证可运行)
#
# 对外接口:
#   is_trading_day(day)        今日/指定日是否为真实交易日(含节假日)
#   is_non_trading_day(day)    是否非交易日(周末+节假日) -> 走"数据拉取+训练"维护
#   latest_calendar_day()      官方日历里 <= today 的最近交易日
#   refresh(force=False)       刷新缓存(供守护每日调用; force 忽略缓存年龄)
# ============================================================

import os
import json
from datetime import date, datetime, timedelta

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(CONFIG_DIR, "data")
CAL_FILE = os.path.join(DATA_DIR, "trade_calendar.json")

# 缓存最大年龄: 超过则尝试刷新一次 (官方日历一年发布, 30天刷新足够)
CACHE_MAX_AGE_DAYS = 30


def _empty_set():
    return {"ok": False, "days": set(), "updated": None, "source": None}


def _load_cache() -> dict:
    """读取本地交易日历缓存. 返回 {ok, days:set, updated, source}."""
    try:
        if not os.path.exists(CAL_FILE):
            return _empty_set()
        with open(CAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        days = set(data.get("days", []))
        if not days:
            return _empty_set()
        return {
            "ok": True,
            "days": days,
            "updated": data.get("updated"),
            "source": data.get("source", "cache"),
        }
    except Exception:
        return _empty_set()


def _save_cache(days) -> dict:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CAL_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "days": sorted(days),
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "akshare_tool_trade_date_hist_sina",
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass


def refresh(force: bool = False) -> dict:
    """抓取并缓存官方交易日历. force=True 忽略缓存年龄强制刷新.

    返回 {ok, days, updated, error}. 失败时保留旧缓存, ok=False.
    """
    cache = _load_cache()
    if not force and cache.get("ok"):
        updated = cache.get("updated")
        if updated:
            try:
                upd = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S").date()
                if (date.today() - upd).days <= CACHE_MAX_AGE_DAYS:
                    return cache
            except Exception:
                pass

    try:
        import akshare as ak
        import pandas as pd

        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty or "trade_date" not in df.columns:
            raise RuntimeError("交易日历返回为空或缺 trade_date 列")
        raw = df["trade_date"].astype(str).tolist()
        days = set()
        for s in raw:
            s = s.split(" ")[0].replace("-", "").replace("/", "")
            if len(s) == 8:
                days.add(s)
        if not days:
            raise RuntimeError("交易日历解析出空集合")
        _save_cache(days)
        return {
            "ok": True,
            "days": days,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "akshare (fresh)",
        }
    except Exception as e:
        # 保留旧缓存; 返回失败以便上层决定回退
        if cache.get("ok"):
            cache["error"] = str(e)[:200]
            cache["source"] += " (stale)"
            return cache
        return {"ok": False, "days": set(), "error": str(e)[:200]}


def _calendar_days() -> set:
    """取本地缓存里的交易日集合(YYYYMMDD). 无缓存返回 None."""
    cache = _load_cache()
    if cache.get("ok"):
        return cache["days"]
    return None


def _to_norm(d) -> str:
    """任意日期入参 -> 'YYYYMMDD'."""
    if isinstance(d, str):
        s = d.split(" ")[0].replace("-", "").replace("/", "")
        if len(s) == 8:
            return s
        try:
            dt = datetime.strptime(d.strip()[:10], "%Y-%m-%d")
            return dt.strftime("%Y%m%d")
        except Exception:
            pass
        return None
    if isinstance(d, datetime):
        return d.strftime("%Y%m%d")
    if isinstance(d, date):
        return d.strftime("%Y%m%d")
    return None


def is_trading_day(d) -> bool:
    """指定日(默认今天)是否为真实 A 股交易日.

    逻辑:
      1) 官方日历可用 -> 仅当 d 在官方交易日集合内 且 非周末(双保险)
      2) 否则用 daily_bars 权威数据交叉: 若 d <= 最近日K日, 视为可交易日
      3) 否则回退 weekday<5 (旧行为)
    注: 该函数返回的是"今天是否是正常撮合/选股日". 非交易日请走 must maintenance 分支.
    """
    d = d or date.today()
    norm = _to_norm(d)
    if norm is None:
        return False
    # 归一化为 date 对象用于周末判断
    dd = None
    try:
        dd = datetime.strptime(norm, "%Y%m%d").date()
    except Exception:
        dd = None
    cal = _calendar_days()
    if cal is not None:
        # 官方日历权威; 但兜底: 即使日历缺失当天, 只要周末也必然非交易日
        if dd is not None and dd.weekday() >= 5:
            return False
        return norm in cal
    # 无日历缓存: 用 DuckDB daily_bars 权威交叉(节假日无新日K)
    try:
        from db import StockDB

        con = StockDB()
        try:
            row = con._conn().execute(
                "SELECT MAX(date) FROM daily_bars WHERE date <= ?", [d]
            ).fetchone()
        finally:
            con.close()
        if row and row[0]:
            last = row[0]
            if hasattr(last, "date"):
                last = last.date()
            elif isinstance(last, str):
                last = datetime.strptime(last[:10], "%Y-%m-%d").date()
            # 最近日K距今超过5个自然日 → 大概率长假休息(回到交易日第一天也在此列,
            # 但考虑到日K数据源本身会滞后, 这里偏向"非交易日"以避免误撮合; 数据拉取仍可运行)
            if (d - last).days > 5:
                return False
    except Exception:
        pass
    return d.weekday() < 5


def is_non_trading_day(d) -> bool:
    """是否为非交易日(周末 + 节假日). True 时调度应跳过盘前决策/盘中执行,
    但仍可保留数据拉取与模型训练(见 run_daily.maint 模式)."""
    return not is_trading_day(d)


def latest_calendar_day(d=None) -> "date | None":
    """官方日历里 <= d 的最近一个交易日(date 对象). 无日历缓存返回 None."""
    d = d or date.today()
    norm = _to_norm(d)
    cal = _calendar_days()
    if cal is None or norm is None:
        return None
    les = [x for x in cal if x <= norm]
    if not les:
        return None
    newest = max(les)
    try:
        return datetime.strptime(newest, "%Y%m%d").date()
    except Exception:
        return None