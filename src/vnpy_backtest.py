# ============================================================
# vnpy_backtest.py -- vnpy 独立验证回测 (vnpy 4.4)
# 与 paper_book / backtest_engine 解耦, 仅作为"研究验证"用途.
# 数据源: DuckDB daily_bars; 标的: 当日 selection.json Top N; 权重: 等权.
# 落盘:
#   data/vnpy_backtest/<YYYYMMDD>/{summary.json, curve.json}
#   + ArcticDB bars / trade_records / daily_summary
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from typing import List

import duckdb
import numpy as np
import pandas as pd
import polars as pl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, DAILY_DIR, DUCKDB_PATH, PAPER  # noqa: E402


def _persist_to_arctic(day: str, bar_map: dict, strategy, summary: dict,
                      engine=None) -> dict:
    """回测产物同步到 ArcticDB.
    - bars: 各 symbol 全量日线 (缓存, 下次回测直接读)
    - trade_records: vnpy engine.get_all_trades() 收集的逐笔成交
    - daily_summary: 整段 summary (供 SPC / 退化检测查询)
    失败一律 log.warning, 不抛异常 (vnpy_backtest 主链路已 ok, 不应阻塞).
    返回 {'bars': N, 'trades': M, 'summary': True/False}.
    """
    out = {"bars": 0, "trades": 0, "summary": False}
    try:
        from arctic_store import get_store
        store = get_store()
    except Exception as e:
        _log(f"arctic_store 加载失败: {e}")
        return out
    # 1) bars 缓存 (每只 symbol 写一次)
    for s6, df in (bar_map or {}).items():
        if df is None or df.empty:
            continue
        canon = _s6_to_canon(s6)
        try:
            if store.write_bars(canon, df):
                out["bars"] += 1
        except Exception as e:
            _log(f"write_bars({canon}) 异常: {e}")
    # 2) trade_records: 优先从 engine.get_all_trades(), 否则从 strategy._trades (on_trade hook)
    trades = []
    if engine is not None and hasattr(engine, "get_all_trades"):
        try:
            for t in engine.get_all_trades():
                trades.append(_trade_to_dict(t, day))
        except Exception as e:
            _log(f"engine.get_all_trades() 失败: {e}")
    if not trades:
        trades = getattr(strategy, "_trades", None) or []
    for t in trades:
        try:
            if store.append_trade(t):
                out["trades"] += 1
        except Exception as e:
            _log(f"append_trade 异常: {e}")
    # 3) daily_summary
    try:
        if store.write_daily_summary(day, summary):
            out["summary"] = True
    except Exception as e:
        _log(f"write_daily_summary 异常: {e}")
    _log(f"ArcticDB 同步: bars={out['bars']} trades={out['trades']} summary={out['summary']}")
    return out


def _trade_to_dict(trade, day: str) -> dict:
    """vnpy.TradeData -> arctic_store.append_trade 输入 dict."""
    try:
        vt = getattr(trade, "vt_symbol", "") or ""
        # vt_symbol = "000001.SZSE." -> canon = "000001.SZ"
        parts = vt.split(".")
        canon = ".".join(parts[:2]) if len(parts) >= 2 else vt
        # 也可以去尾, 但保留两位即可表达市场
        if len(parts) == 3 and parts[2] == "":
            # 末尾空字符串 -> 实际是 "000001.SZSE."
            canon = f"{parts[0]}.{parts[1]}"
        direction = getattr(trade.direction, "value", str(trade.direction))
        ts = getattr(trade, "datetime", dt.datetime.now())
        if hasattr(ts, "strftime"):
            day_str = ts.strftime("%Y-%m-%d")
        else:
            day_str = day or str(ts)[:10]
        return {
            "ts": ts,
            "day": day_str or day,
            "symbol": canon,
            "vt_symbol": vt,
            "direction": direction,
            "qty": int(getattr(trade, "volume", 0)),
            "price": float(getattr(trade, "price", 0)),
            "tradeid": str(getattr(trade, "vt_tradeid", "") or getattr(trade, "tradeid", "")),
            "source": "vnpy_backtest",
        }
    except Exception as e:
        _log(f"_trade_to_dict 失败: {e}")
        return {"day": day, "symbol": "unknown", "source": "vnpy_backtest"}


