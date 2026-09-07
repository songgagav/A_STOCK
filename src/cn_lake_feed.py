# ============================================================
# cn_lake_feed.py -- CNEquity 数据湖四层真实取数落库
#
# 在本地行情底座 (local_pull.py) 之上, 提供 L1基本面 / L2资金面 /
# L3宏观 / L4舆情 (+风险事件) 四层真实数据回填, 供 LLM / DRL 策略使用。
#
# 数据源 (均已实测可用, 避开被代理阻断的 eastmoney push2his 推送接口):
#   L1 基本面   stock_financial_abstract_ths (同花顺按报告期)
#   L2 资金面   stock_fund_flow_individual (同花顺全市场资金流)
#               stock_fund_flow_industry    (同花顺行业资金流)
#               stock_lhb_detail_em         (龙虎榜, em 常规接口可用)
#   L3 宏观     macro_china_gdp / macro_china_m2_yearly
#   L4 舆情     stock_news_em (个股新闻) / stock_info_cjzc_em (央视财经早餐)
#
# 表命名沿用 local_pull.build_lake_schema 的 L0-L4 分层映射。
# 写入策略: 每表 DELETE + 全量覆盖 (数据量小), 保证 PIT 一致性近似。
#
# 用法:
#   from cn_lake_feed import feed_all_layers
#   feed_all_layers()                          # 全四层
#   feed_all_layers(layers=["L1"], symbols=["000001"])
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
import warnings
from typing import Iterable, Optional

warnings.filterwarnings("ignore")

import duckdb
import pandas as pd

_LOG = logging.getLogger("cn_lake_feed")


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] [cn_lake_feed] {msg}", flush=True)


try:
    from config import DUCKDB_PATH  # noqa: F401
except Exception:  # pragma: no cover
    DUCKDB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "legacy_stockdb.duckdb")


def _connect(read_only: bool = False):
    if not read_only and not os.path.exists(DUCKDB_PATH):
        # [m4] DuckDB 已退役: 严禁自动重建空库文件
        raise FileNotFoundError(f"DuckDB 已退役/不存在: {DUCKDB_PATH}")
    if not read_only:
        os.makedirs(os.path.dirname(DUCKDB_PATH), exist_ok=True)
    return duckdb.connect(DUCKDB_PATH, read_only=read_only)


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]
    ).fetchone() is not None


def _write_df(con, table: str, df: pd.DataFrame, overwrite: bool = True) -> int:
    """把 DataFrame 全量/增量写入 DuckDB 表。返回行数。"""
    if df is None or df.empty:
        return 0
    df = df.copy()
    # 时间列标准化为字符串, 避免类型冲突
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(df[c].dtype, object):
            df[c] = df[c].apply(
                lambda v: None if pd.isna(v) else v
            )
    if overwrite and _table_exists(con, table):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(f"CREATE TABLE {table} AS SELECT * FROM df")
    return int(len(df))


# ---------------------------------------------------------------- L1 基本面
def feed_fundamentals(con, symbols: Iterable[str] | None = None) -> int:
    """同花顺按报告期财务摘要 -> financials。"""
    try:
        import akshare as ak
    except Exception as e:
        _log(f"akshare 不可用: {e}")
        return 0
    targets = [str(s).zfill(6) for s in (symbols or ["000001"])]
    frames = []
    for sym in targets:
        try:
            df = ak.stock_financial_abstract_ths(symbol=sym, indicator="按报告期")
        except Exception as e:
            _log(f"L1 {sym} 失败: {e}")
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        df.insert(0, "symbol", sym)
        frames.append(df)
    if not frames:
        return 0
    all_df = pd.concat(frames, ignore_index=True)
    return _write_df(con, "financials", all_df)


# ---------------------------------------------------------------- L2 资金面
def feed_capital_flow(con, symbols: Iterable[str] | None = None) -> int:
    """同花顺全市场个股资金流 -> money_flow (eastmoney 阻断的同花顺替代)。"""
    try:
        import akshare as ak
    except Exception as e:
        _log(f"akshare 不可用: {e}")
        return 0
    try:
        df = ak.stock_fund_flow_individual(symbol="即时")
    except Exception as e:
        _log(f"L2 全市场资金流失败: {e}")
        return 0
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["symbol"] = df["股票代码"].astype(str).str.zfill(6)
    df["fetch_time"] = dt.datetime.now().isoformat(sep=" ")
    # 规范化数值: 亿/万 -> 元
    for c in ["流入资金", "流出资金", "净额", "成交额"]:
        if c in df.columns:
            df[c] = df[c].apply(_scale_cny)
    return _write_df(con, "money_flow", df)


def feed_capital_flow_industry(con) -> int:
    try:
        import akshare as ak
    except Exception as e:
        _log(f"akshare 不可用: {e}")
        return 0
    try:
        df = ak.stock_fund_flow_industry(symbol="即时")
    except Exception as e:
        _log(f"L2 行业资金流失败: {e}")
        return 0
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["fetch_time"] = dt.datetime.now().isoformat(sep=" ")
    return _write_df(con, "money_flow_estimate", df)


