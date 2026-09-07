# ============================================================
# backtest_engine.py -- 连续交易日回放引擎 (引擎稳定性 + A股规则验证)
# 目的: 在"真实历史日K"上连续运行10+个交易日, 验证盘中引擎能连续撮合:
#        正确建仓 / T+1锁定 / 止损触发 / 离池卖出 / 累计净值 / 不崩溃
# 数据: 读取本地 DuckDB daily_bars(2010~最新) 的真实收盘价
# 限制(诚实标注): DuckDB 无逐日估值快照, 无法对历史日重新选股,
#        因此回放期目标池沿用"当前选出的 target"; 该回放定位为
#        "撮合引擎稳定性验证", 非完整历史策略回测。
# 用法:
#   python backtest_engine.py --days 10            # 回放最近10个交易日
#   python backtest_engine.py --start 2026-06-01   # 从指定日期起回放
# 输出: data/backtest/<回放ID>/: 每交易日 daily.json + 汇总 equity_curve.json
# ============================================================

import os
import json
import sys
import math
import argparse
from datetime import datetime, date, timedelta

_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)

from config import (
    STATE_FILE, DAILY_DIR, DATA_DIR, INIT_CAPITAL, MAX_POS_RATIO,
    MAX_STOCKS, PAPER, DUCKDB_PATH,
)
from db import StockDB, _canon_to_db
from paper_book import PaperBook, _limit_prices
from selector import RotationSelector
import duckdb
import pandas as pd

# 与实盘引擎共用同一个目标池解析逻辑, 保证回测池 == 实盘池.
# load_targets 优先级 (与 realtime_engine 完全一致):
#  1) data/drl/<day>/target_plan.json  (DRL plan)
#  2) data/daily/<day>/selection.json  (传统 selector)
#  3) 跨日回退: 用最近一个有 selection.json 的目录
#  4) 现场选股 (仅当上述均缺失)
from realtime_engine import (
    load_targets as _load_targets_live,
    _plan_is_formal,
    _plan_to_targets,
    _prev_trade_day,
    _try_load_daily_plan,
    _is_a_share_code,
)

BACKTEST_DIR = os.path.join(DATA_DIR, "backtest")


def load_current_targets(day: str = None):
    """读取实盘引擎同款目标池作为回放期目标池.

    完全复用 realtime_engine.load_targets(唯一入口), 与实盘盘中引擎消费的
    目标池一致(含 DRL plan 正式性校验 + A 股代码段过滤 + 跨日回退), 消除
    此前"回测裸读 plan / 实盘跨日回退 selection"导致的股票池不一致.
    day 为指定目标池对应日(YYYY-MM-DD); 缺省用 date.today()(与实盘运行日一致).
    返回: list[target], 每项至少含 canon.
    """
    as_of = day or date.today().strftime("%Y-%m-%d")
    try:
        targets, _info, _sel_day = _load_targets_live(as_of)
        if targets:
            for t in targets:
                t.setdefault("source", "realtime_compat")
            return targets
    except Exception:
        pass
    # 兜底: 实盘解析异常时, 保持旧行为尽量返回一个有效池 (DRL plan -> selection)
    import glob
    cand = []
    for d in sorted(glob.glob(os.path.join(DAILY_DIR, "*")), reverse=True):
        day_code = os.path.basename(d)
        if not (day_code.isdigit() and len(day_code) == 8):
            continue
        plan = os.path.join(DATA_DIR, "drl", day_code, "target_plan.json")
        sel = os.path.join(d, "selection.json")
        if os.path.exists(plan) or os.path.exists(sel):
            cand.append(day_code)
    for day_code in cand:
        try:
            plan = os.path.join(DATA_DIR, "drl", day_code, "target_plan.json")
            with open(plan, encoding="utf-8") as f:
                p = json.load(f)
            items = p.get("top_n") or []
            if items:
                out = []
                for it in items:
                    out.append({
                        "canon": it["canon"],
                        "price": it.get("price"),
                        "score": it.get("drl_score", 0.0),
                        "source": "drl_plan",
                    })
                return out
        except Exception:
            pass
        try:
            sel = os.path.join(DAILY_DIR, day_code, "selection.json")
            with open(sel, encoding="utf-8") as f:
                s = json.load(f)
            if s.get("top_n"):
                return s["top_n"]
        except Exception:
            pass
    return []