def _s6_to_canon(s6: str) -> str:
    """6 位代码 -> canon. 同 paper_book._canon_of 语义."""
    s = str(s6).zfill(6)
    if s.startswith(("60", "68", "90")):
        return f"{s}.SH"
    if s.startswith(("0", "3")):
        return f"{s}.SZ"
    if s.startswith(("4", "8")):
        return f"{s}.BSE"
    return f"{s}.SZ"


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [vnpy] {msg}", flush=True)


def _load_selection(day_dir: str) -> list[dict]:
    sel_path = os.path.join(DAILY_DIR, day_dir, "selection.json")
    if not os.path.exists(sel_path):
        return []
    with open(sel_path, encoding="utf-8") as f:
        sel = json.load(f)
    return sel.get("top_n", []) or sel.get("targets", []) or []


def _load_bars(symbol6: str, end_day: dt.date, days: int = 120) -> pd.DataFrame:
    """加载日线数据, 包含 change_pct 用于复权重建.
    返回 DataFrame 含列: date, open, high, low, close, volume, amount, change_pct, adj_close.
    """
    # 1) 首选 DuckDB (兼容旧环境)
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            df = con.execute(
                "SELECT date, open, high, low, close, volume, amount, change_pct "
                "FROM daily_bars WHERE symbol=? AND date<=? ORDER BY date DESC",
                [symbol6, end_day],
            ).fetchdf()
        finally:
            con.close()
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").tail(days).reset_index(drop=True)
            _build_adj_close_inplace(df)
            return df
    except Exception:
        pass

    # 2) h5i 主数据源 (daily_bars 在 data/h5i/market.db)
    try:
        from factor_fusion import _sql
        iso = end_day.strftime("%Y-%m-%d")
        df = _sql(
            f"SELECT CAST(ts AS DATE) AS date, open, high, low, close, volume, "
            f"amount, change_pct FROM daily_bars WHERE symbol = '{symbol6}' "
            f"AND CAST(ts AS DATE) <= DATE '{iso}' ORDER BY CAST(ts AS DATE) DESC")
        if df is None or df.empty:
            return df if df is not None else pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").tail(days).reset_index(drop=True)
        _build_adj_close_inplace(df)
        return df
    except Exception:
        return pd.DataFrame()


def _build_adj_close_inplace(df: pd.DataFrame) -> None:
    """用 build_adj_close 在 df 中增加 adj_close 列 (原地修改)."""
    from factor_library import build_adj_close
    df["adj_close"] = build_adj_close(df)


def _build_strategy_class(vt_symbols: List[str], weights: List[float], day: str = ""):
    """vt_symbols + weights: 目标持仓; day: 回测日, 用于写 trade_records 标识."""
    from vnpy.alpha import AlphaStrategy

    class SelectionStrategy(AlphaStrategy):
        def on_init(self) -> None:
            self._targets = list(zip(vt_symbols, weights))
            self._bought = set()
            # 收集逐笔成交 (用于写 ArcticDB trade_records)
            self._trades: list[dict] = []

        def on_bars(self, bars: dict) -> None:
            # 在所有目标的首根 bar 同时建仓
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
            """逐笔成交 hook. 收集数据供 ArcticDB 持久化."""
            try:
                # vnpy.trader.object.TradeData 字段:
                # trade.vt_symbol, trade.direction, trade.price, trade.volume,
                # trade.datetime, trade.tradeid
                canon = ".".join(trade.vt_symbol.split(".")[:-1]) if "." in trade.vt_symbol else trade.vt_symbol
                # canon 转 6位+.SH/.SZ/.BSE
                if "." not in canon:
                    s6 = canon.split(".")[0] if "." in canon else canon
                    canon = s6
                self._trades.append({
                    "ts": trade.datetime,
                    "day": day or (trade.datetime.strftime("%Y-%m-%d") if hasattr(trade.datetime, "strftime") else str(trade.datetime)[:10]),
                    "symbol": canon,
                    "vt_symbol": trade.vt_symbol,
                    "direction": trade.direction.value if hasattr(trade.direction, "value") else str(trade.direction),
                    "qty": int(trade.volume),
                    "price": float(trade.price),
                    "tradeid": str(trade.tradeid),
                    "source": "vnpy_backtest",
                })
            except Exception as e:
                import traceback
                _log(f"on_trade 收集失败: {e}")
                traceback.print_exc()
            return  # 原方法无操作
            pass

    return SelectionStrategy