def feed_lhb(con, start: str, end: str) -> int:
    """龙虎榜明细 (em 常规接口, 非 push2his, 可用)。"""
    try:
        import akshare as ak
    except Exception as e:
        _log(f"akshare 不可用: {e}")
        return 0
    frames = []
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    cur = s
    while cur <= e:
        seg_end = min(cur + dt.timedelta(days=29), e)
        try:
            df = ak.stock_lhb_detail_em(
                start_date=cur.strftime("%Y%m%d"), end_date=seg_end.strftime("%Y%m%d")
            )
        except Exception as ex:
            _log(f"L2 龙虎榜 {cur}~{seg_end} 失败: {ex}")
        else:
            if df is not None and not df.empty:
                frames.append(df)
        cur = seg_end + dt.timedelta(days=1)
    if not frames:
        return 0
    return _write_df(con, "lhb", pd.concat(frames, ignore_index=True))


def _scale_cny(v) -> float | None:
    """'1.04亿'/'5833.61万' -> 元。"""
    if v is None or (isinstance(v, str) and str(v).strip() in ("", "-", "nan", "None")):
        return None
    s = str(v).strip()
    mult = 1.0
    if s.endswith("万亿"):
        mult, s = 1e12, s[:-2]
    elif s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return None


# ---------------------------------------------------------------- L3 宏观
def feed_macro_gdp(con) -> int:
    try:
        import akshare as ak
    except Exception as e:
        _log(f"akshare 不可用: {e}")
        return 0
    try:
        df = ak.macro_china_gdp()
    except Exception as e:
        _log(f"L3 GDP 失败: {e}")
        return 0
    return _write_df(con, "macro_gdp", df)


def feed_macro_m2(con) -> int:
    try:
        import akshare as ak
    except Exception as e:
        _log(f"akshare 不可用: {e}")
        return 0
    try:
        df = ak.macro_china_m2_yearly()
    except Exception as e:
        _log(f"L3 M2 失败: {e}")
        return 0
    return _write_df(con, "macro_m2", df)


# ---------------------------------------------------------------- L4 舆情
def feed_news(con, symbols: Iterable[str] | None = None) -> int:
    """个股新闻 (em) -> stock_news。"""
    try:
        import akshare as ak
    except Exception as e:
        _log(f"akshare 不可用: {e}")
        return 0
    targets = [str(s).zfill(6) for s in (symbols or ["000001"])]
    frames = []
    for sym in targets:
        try:
            df = ak.stock_news_em(symbol=sym)
        except Exception as e:
            _log(f"L4 新闻 {sym} 失败: {e}")
            continue
        if df is not None and not df.empty:
            df = df.copy()
            df.insert(0, "symbol", sym)
            frames.append(df)
    if not frames:
        return 0
    return _write_df(con, "stock_news", pd.concat(frames, ignore_index=True))


def feed_news_digest(con) -> int:
    """央视财经早餐 (em) -> news_cctv。"""
    try:
        import akshare as ak
    except Exception as e:
        _log(f"akshare 不可用: {e}")
        return 0
    try:
        df = ak.stock_info_cjzc_em()
    except Exception as e:
        _log(f"L4 财经早餐失败: {e}")
        return 0
    return _write_df(con, "news_cctv", df)


# ---------------------------------------------------------------- 主入口
LAYER_FUNCS = {
    "L1": ["fundamentals", feed_fundamentals],
    "L2": ["capital", feed_capital_flow],
    "L3": ["macro", feed_macro_gdp],
    "L4": ["news", feed_news],
}


def feed_all_layers(layers: list[str] | None = None,
                    symbols: list[str] | None = None,
                    lhb: bool = False,
                    digest: bool = False) -> dict:
    """全四层取数落库。layers 可选 [L1,L2,L3,L4]; 默认全拉。"""
    layers = layers or ["L1", "L2", "L3", "L4"]
    con = _connect(read_only=False)
    result = {}
    started = time.time()
    try:
        for layer in layers:
            if layer == "L1":
                result["L1_financials"] = feed_fundamentals(con, symbols)
            elif layer == "L2":
                n = feed_capital_flow(con, symbols)
                n2 = feed_capital_flow_industry(con)
                result["L2_money_flow"] = n
                result["L2_money_flow_estimate"] = n2
            elif layer == "L3":
                g = feed_macro_gdp(con)
                m = feed_macro_m2(con)
                result["L3_gdp"] = g
                result["L3_m2"] = m
            elif layer == "L4":
                n = feed_news(con, symbols)
                result["L4_stock_news"] = n
        if lhb:
            # 近 30 天龙虎榜
            end = dt.date.today()
            start = end - dt.timedelta(days=30)
            result["L2_lhb"] = feed_lhb(con, start.isoformat(), end.isoformat())
        if digest:
            result["L4_news_cctv"] = feed_news_digest(con)
        result["elapsed_seconds"] = round(time.time() - started, 2)
        result["ok"] = True
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
    finally:
        con.close()
    return result


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="CNEquity 四层取数落库")
    ap.add_argument("--layers", default="L1,L2,L3,L4", help="逗号分隔层, 如 L1,L3")
    ap.add_argument("--symbols", default=None, help="逗号分隔个股, 默认 000001")
    ap.add_argument("--lhb", action="store_true", help="额外拉龙虎榜近30天")
    ap.add_argument("--digest", action="store_true", help="额外拉央视财经早餐")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    syms = a.symbols.split(",") if a.symbols else None
    r = feed_all_layers(layers=[x.strip() for x in a.layers.split(",")],
                        symbols=syms, lhb=a.lhb, digest=a.digest)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))