def trading_days(con, start: date, days: int) -> list:
    """[start, 最新] 范围内交易日列表(升序), 取最近 days 个.
    BAR_STORE=h5i 时从 h5i 版本化库读取(全链路迁移 m3/m4)."""
    if os.environ.get("BAR_STORE", "h5i").lower() == "h5i":
        from datetime import date as _date
        from h5i_bar_store import H5iBarStore
        ds = [d for d in H5iBarStore().trading_days() if d >= start.isoformat()]
        ds = ds[-days:] if len(ds) > days else ds
        return [_date.fromisoformat(x) for x in ds]
    rows = con.execute(
        "SELECT DISTINCT date FROM daily_bars WHERE date >= ? ORDER BY date",
        [start],
    ).fetchall()
    days_l = [r[0] for r in rows]
    # 取"最后 days 个"(最接近最新的历史交易日), 保证回放终点贴近最新
    return days_l[-days:] if len(days_l) > days else days_l


_ADJ_CACHE = {}   # P1: {symbol_db: [(date_str, adj_close)]}, 固定以库内最新bar为锚
_H5I_SER = {}     # 迁移: h5i 复权序列缓存(同口径)


def _adj_series_h5i(sym: str) -> list:
    """h5i 版复权收盘序列: [(date_str, adj_close)] asc, 与 duck _ADJ_CACHE 同构."""
    if sym in _H5I_SER:
        return _H5I_SER[sym]
    from h5i_bar_store import H5iBarStore
    store = H5iBarStore()
    df = store._db.sql(
        f"SELECT CAST(CAST(ts AS DATE) AS VARCHAR) d, close, change_pct "
        f"FROM daily_bars WHERE symbol='{sym}' ORDER BY ts").to_pandas()
    if df.empty:
        _H5I_SER[sym] = []
        return _H5I_SER[sym]
    from factor_library import build_adj_close
    adj = build_adj_close(df.rename(columns={"d": "date"}))
    _H5I_SER[sym] = list(zip(df["d"].tolist(), [float(v) for v in adj]))
    return _H5I_SER[sym]


def _close_prices_for_h5i(day: date, canons: list) -> dict:
    out = {}
    dkey = day.strftime("%Y-%m-%d")
    for canon in canons:
        sym = _canon_to_db(canon)
        series = _adj_series_h5i(sym)
        val = 0.0
        for dt_s, px in series:
            if dt_s <= dkey:
                val = px
            else:
                break
        out[canon] = val
    return out


def close_prices_for(con, day: date, canons: list) -> dict:
    """返回 {canon: 该日复权收盘价}, 消除除权/拆股造成的虚假跳变. 无数据返回0.

    P1: daily_bars.close 为不复权原始价, 分红/送转除权日会从 X 元骤变(如
    601318 2026-08-25 拆股 raw -47.6%); 用它直接平仓/估值会在除权日制造
    虚假亏损、误触止损。

    实现: 用 change_pct(数据源给出的真实日涨跌幅, 已含除权修正)复利重建一条
    相邻比值 = (1+change_pct) 的复权 close。为避免"每次从各自子集最后一日回推"
    导致的跨日量纲错位, 这里固定以库内该 symbol 最新一根 bar 为锚、重建整段
    历史序列后缓存, 每次按 day 在序列内定位取价——所有交易日均处于同一复权刻度, 除权日在复权维度上不再产生跳变, 买卖/持仓成本也同处复权口径。
    """
    if os.environ.get("BAR_STORE", "h5i").lower() == "h5i":
        return _close_prices_for_h5i(day, canons)
    if not canons:
        return {}
    from factor_library import build_adj_close
    syms = [_canon_to_db(c) for c in canons]
    out = {}
    for canon, s in zip(canons, syms):
        series = _ADJ_CACHE.get(s)
        if series is None:
            try:
                df = con.execute(
                    "SELECT date, close, change_pct FROM daily_bars "
                    "WHERE symbol=? ORDER BY date",
                    [s],
                ).fetchdf()
            except Exception:
                df = None
            if df is None or df.empty:
                _ADJ_CACHE[s] = []
                series = _ADJ_CACHE[s]
            else:
                dates = [str(x) for x in df["date"].astype(str)]
                adj = build_adj_close(df)
                _ADJ_CACHE[s] = list(zip(dates, [float(v) for v in adj]))
                series = _ADJ_CACHE[s]
        # 取 <= day 的最后一条(day 当日有数据则该日, 否则之前最近)
        val = 0.0
        dkey = day.strftime("%Y-%m-%d")
        for dt, px in series:
            if dt <= dkey:
                val = px
            else:
                break
        out[canon] = val if math.isfinite(val) and val > 0 else 0.0
    return out


