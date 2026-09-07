# ============================================================
# vnpy_backtest_universe.py -- 基于现有全量 bars 跑 vnpy 全市场回测
#
# 与 vnpy_backtest.py 区别:
#   - 不依赖 selection.json, universe 直接来自 DuckDB / ArcticDB 全市场
#   - 数据源可选: --source duckdb | arcticdb (默认 duckdb, 验证两边一致性)
#   - 标的规模: --universe top50 | top200 | top500 | all
#   - 权重: 等权 (回测验证用, 非策略权重)
#   - 输出: data/vnpy_backtest_universe/<YYYYMMDD>/summary.json + curve.json
#     + ArcticDB bars 缓存 + daily_summary
#
# 用途:
#   验证 ArcticDB 全量数据可用性 + vnpy engine 能否扛住百-千级标的批量回测.
# ============================================================

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import duckdb
import pandas as pd
import polars as pl

from config import DATA_DIR, DUCKDB_PATH


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [universe] {msg}", flush=True)


# ---------- 数据源: DuckDB ----------
def _duckdb_universe(limit: int | None, end_day: dt.date, min_history: int = 60) -> list[str]:
    """DuckDB daily_bars 取出最近 N 日有数据且历史足够长的 symbol 列表 (canon)."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        sql = """
            SELECT symbol,
                   COUNT(*) AS n,
                   MAX(date) AS last_dt
            FROM daily_bars
            WHERE date IS NOT NULL AND date <= ?
            GROUP BY symbol
            HAVING n >= ? AND last_dt >= ?
            ORDER BY n DESC
        """
        # 只要最近 30 天内有 bar 的就算"活跃"
        cutoff = end_day - dt.timedelta(days=30)
        rows = con.execute(sql, [end_day, min_history, cutoff]).fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass
    syms6 = [r[0] for r in rows]
    if limit:
        syms6 = syms6[:limit]
    return [_s6_to_canon(s) for s in syms6]


def _duckdb_bars(sym6: str, end_day: dt.date, lookback_days: int) -> pd.DataFrame:
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        df = con.execute(
            "SELECT date, open, high, low, close, volume, amount FROM daily_bars "
            "WHERE symbol=? AND date<=? ORDER BY date DESC",
            [sym6, end_day],
        ).fetchdf()
    finally:
        try:
            con.close()
        except Exception:
            pass
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").tail(lookback_days).reset_index(drop=True)
    return df


# ---------- 数据源: ArcticDB ----------
def _arcticdb_universe(limit: int | None, end_day: dt.date, min_history: int = 60) -> list[str]:
    """ArcticDB bars lib 所有 symbol + 按历史长度过滤."""
    from arctic_store import get_store
    store = get_store()
    syms = store.list_bars_symbols()
    out = []
    for s in syms:
        try:
            df = store.read_bars(s)
            if df is None or df.empty or len(df) < min_history:
                continue
            if df.index.max().date() < end_day - dt.timedelta(days=30):
                continue
            out.append(s)
        except Exception:
            continue
    if limit:
        out = out[:limit]
    return out


def _arcticdb_bars(canon: str, end_day: dt.date, lookback_days: int) -> pd.DataFrame:
    from arctic_store import get_store
    store = get_store()
    end_ts = pd.Timestamp(end_day)
    start_ts = end_ts - pd.Timedelta(days=int(lookback_days * 1.6))  # 多取一些防遗漏
    try:
        df = store.read_bars(canon, date_range=(start_ts, end_ts))
    except Exception:
        df = None
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    # 列名标准化: date/open/high/low/close/volume/amount
    rename = {}
    if "date" in df.columns:
        pass
    elif "datetime" in df.columns:
        rename["datetime"] = "date"
    df = df.rename(columns=rename)
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c not in df.columns and c.upper() in df.columns:
            df[c] = df[c.upper()]
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").tail(lookback_days).reset_index(drop=True)


# ---------- 工具 ----------
def _s6_to_canon(s6: str) -> str:
    s = str(s6).zfill(6)
    if "." in s6:
        return s6
    if s.startswith(("60", "68", "90")):
        return f"{s}.SH"
    if s.startswith(("0", "3", "2")):
        return f"{s}.SZ"
    if s.startswith(("4", "8", "92")):
        return f"{s}.BJ"
    return f"{s}.SH"


def _ex_value(s6: str) -> str:
    if s6.startswith(("5", "6", "7")) and not s6.startswith(("688",)):
        return "SSE"
    if s6.startswith(("688",)):
        return "SSE"
    if s6.startswith(("0", "3", "2")):
        return "SZSE"
    if s6.startswith(("8", "43", "92")):
        return "BSE"
    return "SSE"


# ---------- 主流程 ----------
def run_universe_backtest(day: str,
                          universe_size: str,
                          source: str,
                          lookback_days: int = 120,
                          max_positions: int | None = None) -> dict:
    """回测入口.

    universe_size: top50 / top200 / top500 / all / N (数字)
    source: duckdb | arcticdb
    """
    day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
    day_dir = day.replace("-", "")

    # 解析 universe 规模
    size_map = {"top50": 50, "top200": 200, "top500": 500, "all": None}
    if universe_size in size_map:
        limit = size_map[universe_size]
    else:
        try:
            limit = int(universe_size)
        except Exception:
            limit = 50

    # 取 universe
    t0 = time.time()
    if source == "duckdb":
        canons = _duckdb_universe(limit, day_dt, min_history=60)
        _loader = lambda c: _duckdb_bars(c.split(".")[0], day_dt, lookback_days)
    elif source == "arcticdb":
        canons = _arcticdb_universe(limit, day_dt, min_history=60)
        _loader = lambda c: _arcticdb_bars(c, day_dt, lookback_days)
    else:
        return {"ok": False, "error": f"未知数据源: {source}"}

    _log(f"universe={universe_size} 实际 {len(canons)} 只, 数据源={source}, "
         f"取数 {time.time()-t0:.1f}s")

    if not canons:
        return {"ok": False, "error": "universe 为空"}

    # 控仓上限 (避免一次建仓 6000 只把 vnpy 拖垮)
    if max_positions and len(canons) > max_positions:
        # 取历史最长的前 max_positions 只
        canons = canons[:max_positions]
        _log(f"启用 max_positions 限制, 实际回测 {len(canons)} 只")

    # 预加载所有 bar
    t1 = time.time()
    bar_map: dict[str, pd.DataFrame] = {}
    fail_count = 0
    for c in canons:
        df = _loader(c)
        if df is None or df.empty:
            fail_count += 1
            continue
        bar_map[c.split(".")[0]] = df
    _log(f"加载 bars 完成: 成功 {len(bar_map)}/{len(canons)} (失败 {fail_count}), "
         f"用时 {time.time()-t1:.1f}s")

    if not bar_map:
        return {"ok": False, "error": "无任何标的 bar 数据"}

    # 选择实际建仓标的 (按 vnpy 处理量, 默认 100)
    n_pos = min(max_positions or 100, len(bar_map))
    symbol6_list = sorted(bar_map.keys())[:n_pos]
    canon_list = [_s6_to_canon(s) for s in symbol6_list]
    weights = [1.0 / len(symbol6_list)] * len(symbol6_list)

    _log(f"实际建仓标的: {len(symbol6_list)} 只 (等权)")

    # 调 vnpy 回测引擎
    summary = _run_vnpy_engine(
        bar_map=bar_map,
        symbol6_list=symbol6_list,
        canon_list=canon_list,
        weights=weights,
        day=day,
        day_dir=day_dir,
        lookback_days=lookback_days,
        source=source,
        universe_size=universe_size,
    )
    return summary


def _build_strategy_class(vt_symbols: List[str], weights: List[float], day: str):
    from vnpy.alpha import AlphaStrategy

    class SelectionStrategy(AlphaStrategy):
        def on_init(self) -> None:
            self._targets = list(zip(vt_symbols, weights))
            self._bought = set()
            self._trades: list[dict] = []

        def on_bars(self, bars: dict) -> None:
            for tgt_vt, w in self._targets:
                if tgt_vt in self._bought:
                    continue
                if tgt_vt not in bars:
                    continue
                bar = bars[tgt_vt]
                cash = self.get_cash_available()
                price = bar.close_price
                if price and price > 0:
                    qty = int(cash * w / price) // 100 * 100
                    if qty > 0:
                        self.buy(tgt_vt, price, qty)
                        self._bought.add(tgt_vt)

        def on_trade(self, trade) -> None:
            try:
                canon = trade.vt_symbol.split(".")[0]
                self._trades.append({
                    "ts": trade.datetime,
                    "day": day,
                    "symbol": canon,
                    "vt_symbol": trade.vt_symbol,
                    "direction": trade.direction.value if hasattr(trade.direction, "value") else str(trade.direction),
                    "qty": int(trade.volume),
                    "price": float(trade.price),
                    "tradeid": str(trade.tradeid),
                    "source": "vnpy_backtest_universe",
                })
            except Exception as e:
                _log(f"on_trade 收集失败: {e}")

    return SelectionStrategy


def _build_bars_list(bar_map: dict[str, pd.DataFrame], symbol6_list: List[str]):
    from vnpy.trader.constant import Exchange, Interval as VInterval
    from vnpy.trader.object import BarData

    def _exchange(s6: str):
        if s6.startswith(("5", "6", "7")) and not s6.startswith(("688",)):
            return Exchange.SSE
        if s6.startswith(("688",)):
            return Exchange.SSE
        if s6.startswith(("0", "3", "2")):
            return Exchange.SZSE
        if s6.startswith(("8", "43", "92")):
            return Exchange.BSE
        return Exchange.SSE

    bars = []
    for s6 in symbol6_list:
        df = bar_map.get(s6)
        if df is None or df.empty:
            continue
        ex = _exchange(s6)
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        for _, r in df.iterrows():
            d = r["date"]
            bars.append(BarData(
                symbol=s6,
                exchange=ex,
                datetime=d.to_pydatetime() if hasattr(d, "to_pydatetime") else d,
                interval=VInterval.DAILY,
                open_price=float(r["open"]) if r["open"] else 0,
                high_price=float(r["high"]) if r["high"] else 0,
                low_price=float(r["low"]) if r["low"] else 0,
                close_price=float(r["close"]) if r["close"] else 0,
                volume=float(r["volume"]) if r["volume"] else 0,
                turnover=float(r["amount"]) if r["amount"] else 0,
                gateway_name="",
            ))
    return bars


def _run_vnpy_engine(bar_map, symbol6_list, canon_list, weights, day, day_dir,
                     lookback_days, source, universe_size) -> dict:
    from vnpy.alpha import BacktestingEngine, AlphaLab
    from vnpy.trader.constant import Interval as VInterval

    vt_symbols = [f"{s6}.{_ex_value(s6)}" for s6 in symbol6_list]
    lab_path = os.path.join(DATA_DIR, "vnpy_lab_universe")
    if os.path.isdir(lab_path):
        shutil.rmtree(lab_path, ignore_errors=True)
    os.makedirs(lab_path, exist_ok=True)
    lab = AlphaLab(lab_path)

    for vt in vt_symbols:
        lab.add_contract_setting(vt, long_rate=0.00025, short_rate=0.00025,
                                 size=1, pricetick=0.01)

    all_bars = _build_bars_list(bar_map, symbol6_list)
    if not all_bars:
        return {"ok": False, "error": "bar 数据为空"}

    _log(f"构造 BarData: {len(all_bars)} 条, 写入 AlphaLab ...")
    vt_bucket: dict[str, list] = {}
    for b in all_bars:
        vt_bucket.setdefault(b.vt_symbol, []).append(b)
    for vt, blist in vt_bucket.items():
        lab.save_bar_data(blist)
    _log(f"AlphaLab 写入完成: {len(vt_bucket)} 个 vt_symbol")

    sig_rows = []
    for s6, vt in zip(symbol6_list, vt_symbols):
        if s6 not in bar_map:
            continue
        for d in bar_map[s6]["date"]:
            sig_rows.append({"datetime": pd.Timestamp(d), "symbol": vt, "signal": 1.0})
    signal_df = pl.DataFrame(sig_rows) if sig_rows else pl.DataFrame(
        schema={"datetime": pl.Datetime, "symbol": pl.Utf8, "signal": pl.Float64}
    )

    start_dt = bar_map[symbol6_list[0]]["date"].iloc[0].to_pydatetime()
    end_dt = bar_map[symbol6_list[0]]["date"].iloc[-1].to_pydatetime()

    strategy_cls = _build_strategy_class(vt_symbols, weights, day=day)
    eng = BacktestingEngine(lab)
    eng.set_parameters(vt_symbols=vt_symbols, interval=VInterval.DAILY,
                       start=start_dt, end=end_dt, capital=100_000)
    for vt in vt_symbols:
        bars_for_vt = lab.load_bar_data(vt, VInterval.DAILY, start_dt, end_dt)
        for b in bars_for_vt:
            eng.history_data[(b.datetime, vt)] = b
            eng.dts.add(b.datetime)
    eng.add_strategy(strategy_class=strategy_cls, setting={}, signal_df=signal_df)

    t0 = time.time()
    eng.run_backtesting()
    _log(f"vnpy run_backtesting 用时 {time.time()-t0:.1f}s")

    result_df = eng.calculate_result()
    stats = eng.calculate_statistics()

    # vnpy 4.4 result_df 列: date, net_pnl, trade_count ... (balance 在 stats 里是 scalar)
    # 从 net_pnl 自行重建 balance 曲线 (与 stats["end_balance"] 一致)
    curve = []
    try:
        if result_df is not None and not result_df.is_empty():
            cols = result_df.columns
            date_col = next((c for c in ("date", "datetime") if c in cols), None)
            npnl_col = next((c for c in ("net_pnl", "hold_pnl") if c in cols), None)
            if date_col and npnl_col:
                cap = 100_000.0
                cum = 0.0
                rows = list(result_df.iter_rows(named=True))
                # 按日期升序
                rows.sort(key=lambda r: r[date_col])
                for r in rows:
                    cum += float(r[npnl_col] or 0)
                    bal = cap + cum
                    curve.append({
                        "date": str(r[date_col])[:10],
                        "balance": round(bal, 2),
                        "net_pnl": round(float(r[npnl_col] or 0), 2),
                    })
                _log(f"curve 重建: {len(curve)} pts, 起始=100000, "
                     f"末点={curve[-1]['balance'] if curve else None}")
    except Exception as e:
        _log(f"解析 curve 失败: {e}")

    try:
        stats_dict = dict(stats) if stats is not None else {}
    except Exception:
        stats_dict = {"raw": str(stats)}

    # 落盘
    out_dir = os.path.join(DATA_DIR, "vnpy_backtest_universe", day_dir)
    os.makedirs(out_dir, exist_ok=True)

    summary = {
        "ok": True,
        "day": day,
        "engine": "vnpy.alpha.BacktestingEngine",
        "data_source": source,
        "universe_size": universe_size,
        "actual_universe": canon_list,
        "weights": weights,
        "n_bars": len(all_bars),
        "n_symbols_loaded": len(bar_map),
        "n_positions": len(symbol6_list),
        "lookback_days": lookback_days,
        "stats": stats_dict,
        "curve_points": len(curve),
    }

    # 若 vnpy 链路无成交统计, 退化到简化等权回测
    if (not stats_dict.get("total_return") and
            not stats_dict.get("end_balance") and
            not stats_dict.get("total_net_pnl")):
        _log("vnpy 链路无成交统计, 启用自研简化回测")
        fb = _fallback(bar_map, symbol6_list, weights)
        summary["stats"] = fb["stats"]
        summary["curve_points"] = fb["curve_points"]
        summary["curve"] = fb["curve"]
        summary["fallback"] = True
    else:
        summary["curve"] = curve

    # 写文件
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(out_dir, "curve.json"), "w", encoding="utf-8") as f:
        json.dump(summary["curve"], f, ensure_ascii=False, indent=2, default=str)
    _log(f"结果已落盘: {out_dir}")

    # ArcticDB 同步 (尝试, 失败不影响主链路)
    try:
        from arctic_store import get_store
        store = get_store()
        for s6, df in bar_map.items():
            canon = _s6_to_canon(s6)
            try:
                store.write_bars(canon, df)
            except Exception as e:
                _log(f"arcticdb write_bars({canon}) 异常: {e}")
        try:
            # ArcticDB daily_summary key 必须是 pd.to_datetime 可解析; 用 day+索引组合
            store.write_daily_summary(f"{day} 00:00:01", summary)
        except Exception as e:
            _log(f"arcticdb write_daily_summary 异常: {e}")
        _log("ArcticDB 同步完成")
    except Exception as e:
        _log(f"ArcticDB 同步失败: {e}")

    return summary


def _fallback(bar_map, symbol6_list, weights) -> dict:
    dates = set()
    for df in bar_map.values():
        for d in df["date"]:
            dates.add(d)
    dates = sorted(dates)
    initial = 100_000.0
    nav = initial
    curve = []
    for d in dates:
        rets = []
        for s6 in symbol6_list:
            df = bar_map.get(s6)
            if df is None:
                continue
            sub = df[df["date"] <= d]
            if len(sub) < 2:
                continue
            prev = float(sub.iloc[-2]["close"])
            cur = float(sub.iloc[-1]["close"])
            if prev > 0:
                rets.append(cur / prev - 1)
        if rets:
            nav *= (1 + sum(rets) / len(rets))
        curve.append({"date": pd.Timestamp(d).strftime("%Y-%m-%d"),
                      "balance": round(nav, 2)})
    total_return = (nav - initial) / initial * 100
    peak = initial
    max_dd = 0.0
    for pt in curve:
        if pt["balance"] > peak:
            peak = pt["balance"]
        dd = (peak - pt["balance"]) / peak
        if dd > max_dd:
            max_dd = dd
    return {
        "stats": {
            "total_return": total_return,
            "max_drawdown_pct": max_dd * 100,
            "final_balance": nav,
            "n_days": len(curve),
            "method": "fallback_equal_weight",
        },
        "curve_points": len(curve),
        "curve": curve,
    }


def run_fixed_universe(day: str,
                      source: str,
                      fixed_universe_path: str,
                      lookback_days: int = 120,
                      max_positions: int | None = None) -> dict:
    """用给定的 canon 列表跑回测, 数据源可切 duckdb/arcticdb."""
    import json as _json
    day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
    day_dir = day.replace("-", "")

    with open(fixed_universe_path, encoding="utf-8") as f:
        payload = _json.load(f)
    canons = payload["canons"] if isinstance(payload, dict) else payload
    _log(f"fixed universe: {len(canons)} 只, 数据源={source}")

    if source == "duckdb":
        _loader = lambda c: _duckdb_bars(c.split(".")[0], day_dt, lookback_days)
    elif source == "arcticdb":
        _loader = lambda c: _arcticdb_bars(c, day_dt, lookback_days)
    else:
        return {"ok": False, "error": f"未知数据源: {source}"}

    t1 = time.time()
    bar_map: dict[str, pd.DataFrame] = {}
    fail_count = 0
    for c in canons:
        df = _loader(c)
        if df is None or df.empty:
            fail_count += 1
            continue
        bar_map[c.split(".")[0]] = df
    _log(f"加载 bars 完成: 成功 {len(bar_map)}/{len(canons)} (失败 {fail_count}), "
         f"用时 {time.time()-t1:.1f}s")

    if not bar_map:
        return {"ok": False, "error": "无任何标的 bar 数据"}

    n_pos = min(max_positions or 100, len(bar_map))
    symbol6_list = sorted(bar_map.keys())[:n_pos]
    canon_list = [_s6_to_canon(s) for s in symbol6_list]
    weights = [1.0 / len(symbol6_list)] * len(symbol6_list)

    return _run_vnpy_engine(
        bar_map=bar_map,
        symbol6_list=symbol6_list,
        canon_list=canon_list,
        weights=weights,
        day=day,
        day_dir=day_dir,
        lookback_days=lookback_days,
        source=source,
        universe_size=f"fixed_{len(canons)}",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=(dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--universe", default="top200",
                    help="top50 / top200 / top500 / all / 任意 N")
    ap.add_argument("--source", default="duckdb", choices=["duckdb", "arcticdb"])
    ap.add_argument("--lookback", type=int, default=120)
    ap.add_argument("--max-positions", type=int, default=100,
                    help="实际建仓上限 (避免一次拖垮 vnpy)")
    ap.add_argument("--fixed-universe", default=None,
                    help="用外部 canon 列表 (json 文件) 跑回测, 跳过 universe 选择步骤")
    args = ap.parse_args()

    if args.fixed_universe:
        result = run_fixed_universe(
            day=args.day,
            source=args.source,
            fixed_universe_path=args.fixed_universe,
            lookback_days=args.lookback,
            max_positions=args.max_positions,
        )
    else:
        result = run_universe_backtest(
            day=args.day,
            universe_size=args.universe,
            source=args.source,
            lookback_days=args.lookback,
            max_positions=args.max_positions,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()