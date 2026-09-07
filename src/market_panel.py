# -*- coding: utf-8 -*-
# ============================================================
# market_panel.py -- [A层] 市场情绪面板 (全A日频)
#
# 架构定位: 探索与输入层的第一块落地。把"每天市场处于什么状态"
#   从 DuckDB 全A日线里计算出来, 输出一个可读/可查/可被轮动策略
#   引用的市场情绪快照。
#
# 数据源:
#   * daily_bars(全A日线): change_pct(涨跌幅%), turnover(换手率%),
#     amount(成交额元), close, high, low
#   * symbols: name / market / is_active
#
# 输出:
#   * 一个 dict 情绪面板, 含:
#       - 市场宽度  : 上涨/下跌/平盘家数, 涨跌比, 涨停/跌停, 连板高度
#       - 赚钱效应  : 涨幅>3% 家数占比, 中位数涨跌幅, 平均涨跌幅
#       - 成交维度  : 两市总成交额, 分板块(主板/创业板/科创板/北交所)成交分布
#       - 换手热度  : 平均换手, 高换手占比
#       - 时间/来源 : 计算日期, 数据截止日
#   * 写JSON到 data/market/<date>/market_panel.json (供dashboard/复戏接入)
#   * CLI 输出人读表格
#
# 用法:
#   python market_panel.py                    # 用最新交易日
#   python market_panel.py 2026-08-21         # 指定日
#   python market_panel.py --days 5           # 输出最近5日趋势(供D.反馈层基准)
# ============================================================

import os
import sys
import json
import argparse
import datetime

import duckdb
import pandas as pd

from config import DUCKDB_PATH, DATA_DIR

MARKET_DIR = os.path.join(DATA_DIR, "market")


# ---------------- 涨跌停阈值 (A股主板/创业板/科创板/北交所) ----------------
def _limit_pct(market: str, name: str) -> float:
    """估算某只股票的当日涨跌幅限制。ST 5%, 主板 10%, 创业板/科创板 20%, 北交所 30%。
    返回小数(0.10 = 10%)。无精确识别时默认 10%。"""
    n = (name or "").upper()
    if "ST" in n or "退" in n:
        return 0.05
    mk = (market or "").lower()
    if mk in ("bj",):
        return 0.30
    if mk in ("cy", "kcb"):
        return 0.20
    return 0.10


def _norm_date(d) -> str:
    s = str(d).strip()
    if len(s) == 8 and s.isdigit():
        return "{}-{}-{}".format(s[:4], s[4:6], s[6:8])
    return s[:10]


# [m4] DuckDB 退役后 market_panel 默认从 h5i(daily_bars) + symbols.parquet 读;
# 仅显式 BAR_STORE=duck 且文件仍在时走 DuckDB 对照 (输出键/数值一致).
_H5I_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "h5i", "market.db")
_SYM_PARQUET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "h5i", "static", "symbols.parquet")


def _use_duck_source(db_path: str) -> bool:
    """是否走 DuckDB 对照源 (显式 BAR_STORE=duck 且文件仍在)."""
    if os.environ.get("BAR_STORE", "").strip().lower() != "duck":
        return False
    return os.path.exists(db_path)


def _load_day(day: str, db_path: str) -> pd.DataFrame:
    """取指定交易日 daily_bars + symbols(name/market) 宽表 (与 duck 版同列序).

    h5i 主源: daily_bars(ts 时间列 CAST(ts AS DATE)) + symbols.parquet 左连接;
    输出列: symbol, change_pct, turnover, amount, close, name, market.
    """
    if _use_duck_source(db_path):
        con = duckdb.connect(db_path, read_only=True)
        try:
            return con.execute("""
                SELECT b.symbol, b.change_pct, b.turnover, b.amount, b.close,
                       s.name, s.market
                FROM daily_bars b
                LEFT JOIN symbols s ON s.symbol = b.symbol
                WHERE b.date = ? AND b.change_pct IS NOT NULL
            """, [day]).fetchdf()
        finally:
            con.close()
    import h5i_db
    import pyarrow.parquet as pq
    db = h5i_db.Database(_H5I_PATH)
    try:
        df = db.sql(
            "SELECT symbol, change_pct, turnover, amount, close FROM daily_bars "
            f"WHERE CAST(ts AS DATE) = DATE '{day}' AND change_pct IS NOT NULL"
        ).to_pandas()
    finally:
        try:
            db.close()
        except Exception:
            pass
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        sy = pq.read_table(_SYM_PARQUET).to_pandas()[["symbol", "name", "market"]]
        df = df.merge(sy, on="symbol", how="left")
        df["name"] = df["name"].fillna("")
        df["market"] = df["market"].fillna("")
    except Exception:
        df["name"] = ""
        df["market"] = ""
    return df