def _build_bars_list(bar_map: dict[str, pd.DataFrame], symbol6_list: List[str]):
    """构造 BarData 列表, 供 lab.save_bar_data(bars) 使用.
    vnpy 4.4 文件名按 vt_symbol(=symbol.exchange.gateway_name)分组, 这里 gateway_name 留空
    以让 600018.SSE. 这种 vt_symbol 自洽.
    """
    from vnpy.trader.constant import Exchange, Interval as VInterval
    from vnpy.trader.object import BarData

    def _exchange(s6: str):
        if s6.startswith(("5", "6", "7")) and not s6.startswith(("688",)):
            return Exchange.SSE
        if s6.startswith(("688",)):
            return Exchange.SSE
        if s6.startswith(("0", "3")):
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
            # P1: 使用 adj_close (复权价) 而非 raw close, 消除除权跳变
            adj_close = float(r["adj_close"]) if pd.notna(r.get("adj_close")) and r.get("adj_close") else float(r["close"])
            bars.append(BarData(
                symbol=s6,
                exchange=ex,
                datetime=d.to_pydatetime() if hasattr(d, "to_pydatetime") else d,
                interval=VInterval.DAILY,
                open_price=float(r["open"]) if r["open"] else 0,
                high_price=float(r["high"]) if r["high"] else 0,
                low_price=float(r["low"]) if r["low"] else 0,
                close_price=adj_close,
                volume=float(r["volume"]) if r["volume"] else 0,
                turnover=float(r["amount"]) if r["amount"] else 0,
                gateway_name="",
            ))
    return bars


def _vt_symbol(s6: str, ex: Exchange) -> str:
    """根据 symbol6 推导 vnpy vt_symbol, 与 BarData.vt_symbol 一致."""
    from vnpy.trader.utility import extract_vt_symbol
    from vnpy.trader.constant import Exchange
    # BarData.vt_symbol = f"{symbol}.{exchange.value}.{gateway_name}" if gateway_name else f"{symbol}.{exchange.value}."
    # 直接按规则拼
    ex_value = ex.value
    return f"{s6}.{ex_value}."


def _inject_risk_ratios(stats_dict: dict, balances: list) -> None:
    """补齐 vnpy summary 的 Calmar / Sortino (2026-09-07).

    - calmar: 优先用引擎 annual_return(%) / |max_ddpercent(%)| (精确).
    - sortino: 若能取到逐日净值(结算曲线/fallback曲线)则真算; 否则不强填
      (检测层回退近似口径时会如实标注).
    """
    import numpy as np

    def _f(key):
        try:
            v = stats_dict.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    annual = _f("annual_return")          # %
    maxdd = _f("max_ddpercent")           # 负 %
    if annual is not None and maxdd is not None and abs(maxdd) > 1e-9:
        stats_dict["calmar"] = round(annual / abs(maxdd), 4)

    vals = [b for b in balances if b is not None and np.isfinite(float(b))]
    vals = [float(v) for v in vals]
    if len(vals) >= 5:
        eq = np.asarray(vals)
        eq = eq[eq > 0]
        if len(eq) >= 5:
            r = np.diff(eq) / eq[:-1]
            mean_r = float(np.mean(r))
            down = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2)))
            if down > 1e-12:
                stats_dict["sortino_ratio"] = round(
                    mean_r / down * np.sqrt(252.0), 4)


