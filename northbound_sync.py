# -*- coding: utf-8 -*-
# ============================================================
# northbound_sync.py -- 北向(沪深港通)资金流每日续更 -> h5i.northbound_money
#
# 背景
# ----
# h5i.northbound_money 由 migrate_m4_misc_h5i 从 DuckDB 迁入 (表结构 NB_SCHEMA,
# ts=交易日 00:00, 每交易日 4 行: 沪股通/深股通/港股通沪/港股通深)。现有历史到
# 2026-09-05。本模块提供"每日续更": 以东财沪深港通资金流汇总接口
# (akshare.stock_hsgt_fund_flow_summary_em) 探测当天快照, 将 h5i 水位之后新到的
# 完整交易日(整日 4 行)按相同结构 append, 幂等(只追加 > 当前 max(ts) 的整日).
#
# 约束: 仅 h5i + pyarrow/pandas + akshare, 不引用 DuckDB, 无常驻进程, 不删除
#       任何现有表/文件。
#
# CLI:
#   python northbound_sync.py           # 执行一次每日续更
#   python northbound_sync.py --json    # 以 json 输出
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import os

import pandas as pd
import pyarrow as pa

_BASE = os.path.dirname(os.path.abspath(__file__))
H5I_PATH = os.path.join(_BASE, "data", "h5i", "market.db")

# 复用迁移模块的权威表结构, 保证新行与既有 18 列完全一致
from migrate_m4_misc_h5i import NB_SCHEMA, NB_COLS  # noqa: E402


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] [northbound_sync] {msg}", flush=True)


def _open_db(read_only: bool = False):
    import h5i_db
    return h5i_db.Database(H5I_PATH, read_only=read_only)


def _tables(db) -> set:
    try:
        return {r["table_name"] for _, r in db.sql("SHOW TABLES").to_pandas().iterrows()}
    except Exception:
        return set()


def _max_ts_date(db, table: str):
    try:
        df = db.sql(f"SELECT MAX(CAST(ts AS DATE)) m FROM {table}").to_pandas()
        v = df.iloc[0, 0] if df is not None and len(df) else None
        if v is None:
            return None
        return v if isinstance(v, dt.date) else pd.Timestamp(v).date()
    except Exception:
        return None


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _parse_summary_rows(df: pd.DataFrame, trade_date: dt.date,
                        now: dt.datetime) -> list[dict]:
    """把东财汇总接口 4 板块行映射成 northbound_money 字段 (与 update_db 同口径).

    北向(沪股通/深股通)净买额已停更 -> net_buy_amount 置 None; 南向保留成交净买额.
    """
    block_map = {
        "沪股通": "sh_hk_connect", "港股通(沪)": "hk_sh_connect",
        "深股通": "sz_hk_connect", "港股通(深)": "hk_sz_connect",
    }
    mt_map = {
        "沪股通": "沪股通", "港股通(沪)": "港股通沪",
        "深股通": "深股通", "港股通(深)": "港股通深",
    }
    rows = []
    for _, r in df.iterrows():
        block = str(r.get("板块") or "").strip()
        code = block_map.get(block)
        if code is None:
            continue
        direction = str(r.get("资金方向") or "").strip()
        is_north = direction == "北向"
        rows.append({
            "trade_date": trade_date,
            "market_type": mt_map.get(block, block),
            "market_code": code,
            "net_buy_amount": None if is_north else _num(r.get("成交净买额")),
            "buy_amount": None,
            "sell_amount": None,
            "cumulative_net_buy": None,
            "daily_inflow": _num(r.get("资金净流入")),
            "daily_balance": _num(r.get("当日资金余额")),
            "holding_market_value": None,
            "leading_stock": "",
            "leading_change_pct": None,
            "sh_index": None,
            "sh_index_change_pct": _num(r.get("指数涨跌幅")),
            "leading_stock_code": "",
            "source": "akshare_fund_flow_summary_em",
            "ingested_at": now,
        })
    # 期望 4 板块齐全
    if len(rows) < 4:
        got = {r["market_code"] for r in rows}
        need = set(block_map.values())
        missing = need - got
        raise RuntimeError(f"汇总行不齐 (got={len(rows)}, 缺: {sorted(missing)})")
    # 每板块去重(取最后一行)
    out = {}
    for row in rows:
        out[row["market_code"]] = row
    return list(out.values())


def fetch_northbound_daily(day: str | None = None) -> dict:
    """把 max(ts) 之后新到交易日的北向 4 行按同结构追加到 h5i (幂等, 整日批次).

    day: 可选的参照日 YYYY-MM-DD (仅用于快照接口异常时兜底 trade_date);
         实际交易日以接口返回的"交易日"为准。无新交易日 -> 记录"无新增，链路就绪".
    """
    base = {"table": "northbound_money", "ok": False, "appended": 0}
    db = _open_db()
    try:
        if "northbound_money" not in _tables(db):
            base.update({"error": "h5i northbound_money 不存在 (先跑 migrate_m4_misc_h5i.migrate)"})
            return base
        mx = _max_ts_date(db, "northbound_money")
        base["h5i_max_day"] = mx.isoformat() if mx else None
        if mx is None:
            base.update({"error": "h5i northbound_money 为空表, 无法定位水位"})
            return base

        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is None or df.empty:
            base.update({"error": "stock_hsgt_fund_flow_summary_em 返回空",
                         "note": "无新增，链路就绪"})
            return base

        # 交易日: 以接口返回的最大"交易日"为准
        rep = pd.to_datetime(df["交易日"], errors="coerce").dropna()
        reported = rep.max().date() if len(rep) else None
        if reported is None:
            reported = (pd.Timestamp(day).date() if day
                        else dt.date.today())
        base["reported_day"] = reported.isoformat()

        # 幂等: 只接受水位之后的整日
        if reported <= mx:
            base.update({"ok": True, "appended": 0,
                         "skipped_existing": True,
                         "note": f"接口交易日 {reported} 未超过 h5i 水位 {mx}, "
                                 f"无新增，链路就绪"})
            return base

        now = dt.datetime.now()
        rows = _parse_summary_rows(df, reported, now)
        part = pd.DataFrame(rows)
        part["_d"] = pd.to_datetime(part["trade_date"]).dt.normalize()
        part["ts"] = part["_d"].dt.as_unit("us")
        for c in NB_SCHEMA.names:
            if c not in part.columns and c != "ts":
                part[c] = None
        part = part.sort_values(["ts", "market_type", "market_code"])
        part = part[NB_SCHEMA.names]

        db.append("northbound_money",
                  pa.Table.from_pandas(part, schema=NB_SCHEMA, preserve_index=False))
        base.update({"ok": True, "appended": int(len(part)),
                     "day_appended": reported.isoformat(),
                     "market_types": sorted(part["market_type"].astype(str).tolist()),
                     "note": "整日 4 行追加完成"})
        _log(f"northbound 追加 {len(part)} 行 @ {reported} (>水位 {mx})")
        return base
    except Exception as e:  # noqa: BLE001
        base.update({"error": f"{type(e).__name__}: {e}"})
        return base
    finally:
        try:
            db.close()
        except Exception:
            pass


def sync_northbound(day: str | None = None, **_kw) -> dict:
    """run_daily 挂接入口 (非阻断, 风格同 margin_sync)."""
    return fetch_northbound_daily(day=day)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = fetch_northbound_daily(day=a.day)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, default=str, indent=1))
    else:
        print("day_appended =", rep.get("day_appended"),
              "| appended =", rep.get("appended"),
              "| h5i_max_day =", rep.get("h5i_max_day"),
              "| note =", rep.get("note"),
              "| error =", rep.get("error"))