def compute_market(day: str, db_path: str = DUCKDB_PATH) -> dict:
    """计算某交易日的全A市场情绪面板。day 接受 '2026-08-21' 或 '20260821'。"""
    day = _norm_date(day)
    src = "h5i:{}".format(_H5I_PATH)
    if _use_duck_source(db_path):
        src = "duckdb:{}".format(DUCKDB_PATH)
    df = _load_day(day, db_path)

    if df.empty:
        return {"day": day, "ok": False, "error": "该日无 daily_bars 数据"}

    chg = pd.to_numeric(df["change_pct"], errors="coerce")  # 单位 %
    amt = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    tvr = pd.to_numeric(df["turnover"], errors="coerce").fillna(0.0)
    close = pd.to_numeric(df["close"], errors="coerce")

    # ---- 市场宽度 ----
    up = int((chg > 0).sum())
    down = int((chg < 0).sum())
    flat = int((chg == 0).sum())
    total = len(chg)
    up_ratio = round(up / total * 100, 2) if total else 0.0

    # ---- 涨跌停 (按逐只阈值, 容差0.5%吸收停牌边缘) ----
    limit = df.apply(lambda r: _limit_pct(r["market"], r["name"]), axis=1)
    limit_up = int((chg >= limit * 100 - 0.4).sum())
    limit_down = int((chg <= -limit * 100 + 0.4).sum())

    # ---- 赚钱效应 ----
    strong = int((chg >= 3.0).sum())              # 涨>3%
    strong_ratio = round(strong / total * 100, 2) if total else 0.0
    median_chg = round(float(chg.median()), 2) if not chg.empty else 0.0
    mean_chg = round(float(chg.mean()), 2) if total else 0.0
    decent = int((chg >= 0).sum())                # 非下跌

    # ---- 成交维度 ----
    total_amount_yi = round(float(amt.sum()) / 1e8, 2)  # 元->亿
    board_dist = {}
    for mk in ["SH", "SZ", "BJ"]:
        sub = df[df["market"].astype(str).str.upper() == mk]
        if not sub.empty:
            amt_sub = pd.to_numeric(sub["amount"], errors="coerce").fillna(0.0).sum()
            board_dist[mk] = round(float(amt_sub) / 1e8, 2)
    board_dist["other"] = round(
        float(amt.sum() - sum(board_dist.values()) * 1e8) / 1e8, 2)

    # ---- 换手环境 ----
    avg_turnover = round(float(tvr.mean()), 2) if total else 0.0
    high_turn = int((tvr > 10.0).sum())           # 换手>10% 活跃股数
    high_turn_ratio = round(high_turn / total * 100, 2) if total else 0.0

    # ---- 综合情绪判别 (0~100) ----
    # 分量:  上涨占比(0~100) + 中位数涨幅(映射0~100) + 强上涨占比(0~100) + 涨停家数
    median_comp = 50.0 + median_chg * 20.0            # 中位0%->50, +2.5%->100, -2.5%->0
    median_comp = max(0.0, min(100.0, median_comp))
    strong_comp = min(100.0, strong_ratio * 5.0)       # 涨>=3%占20%->满分
    lu_comp = min(100.0, limit_up * 2.0)               # 50家涨停->满分
    senti = round(
        0.35 * up_ratio
        + 0.25 * median_comp
        + 0.25 * strong_comp
        + 0.15 * lu_comp,
        1)
    senti = max(1.0, min(99.0, senti))

    return {
        "day": day,
        "ok": True,
        "source": src,
        "market_width": {
            "total": total,
            "up": up, "down": down, "flat": flat,
            "up_ratio": up_ratio,
            "up_down_ratio": round(up / down, 2) if down else None,
        },
        "limit": {
            "limit_up": limit_up,
            "limit_down": limit_down,
            "limit_up_ratio": round(limit_up / total * 100, 2) if total else 0.0,
        },
        "breadth": {
            "median_chg": median_chg,
            "mean_chg": mean_chg,
            "strong_up_count": strong,
            "strong_up_ratio": strong_ratio,
            "not_down_count": decent,
            "not_down_ratio": round(decent / total * 100, 2) if total else 0.0,
        },
        "turnover": {
            "total_amount_yi": total_amount_yi,
            "avg_turnover": avg_turnover,
            "high_turnover_count": high_turn,
            "high_turnover_ratio": high_turn_ratio,
        },
        "sector": board_dist,
        "sentiment_score": senti,
    }