def run_vnpy_backtest(day: str, top_n: int = 10, lookback_days: int = 120) -> dict:
    from vnpy.trader.constant import Exchange
    day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
    day_dir = day.replace("-", "")

    targets = _load_selection(day_dir)
    if not targets:
        return {"ok": False, "error": "selection.json 无目标池", "rows": 0}
    targets = targets[:top_n]
    canon_list = [t["canon"] for t in targets]
    symbol6_list = [c.split(".")[0] for c in canon_list]

    def _ex_of(s6: str) -> Exchange:
        if s6.startswith(("5", "6", "7")) and not s6.startswith(("688",)):
            return Exchange.SSE
        if s6.startswith(("688",)):
            return Exchange.SSE
        if s6.startswith(("0", "3")):
            return Exchange.SZSE
        if s6.startswith(("8", "43", "92")):
            return Exchange.BSE
        return Exchange.SSE

    # vnpy 4.4: vt_symbol = symbol.exchange (BarData.vt_symbol 公式)
    vt_symbols = [f"{s6}.{_ex_of(s6).value}" for s6 in symbol6_list]
    weights = [1.0 / len(targets)] * len(targets)

    bar_map: dict[str, pd.DataFrame] = {}
    for s6 in symbol6_list:
        df = _load_bars(s6, day_dt, lookback_days)
        if not df.empty:
            bar_map[s6] = df
    if not bar_map:
        return {"ok": False, "error": "无标的日线数据", "rows": 0}

    try:
        from vnpy.alpha import BacktestingEngine, AlphaLab
        from vnpy.trader.constant import Interval as VInterval
        lab_path = os.path.join(DATA_DIR, "vnpy_lab")
        # 清理旧 lab 防止 dataset 残留
        import shutil
        if os.path.isdir(lab_path):
            shutil.rmtree(lab_path, ignore_errors=True)
        os.makedirs(lab_path, exist_ok=True)
        lab = AlphaLab(lab_path)

        # 1) 注册合约配置 (A股税费模型, 与 config.PAPER 实盘撮合对齐):
        #    佣金 commission(双边), 印花税 stamp_tax(卖出单边), 过户费 transfer_fee(双边),
        #    滑点 slippage(实盘计入成交价, 回测近似为双向费率), 冲击成本 impact_cost(单边成交额).
        #    vnpy 只支持按买卖方向各一个费率(long_rate=买入, short_rate=卖出),
        #    无法表达最低5元佣金与滑点(滑点通过费率近似, 见 config.PAPER 说明)。
        #    买入: commission + transfer_fee + slippage + impact_cost
        #    卖出: commission + stamp_tax + transfer_fee + slippage + impact_cost
        #
        # [2026-09-07 P1] 用 Almgren-Chriss 动态滑点替代固定万分之五: 从 bar_map 取
        # 各标的近 20 日均成交额和波动率, 按 10 万资金 / 10 只 ≈ 1 万/单估算参与率.
        comm = PAPER.get("commission", 0.00025)
        stamp = PAPER.get("stamp_tax", 0.0005)
        transfer = PAPER.get("transfer_fee", 0.00001)
        impact = PAPER.get("impact_cost", 0.0002)
        # 动态滑点
        try:
            from slippage_model import almglen_chriss_slippage, estimate_daily_volume
            advs, vols = [], []
            for s6, df in bar_map.items():
                if df is None or len(df) < 5:
                    continue
                amt = pd.to_numeric(df["amount"], errors="coerce").dropna().values
                if len(amt) >= 5:
                    advs.append(float(np.mean(amt[-20:])))
                if "change_pct" in df.columns:
                    chg = pd.to_numeric(df["change_pct"], errors="coerce").dropna().values / 100.0
                    if len(chg) >= 5:
                        vols.append(float(np.std(chg[-20:]) * np.sqrt(252)))
            avg_adv = np.mean(advs) if advs else 5e7
            avg_vol = np.mean(vols) if vols else 0.02
            order_size = 100_000.0 / max(len(symbol6_list), 1)  # ~1万/单
            dyn_slippage = almglen_chriss_slippage(order_size, avg_adv, avg_vol)
            _log(f"动态滑点: 日均成交额={avg_adv:.0f} 波动率={avg_vol:.4f} "
                 f"订单={order_size:.0f} 滑点率={dyn_slippage:.6f}")
        except Exception as e:
            dyn_slippage = PAPER.get("slippage", 0.0005)
            _log(f"动态滑点计算失败, 回退固定 {dyn_slippage}: {e}")
        BUY_RATE = comm + transfer + dyn_slippage + impact
        SELL_RATE = comm + stamp + transfer + dyn_slippage + impact
        # 将动态滑点率写入 summary 供审计追溯
        dynamic_slippage_rate = dyn_slippage
        for vt in vt_symbols:
            lab.add_contract_setting(vt, long_rate=BUY_RATE, short_rate=SELL_RATE,
                                     size=1, pricetick=0.01)

        # 2) 构造 BarData 列表, 保存到 lab (按 vt_symbol 分别保存, vnpy 4.4 save_bar_data 一次只写一个文件)
        all_bars = _build_bars_list(bar_map, symbol6_list)
        if not all_bars:
            return {"ok": False, "error": "bar 数据为空", "rows": 0}
        # 按 vt_symbol 分桶分别保存
        vt_bucket: dict[str, list] = {}
        for b in all_bars:
            vt_bucket.setdefault(b.vt_symbol, []).append(b)
        for vt, blist in vt_bucket.items():
            lab.save_bar_data(blist)

        # 3) 构造等权 signal_df
        sig_rows = []
        for s6, vt in zip(symbol6_list, vt_symbols):
            if s6 not in bar_map:
                continue
            for d in bar_map[s6]["date"]:
                sig_rows.append({"datetime": pd.Timestamp(d), "symbol": vt, "signal": 1.0})
        signal_df = pl.DataFrame(sig_rows) if sig_rows else pl.DataFrame(
            schema={"datetime": pl.Datetime, "symbol": pl.Utf8, "signal": pl.Float64}
        )

        # 4) set_parameters (回测时间窗口)
        start_dt = bar_map[symbol6_list[0]]["date"].iloc[0].to_pydatetime()
        end_dt = bar_map[symbol6_list[0]]["date"].iloc[-1].to_pydatetime()

        strategy_cls = _build_strategy_class(vt_symbols, weights, day=day)
        eng = BacktestingEngine(lab)
        eng.set_parameters(vt_symbols=vt_symbols, interval=VInterval.DAILY,
                           start=start_dt, end=end_dt, capital=100_000)
        # 5) 手动从 lab 加载 bar 到 engine.history_data + engine.dts
        #    vnpy 4.4 没有自动做这一步, 不加载 on_bars 永远不被调用
        for vt in vt_symbols:
            bars_for_vt = lab.load_bar_data(vt, VInterval.DAILY, start_dt, end_dt)
            for b in bars_for_vt:
                eng.history_data[(b.datetime, vt)] = b
                eng.dts.add(b.datetime)
        eng.add_strategy(strategy_class=strategy_cls, setting={}, signal_df=signal_df)
        eng.run_backtesting()
        result_df = eng.calculate_result()
        stats = eng.calculate_statistics()

        # result_df 是 polars DataFrame
        curve = []
        try:
            if result_df is not None and not result_df.is_empty():
                bal_keys = ("balance", "equity", "total_value",
                            "daily_balance", "account_balance")
                pnl_keys = ("net_pnl", "pnl", "daily_net_pnl")
                _cap = 100_000.0
                cur_bal = None
                for row in result_df.iter_rows(named=True):
                    date_col = next((c for c in ("datetime", "date") if c in row), None)
                    if not date_col:
                        continue
                    bkey = next((c for c in bal_keys if c in row), None)
                    if bkey is not None and row.get(bkey) is not None:
                        try:
                            cur_bal = float(row[bkey])
                        except (TypeError, ValueError):
                            pass
                    else:
                        pkey = next((c for c in pnl_keys if c in row), None)
                        if pkey is not None and row.get(pkey) is not None:
                            try:
                                v = float(row[pkey])
                                cur_bal = (_cap + v) if cur_bal is None else (cur_bal + v)
                            except (TypeError, ValueError):
                                pass
                    curve.append({
                        "date": str(row[date_col])[:10],
                        "balance": round(cur_bal, 2) if cur_bal is not None else None,
                    })
        except Exception:
            pass

        try:
            stats_dict = dict(stats) if stats is not None else {}
        except Exception:
            stats_dict = {"raw": str(stats)}

        out_dir = os.path.join(DATA_DIR, "vnpy_backtest", day_dir)
        os.makedirs(out_dir, exist_ok=True)
        summary = {
            "ok": bool(stats.get("total_return") is not None or stats),
            "day": day,
            "engine": "vnpy.alpha.BacktestingEngine",
            "universe": canon_list,
            "weights": weights,
            "n_bars": len(all_bars),
            "n_symbols": len(bar_map),
            "lookback_days": lookback_days,
            "stats": stats_dict,
            "curve_points": len(curve),
            "dynamic_slippage": round(dynamic_slippage_rate, 6),
            "adj_close_enabled": True,
        }
        _inject_risk_ratios(stats_dict, [p.get("balance") for p in curve])
        if (not stats_dict.get("total_return") and
                not stats_dict.get("end_balance") and
                not stats_dict.get("total_net_pnl")):
            # vnpy 链路未能生成交易统计, 退回到自研简化回测
            _log("vnpy 链路无成交统计, 启用自研简化回测作为 fallback")
            fallback = _fallback_backtest(bar_map, symbol6_list, weights, day_dt)
            summary["stats"] = fallback["stats"]
            summary["curve_points"] = fallback["curve_points"]
            summary["fallback"] = True
        with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(out_dir, "curve.json"), "w", encoding="utf-8") as f:
            json.dump(curve, f, ensure_ascii=False, indent=2, default=str)
        # ===== ArcticDB 持久化: bars 缓存 + trade_records + daily_summary =====
        _persist_to_arctic(
            day=day,
            bar_map=bar_map,
            strategy=strategy_cls,
            summary=summary,
            engine=eng,
        )
        _log(f"vnpy 回测完成: {len(bar_map)} 标的, {len(all_bars)} bars, "
             f"curve={len(curve)}pts, fallback={summary.get('fallback', False)}")
        return summary
    except Exception as e:
        import traceback
        tb = traceback.format_exc(limit=3)
        _log(f"vnpy 回测异常: {type(e).__name__}: {e}\n{tb}")
        # 主链路失败, 直接 fallback
        try:
            fallback = _fallback_backtest(bar_map, symbol6_list, weights, day_dt)
            out_dir = os.path.join(DATA_DIR, "vnpy_backtest", day_dir)
            os.makedirs(out_dir, exist_ok=True)
            summary = {
                "ok": True, "day": day, "engine": "fallback_simple",
                "universe": canon_list, "weights": weights,
                "n_bars": sum(len(df) for df in bar_map.values()),
                "n_symbols": len(bar_map), "lookback_days": lookback_days,
                "stats": fallback["stats"], "curve_points": fallback["curve_points"],
                "fallback": True, "error": str(e)[:200],
                "dynamic_slippage": None,
                "adj_close_enabled": True,
            }
            _inject_risk_ratios(summary["stats"],
                                [p.get("balance") for p in fallback.get("curve", [])])
            with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
            with open(os.path.join(out_dir, "curve.json"), "w", encoding="utf-8") as f:
                json.dump(fallback["curve"], f, ensure_ascii=False, indent=2, default=str)
            _log(f"fallback 回测完成: {fallback['curve_points']} pts")
            return summary
        except Exception as e2:
            return {"ok": False, "error": f"主: {e} | fallback: {e2}", "rows": 0}