class BacktestRunner:
    def __init__(self, days: int = 10, start: date = None):
        self.days = days
        self.db = StockDB()                          # 统一只读访问(选股+价)
        # [m4] DuckDB 退役后 h5i 为主源; 仅显式 BAR_STORE=duck 且文件仍在时才开 duck 连接
        self.con = None
        if (os.environ.get("BAR_STORE", "h5i").lower() == "duck"
                and os.path.exists(DUCKDB_PATH)):
            self.con = self.db._conn()
        if start is None:
            # 无指定起点: 向前取足够宽, 由 trading_days 切片到最近 days 个交易日
            start = date(2020, 1, 1)
        self.days_list = trading_days(self.con, start, days)
        if not self.days_list:
            raise SystemExit("无可用交易日数据 (h5i/DuckDB 均无 daily_bars)")

    # ---------- 目标池: 与实盘引擎共用 DRL plan (消除回测池/实盘池分叉) ----------
    _CANON_A = staticmethod(lambda c: _is_a_share_code(str(c or "")))

    def _norm_targets(self, top_n: list) -> list:
        """统一成 {canon,name,score} 列表."""
        return [
            {"canon": t["canon"], "name": t.get("name", t["canon"]),
             "score": t.get("score", 0.0)}
            for t in top_n if t.get("canon")
        ]

    def _select_targets_hist(self, hist_day: str) -> list:
        """在历史执行日 hist_day 取"虚拟盘当日同款目标池"(消除与虚拟盘的池子分叉).

        关键时序约束: 实盘引擎在"执行日 D 的盘前(08:30)"消费池, 此时 D 当日收盘
        才会产出的池(selection/DRL plan)尚不存在, 因此实盘跨日回退到「最近一个
        严格早于 D 的交易日收盘」产出的池。回测若直接读 D 当日的 selection/plan
        并用 D 当日收盘价撮合, 等于"当日选出的池当日买"——纯前视, 会系统性高估
        收益(与实盘持仓分叉)。

        因此本函数严格限制候选目录 C < D(等同实盘盘前视角), 顺序与实盘 load_targets
        的跨日回退完全一致:
          1) 跨日回退 DRL: 全部 drl/<C> (C<D) 倒序取通过正式性校验的最近 plan
          2) 跨日回退 selection: 全部 daily/<C> (C<D) 倒序取最近 selection
          3) 现场选股(hist_day 截断, 无前视)

        修复历史:
          v1 用 RotationSelector.select(hist_day) 现场重选 -> 与实盘 DRL plan 分叉
             (回测 08-31 +2.34% 押中银行 / 虚拟盘 08-31 -0.075% 按其 own plan).
          v2 复用 load_targets 但漏掉"同日池属次日" -> 执行日 D 误用 D 当日刚生成
             的 selection(D 收盘产物), 而实盘 D 当日持前一日池 -> 仍分叉.
          v3(本次) 严格执行 C < D, 与实盘盘前消费对齐.
        """
        d = hist_day.replace("-", "")
        try:
            prev = _prev_trade_day(hist_day, db=self.db)
        except Exception:
            prev = None

        # 1) 跨日回退 DRL: 候选目录严格 C < D (盘前视角防前视)
        try:
            cands = [x for x in os.listdir(os.path.join(DATA_DIR, "drl"))
                     if x.isdigit() and len(x) == 8 and x < d]
            cands.sort(reverse=True)
            for cand in cands:
                try:
                    top_n, _info = _try_load_daily_plan(cand, hist_day,
                                                        prev_trade_day=prev)
                    if top_n:
                        return self._norm_targets(top_n)
                except Exception:
                    continue
        except Exception:
            pass

        # 2) 跨日回退 selection: 候选目录严格 C < D (盘前视角防前视)
        try:
            subdirs = [x for x in os.listdir(DAILY_DIR)
                       if os.path.isdir(os.path.join(DAILY_DIR, x))
                       and x.isdigit() and x < d]
            subdirs.sort(reverse=True)
            for cand in subdirs:
                sp = os.path.join(DAILY_DIR, cand, "selection.json")
                if os.path.exists(sp):
                    try:
                        with open(sp, encoding="utf-8") as f:
                            sel = json.load(f)
                        if sel and sel.get("top_n"):
                            top = [t for t in sel["top_n"]
                                   if self._CANON_A(t.get("canon"))]
                            if top:
                                return self._norm_targets(top)
                    except Exception:
                        continue
        except Exception:
            pass

        # 3) 现场选股 (hist_day 截断, 无前视)
        sel = RotationSelector(self.db).select(hist_day=hist_day)
        if not sel or sel.get("error"):
            return []
        return self._norm_targets(sel.get("top_n") or [])

    def run(self, tag: str = None):
        # P4: 目标池不再沿用"当前选股", 而是每个历史交易日 D 用 <=D 的数据
        # 逐日重选(no-lookahead), 并在下一交易日按当日收盘价撮合——彻底消除前视。
        total_mv = INIT_CAPITAL * MAX_POS_RATIO
        # 止损阈值对齐实盘引擎: realtime_engine 用 PAPER.stop_loss(-3%),
        # 此处不再硬编码 8%(历史偏差), 统一读 config, 保证回测/实盘行为一致.
        stop = float(PAPER.get("stop_loss", 0.03) or 0.03)

        tag = tag or datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(BACKTEST_DIR, tag)
        os.makedirs(out_dir, exist_ok=True)

        pb = PaperBook(init_capital=INIT_CAPITAL)   # 10W 初始, 空仓
        curve = []                                   # 净值曲线
        trade_log = []
        last_trade_date = None
        # 降摩擦: 与实盘引擎对齐——换手预算 + 冷却集 + 小单门槛
        _to_used = 0.0
        _to_day = None
        _cooled = set()
        _last_rebal_day = None   # 策略调仓间隔 (2026-09-04, 与实盘引擎对齐)

        # 与实盘引擎时间轴对齐: 每个执行日 D 直接在当日盘前取"实盘同款目标池"
        # (load_targets(D): 优先 DRL plan, 校验窗口=[D前一交易日16:00, D 00:00),
        #  天然只基于 D 之前数据 -> 无前视). 不再"D-1 选池、D 执行", 消除了
        #  DRL plan 盘后补生成导致的时间轴错位(此前 08-31 误用 08-28 的池).
        targets_exec = []
        print(f"[回放] {len(self.days_list)} 个交易日 | 初始资金 {INIT_CAPITAL:,.0f}")
        print(f"[回放] 区间: {self.days_list[0]} ~ {self.days_list[-1]}")
        print(f"[回放] 每个执行日直接用当日 load_targets(D) 目标池(与虚拟盘一致)")

        for d in self.days_list:
            d_str = d.strftime("%Y-%m-%d")
            pb.trade_date = d_str
            pb.day = d_str
            # 当日目标池 == 实盘当日 load_targets; 失败时沿用上一次成功池
            nxt = self._select_targets_hist(d_str)
            if nxt:
                targets_exec = nxt
            else:
                print(f"  [warn] {d_str} 目标池为空, 沿用上次 {len(targets_exec)} 只")
                if not targets_exec:
                    continue   # 无任何可用池, 跳过该日撮合
            # 前一交易日 D-1 决定的目标池在当前日执行
            canon_target = {t["canon"] for t in targets_exec}
            name_map = {t["canon"]: t.get("name", t["canon"]) for t in targets_exec}

            # 当日收盘撮合价(P1复权口径, 消除除权跳变)
            cands = list(canon_target) + list(pb.positions.keys())
            closes = close_prices_for(self.con, d, cands)
            pb.d_price = {c: px for c, px in closes.items() if px > 0}
            # 上一交易日收盘价 -> 当日涨跌停价基准
            prev_day = self.days_list[self.days_list.index(d) - 1]
            prev_closes = close_prices_for(self.con, prev_day, cands)

            # 涨跌停/停牌可交易性门控
            tradable = {}   # canon -> (limit_up, limit_down)
            for c in cands:
                pc = prev_closes.get(c, 0.0)
                lu, ld = _limit_prices(c, pc)
                tradable[c] = (lu, ld)

            def _buyable(c):
                pr = pb.market_price(c)
                lu = tradable.get(c, (None, None))[0]
                if pr <= 0 or not math.isfinite(pr):   # 当日无bar -> 停牌
                    return False
                if lu is not None and pr >= lu:         # 涨停封板, 不追买
                    return False
                return True

            def _sellable(c):
                pr = pb.market_price(c)
                ld = tradable.get(c, (None, None))[1]
                if pr <= 0 or not math.isfinite(pr):   # 当日无bar -> 停牌
                    return False
                if ld is not None and pr <= ld:         # 跌停封板, 卖不出
                    return False
                return True

            trades_day = []

            # 0) 降摩擦: 跨交易日重置换手预算 + 冷却集 (与实盘引擎对齐)
            to_pct = float(PAPER.get("max_turnover_pct", 0.25) or 0.25)
            if _to_day != d_str:
                _to_day = d_str
                _to_used = 0.0
                _cooled = set()
            equity_now = pb.cash + pb.market_value()
            to_budget = equity_now * to_pct

            # 0.1) 策略调仓间隔 gate (2026-09-04 与实盘引擎对齐: 方向2/降频)
            # 距上次策略调仓不足 N 自然日 -> 本轮只做止损/风控, 不做策略换仓.
            rebal_iv = int(PAPER.get("rebalance_interval_days", 0) or 0)
            gate_open = True
            gap_days = 999
            if rebal_iv > 0 and _last_rebal_day:
                try:
                    gap_days = (date.fromisoformat(d_str) -
                                date.fromisoformat(_last_rebal_day)).days
                except Exception:
                    gap_days = 999
                gate_open = gap_days >= rebal_iv

            # 1) 卖出离开目标池的持仓 (可卖且未跌停/停牌; T+1 由 paper_book 原生保护)
            min_hold = int(PAPER.get("min_hold_days", 0) or 0)
            if gate_open:
                for canon in list(pb.positions.keys()):
                    if canon not in canon_target and _sellable(canon):
                        # 最小持仓天数: 持仓不足N天不出售(止损/风控除外)
                        if min_hold > 0:
                            buy_date = pb.positions[canon].get("buy_date")
                            if buy_date:
                                try:
                                    held = (date.fromisoformat(d_str) - date.fromisoformat(buy_date)).days
                                except Exception:
                                    held = 999
                                if held < min_hold:
                                    continue
                        pr = pb.market_price(canon)
                        qty = pb.positions[canon]["qty"]
                        mv = qty * pr
                        if _to_used + mv > to_budget:
                            continue
                        r = pb.sell(canon, qty, pr)
                        if r:
                            _to_used += mv
                            _cooled.add(canon)   # 任何卖出 → 当日不再回补(防振荡)
                            trades_day.append(("S", canon, r["qty"], round(pr, 3), "离池"))

            # 2) 止损 (仅可卖部分, 未跌停/停牌; T+1 原生保护)
            #    遍历全部持仓(不限于目标池内): gate 关闭期间跌出池的持仓
            #    也需要止损保护, 与实盘引擎行为一致.
            for canon in list(pb.positions.keys()):
                p = pb.positions[canon]
                pr = pb.market_price(canon)
                if pr <= 0 or not _sellable(canon):
                    continue
                dd = pr / p["avg_cost"] - 1
                if dd < -stop:
                    r = pb.sell(canon, p["qty"], pr)
                    if r:
                        _cooled.add(canon)   # 止损减仓 -> 当日不再回补
                        trades_day.append(("S", canon, r["qty"], round(pr, 3), f"止损{dd*100:.1f}%"))

            # 3) 补仓目标池到目标权重 (涨停/停牌/冷却/小单门槛/换手预算/调仓间隔 跳过)
            # 策略层优化 (2026-09-05, 与实盘引擎对齐): 消费每项 target_weight.
            band = total_mv / max(len(targets_exec), 1)
            from target_weighting import normalize_weights
            normalize_weights(targets_exec)
            _tw_sum = sum(float(t.get("target_weight", 0.0) or 0.0)
                          for t in targets_exec) or 1.0
            tw_map = {t["canon"]: float(t.get("target_weight", 0.0) or 0.0) /
                      _tw_sum for t in targets_exec}
            min_cash = INIT_CAPITAL * (1 - MAX_POS_RATIO)
            impact = PAPER.get("impact_cost", 0.0002)
            exec_ratio = (1 + PAPER["slippage"] + impact)
            min_abs = PAPER.get("min_reorder_notional", 500.0) or 0.0
            if gate_open:
                for t in targets_exec:
                    canon = t["canon"]
                    if canon in _cooled:
                        continue
                    pr = pb.market_price(canon)
                    if not _buyable(canon):
                        continue
                    # 信号方向门槛 (2026-09-04 与实盘引擎对齐: 方向1/成本覆盖)
                    sig = str(t.get("source_signal") or "").strip().upper()
                    if PAPER.get("min_signal_buy") and sig:
                        neg = ("SELL" in sig) or ("卖出" in sig) or ("减仓" in sig) or ("观望" in sig) or ("中性" in sig)
                        if neg:
                            continue
                    cur_mv = pb.positions[canon]["qty"] * pr if canon in pb.positions else 0
                    tw = tw_map.get(canon)
                    target_mv = total_mv * float(tw) if tw else band
                    diff = target_mv - cur_mv
                    if diff <= pr * 100:
                        continue
                    qty = int(diff // (pr * 100)) * 100
                    if qty < 100:
                        continue
                    cost = qty * pr * exec_ratio
                    mv = cost
                    # 单笔最小名义额: 绝对门槛与相对净资产门槛取较大 (方向3/摊薄成本)
                    min_rel = equity_now * (float(PAPER.get("min_reorder_weight_pct", 0.0) or 0.0) / 100.0)
                    min_notional = max(min_abs, min_rel)
                    if mv < min_notional:   # 小单门槛: 低名义额补权不出手
                        continue
                    if _to_used + mv > to_budget:   # 换手预算门控
                        continue
                    if pb.cash - cost - pb._commission(cost, is_sell=False) >= min_cash:
                        r = pb.buy(canon, qty, pr)
                        if r:
                            _to_used += mv
                            trades_day.append(("B", canon, r["qty"], round(pr, 3), "建仓/补仓"))

            # 推进策略调仓日: 本窗口确有策略成交才锁定间隔 (与实盘引擎一致)
            if gate_open and _to_used > 0:
                _last_rebal_day = d_str

            # 当日汇总
            snap = pb.snapshot()
            daily = {
                "day": d_str,
                "equity": round(snap["equity"], 2),
                "cash": round(snap["cash"], 2),
                "market_value": round(snap["market_value"], 2),
                "realized": round(snap["realized"], 2),
                "open_positions": snap["open_positions"],
                "trades": [
                    {"type": tt[0], "canon": tt[1], "qty": tt[2],
                     "price": tt[3], "reason": tt[4]} for tt in trades_day
                ],
                "positions": {
                    c: {"qty": pp["qty"], "avg_cost": round(pp["avg_cost"], 3),
                        "last_price": round(pp.get("last_price", pb.market_price(c)), 3),
                        "locked_qty": pp.get("locked_qty", 0)}
                    for c, pp in snap["positions"].items()
                },
            }
            with open(os.path.join(out_dir, f"{d_str.replace('-','')}_daily.json"), "w", encoding="utf-8") as f:
                json.dump(daily, f, ensure_ascii=False, indent=2)

            curve.append({"day": d_str, "equity": round(snap["equity"], 2),
                          "pnl_pct": round((snap["equity"] / INIT_CAPITAL - 1) * 100, 2)})
            trade_log.extend(trades_day)
            last_trade_date = d_str
            print(f"  {d_str}: 权益 {snap['equity']:>12,.2f} | 现金 {snap['cash']:>10,.2f} | "
                  f"持仓 {snap['open_positions']:>2} | 成交 {len(trades_day)} | 净值 {curve[-1]['pnl_pct']:+.2f}%")

            # P4: 目标池已在循环顶部按"执行日 D 当日"取好(与实盘 load_targets 一致),
            # 无需在日末重选下一日池(避免 DRL plan 盘后补生成的时间轴错位)。
            del canon_target, name_map, cands, closes, prev_closes, tradable

        # 汇总
        result = {
            "tag": tag,
            "start": self.days_list[0].strftime("%Y-%m-%d"),
            "end": self.days_list[-1].strftime("%Y-%m-%d"),
            "init_capital": INIT_CAPITAL,
            "final_equity": round(pb.snapshot()["equity"], 2),
            "total_return": round((pb.snapshot()["equity"] / INIT_CAPITAL - 1) * 100, 2),
            "trade_days": len(self.days_list),
            "total_trades": len(trade_log),
            "max_drawdown_pct": self._max_dd(curve),
            "method": "执行日D直接用 load_targets(D) 当日池(与虚拟盘完全一致), D当日收盘价撮合",
            "note": "每个执行日D的池 == 实盘当日 load_targets(D): 优先 DRL target_plan"
                    "(校验窗口=[D前一交易日16:00, D 00:00), 只基于D之前数据, 无前视), "
                    "无plan回退当日selection/跨日回退(均<=D)/现场选股; 消除回测池与"
                    "虚拟盘池分叉(此前回测误用现场重选池, 08-31 押中银行股 +2.34% 而"
                    "虚拟盘按其 own plan 全量换仓 -0.075%)。T+1由账本原生锁定, "
                    "涨停不买/跌停不卖/停牌跳过。",
            "curve": curve,
        }
        with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        # 复制一份当前回放结果到固定路径供dashboard读取
        with open(os.path.join(DATA_DIR, "backtest_latest.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        self.db.close()
        print("\n=== 回放完成 ===")
        print(f"区间 {result['start']}~{result['end']}, {result['trade_days']}个交易日, "
              f"总成交 {result['total_trades']}笔")
        print(f"初始资金 {INIT_CAPITAL:,.0f} -> 期末 {result['final_equity']:,.2f} "
              f"({result['total_return']:+.2f}%), 最大回撤 {result['max_drawdown_pct']:.2f}%")
        return result

    @staticmethod
    def _max_dd(curve: list) -> float:
        peak, mdd = -1e18, 0.0
        for p in curve:
            if p["equity"] > peak:
                peak = p["equity"]
            dd = (peak - p["equity"]) / peak * 100
            if dd > mdd:
                mdd = dd
        return round(mdd, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="回放交易日数")
    ap.add_argument("--start", type=str, default=None, help="起始日期 YYYY-MM-DD")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    BacktestRunner(days=args.days, start=start).run()


if __name__ == "__main__":
    main()