def main():
    ap = argparse.ArgumentParser(description="全A市场情绪面板")
    ap.add_argument("day", nargs="?", help="交易日 YYYY-MM-DD 或 YYYYMMDD; 缺省用最新")
    ap.add_argument("--days", type=int, default=1,
                    help="往回输出的交易日数(>1 输出最近N日趋势, 用于D层基准)")
    args = ap.parse_args()

    day = _norm_date(args.day) if args.day else None
    if _use_duck_source(DUCKDB_PATH):
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            rows = con.execute("SELECT DISTINCT date FROM daily_bars "
                               "ORDER BY date DESC LIMIT ?",
                               [max(1, args.days)]).fetchall()
            dates = [d[0].isoformat() if hasattr(d[0], "isoformat") else str(d[0])
                     for d in rows]
        finally:
            con.close()
    else:
        import h5i_db
        db = h5i_db.Database(_H5I_PATH)
        try:
            # DataFusion SQL 不支持 LIMIT 参数绑定; 读回后在 pandas 端裁剪
            df = db.sql("SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars "
                        "ORDER BY d DESC").to_pandas()
        finally:
            db.close()
        dates = [str(x) for x in df["d"].tolist()][:max(1, args.days)]

    day = day or dates[0]
    if args.days > 1:
        target_days = [d for d in dates if d <= day][:args.days]
        for d in reversed(target_days):
            r = compute_market(d)
            _print(r)
        return 0

    r = compute_market(day)
    if not r.get("ok"):
        print("错误: {}".format(r.get("error")))
        return 1
    os.makedirs(MARKET_DIR, exist_ok=True)
    out_dir = os.path.join(MARKET_DIR, r["day"].replace("-", ""))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "market.json"), "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    _print(r)
    print("\n已写: {}".format(os.path.join(out_dir, "market.json")))
    return 0


def _print(r):
    """终端的可读输出。"""
    if not r.get("ok"):
        print("day={} 无数据".format(r.get("day")))
        return
    mw = r["market_width"]; br = r["breadth"]; tv = r["turnover"]; lm = r["limit"]
    print("=" * 46)
    print("市场情绪面板  {}".format(r["day"]))
    print("情绪分: {} / 100".format(r["sentiment_score"]))
    print("-" * 46)
    print("宽度: 总 {} | 涨 {} | 跌 {} | 平 {} | 涨占比 {}%".format(
        mw["total"], mw["up"], mw["down"], mw["flat"], mw["up_ratio"]))
    print(" 涨停 {} | 跌停 {} | 涨跌比 {}".format(
        lm["limit_up"], lm["limit_down"], mw["up_down_ratio"]))
    print("赚钱: 中位 {}% | 均值 {}% | 涨>=3% {}只({}%)".format(
        br["median_chg"], br["mean_chg"], br["strong_up_count"], br["strong_up_ratio"]))
    print("成交: 两市 {} 亿 | 平均换手 {}% | 高换手(>10%) {}只".format(
        tv["total_amount_yi"], tv["avg_turnover"],
        tv["high_turnover_count"]))


if __name__ == "__main__":
    sys.exit(main())