def _fallback_backtest(bar_map: dict, symbol6_list: List[str], weights: List[float],
                       day_dt: dt.date) -> dict:
    """自研简化等权回测: 等权买入所有标的, 计算每日组合净值."""
    if not bar_map:
        return {"stats": {}, "curve_points": 0, "curve": []}
    # 取所有标的的并集日期作为组合时间轴
    dates: set = set()
    for df in bar_map.values():
        for d in df["date"]:
            dates.add(d)
    dates = sorted(dates)
    # 计算组合日收益 = 各标的今日回报的等权平均
    curve = []
    initial = 100_000.0
    nav = initial
    for d in dates:
        rets = []
        for s6 in symbol6_list:
            df = bar_map.get(s6)
            if df is None:
                continue
            sub = df[df["date"] <= d]
            if len(sub) < 2:
                continue
            # P1: 用 adj_close (复权价) 替代 raw close, 消除除权跳变
            prev = float(sub.iloc[-2]["adj_close"]) if "adj_close" in sub.columns and pd.notna(sub.iloc[-2].get("adj_close")) else float(sub.iloc[-2]["close"])
            cur = float(sub.iloc[-1]["adj_close"]) if "adj_close" in sub.columns and pd.notna(sub.iloc[-1].get("adj_close")) else float(sub.iloc[-1]["close"])
            if prev > 0:
                rets.append(cur / prev - 1)
        if rets:
            day_ret = sum(rets) / len(rets)
            nav *= (1 + day_ret)
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


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="YYYY-MM-DD; 默认昨日")
    args = ap.parse_args()
    d = args.day or (dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    print(json.dumps(run_vnpy_backtest(d), ensure_ascii=False, indent=2, default=str))