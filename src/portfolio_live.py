# -*- coding: utf-8 -*-
"""把多策略组合回测接到**虚拟盘/日更**的接线层 (路线图 ⑪ 接线)。

为什么需要这一层
----------------
`portfolio_backtest.py` 刻意**不自己取数**(价格由 `price_of` 回调给入), 好处是
纯函数可测、可注入受控账本; 代价是它不能单独跑。本模块就是那个"取数适配器":
把

  · 历史目标池(与虚拟盘同源的 5 级回退梯子) -> 每条腿的 目标权重矩阵
  · h5i 日线的**复权**收盘价                    -> `price_of`
  · 前一交易日收盘价 + A 股涨跌停规则            -> `tradable_of`

接成 `portfolio_backtest.run_portfolio_backtest` 能吃的形状。

复权口径为什么必须对齐
----------------------
`backtest_engine.close_prices_for` 用 `change_pct` 复利重建一条**复权**序列,
消除除权/拆股跳变; 若这里改用未复权 `close`, 除权日会凭空多出一根 -47% 的 bar,
直接把移动止损打穿 —— 那是"假信号触发真交易", 比不接还糟。故本模块**只调用**
`close_prices_for`, 不另写取价逻辑(避免第二份实现漂移, 本仓已因此吃过亏)。

与虚拟盘"同源"的含义与限度(如实)
--------------------------------
`targets_of` 默认走 `backtest_engine.BacktestRunner._select_targets_hist`, 它执行
候选目录 `C < D` 的**盘前视角** —— 即复现"当天开盘前能拿到什么池", 因此可用于
历史回放。而实盘 `realtime_engine.load_targets(D)` 是"消费时刻的磁盘状态",
**事后调用会停在与当时不同的一档**(见该函数 docstring 的说明)。两者**不可互换**,
本模块也不假装它们等价: 传入 `live_pool=True` 时改用 `load_targets`(复现"现在读
会拿到什么"), 用于对照而非回放。
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Callable, Iterable, Sequence

import numpy as np


def _canon_of(raw) -> str:
    """6 位代码 -> canon(与 paper_book._canon_of 同语义)。已带后缀则原样返回。

    **空值返回空串**(而不是补成 `000000.SZ`): 目标池里一行缺 `canon` 键是
    "这项无效", 不是"一项代码为 000000 的标的"。把它补成一个看似合法的 canon
    会让该行进入权重矩阵, 在组合里凭空多出一个不存在的持仓。
    """
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    if "." in s:
        return s
    s6 = s.zfill(6)
    if s6.startswith(("60", "68", "90")):
        return f"{s6}.SH"
    if s6.startswith(("0", "3")):
        return f"{s6}.SZ"
    if s6.startswith(("4", "8")):
        return f"{s6}.BSE"
    return f"{s6}.SZ"


def _d(x) -> dt.date:
    """把 'YYYY-MM-DD' / 'YYYYMMDD' / 'YYYY/MM/DD' / date 统一成 date。"""
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    s = str(x).strip().replace("-", "").replace("/", "")
    return dt.datetime.strptime(s[:8], "%Y%m%d").date()


# --------------------------------------------------------------------------
# 1) 历史目标池 -> 权重矩阵
# --------------------------------------------------------------------------
def weights_from_targets(dates: Sequence[str], targets_of: Callable[[str], list],
                         symbols: Sequence[str] | None = None, *,
                         weight_key: str = "target_weight",
                         normalize: bool = True) -> tuple[list[str], np.ndarray]:
    """逐日取目标池 -> (symbols, weights) 矩阵。

    `targets_of(day)` 返回 `[{canon, target_weight?, ...}]`; 缺 `target_weight`
    时按**等权**填(与 `paper_book.rebalance` 的 band 语义一致)。
    只出现在部分日期的标的会被保留(列全 0 即"那天不持有") —— 对齐交由
    `portfolio_backtest.align_weights` 做并集, 这里不提前裁剪。
    """
    rows: list[dict] = []
    all_syms: list[str] = []
    for day in dates:
        try:
            ts = list(targets_of(day) or [])
        except Exception:  # noqa: BLE001
            ts = []
        d: dict[str, float] = {}
        for t in ts:
            c = _canon_of(t.get("canon") or t.get("symbol"))
            if not c:
                continue
            try:
                w = float(t.get(weight_key)) if t.get(weight_key) is not None else None
            except (TypeError, ValueError):
                w = None
            d[c] = w
            if c not in all_syms:
                all_syms.append(c)
        rows.append(d)
    syms = list(symbols) if symbols is not None else sorted(all_syms)
    if not syms:
        return [], np.zeros((len(dates), 0), dtype=np.float64)
    W = np.zeros((len(dates), len(syms)), dtype=np.float64)
    pos = {s: j for j, s in enumerate(syms)}
    for i, d in enumerate(rows):
        have = {c: w for c, w in d.items() if c in pos}
        if not have:
            continue
        known = [w for w in have.values() if w is not None and w > 0]
        if len(known) == len(have) and known:
            vals = have
        else:
            # 有缺 target_weight 的项 -> 整日退化为等权(不混用两种口径:
            # 半带权半等权会让"总暴露"这个数失去意义)
            eq = 1.0 / len(have)
            vals = {c: eq for c in have}
        tot = sum(float(v or 0.0) for v in vals.values())
        for c, w in vals.items():
            x = float(w or 0.0)
            W[i, pos[c]] = (x / tot) if (normalize and tot > 0) else x
    return syms, W


# --------------------------------------------------------------------------
# 2) 价格 / 可交易性回调
# --------------------------------------------------------------------------
def make_price_of(con=None, *, cache: bool = True) -> Callable[[str, str], float]:
    """构造 `price_of(day, canon) -> 复权收盘价`(<=0 表示当日无价/停牌)。

    **只调用** `backtest_engine.close_prices_for` —— 复权口径与虚拟盘/回放一致。
    `con` 在 `BAR_STORE=h5i`(现行默认)下不被使用, 仅为 DuckDB 回退路径保留。
    """
    from backtest_engine import close_prices_for
    memo: dict = {}

    def price_of(day: str, canon: str) -> float:
        d = _d(day)
        key = (d, canon)
        if cache and key in memo:
            return memo[key]
        try:
            px = float((close_prices_for(con, d, [canon]) or {}).get(canon, 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            px = 0.0
        if cache:
            memo[key] = px
        return px

    return price_of


def make_tradable_of(con=None) -> Callable[[str, str], bool]:
    """构造 `tradable_of(day, canon) -> bool`: 涨停不可买 / 跌停不可卖。

    涨跌停价由**前一交易日**收盘价推(`paper_book._limit_prices`, 与撮合层同源),
    故需要前一日收盘 —— 这也是为什么它必须在数据层而不是 `portfolio_backtest`
    里算(那里拿不到"前一日")。

    实现: 对当日的价格一律判"最低价 == 最高价(一字板)"才视为不可成交; 否则按
    涨跌停价比较。无前收盘时返回 True(不判定) —— 与 `_limit_prices` 返回
    (None, None) 的语义一致: **不因缺数据拒单**。
    """
    from backtest_engine import close_prices_for
    from paper_book import _limit_prices

    def tradable_of(day: str, canon: str) -> bool:
        d = _d(day)
        try:
            today = float((close_prices_for(con, d, [canon]) or {}).get(canon, 0.0) or 0.0)
            prev_day = d - dt.timedelta(days=1)
            prev = float((close_prices_for(con, prev_day, [canon]) or {}).get(canon, 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            return True
        if today <= 0 or prev <= 0:
            return True                    # 无数据 -> 不判定(不据此拒单)
        lu, ld = _limit_prices(canon, prev)
        if lu is not None and today >= lu:
            return False                   # 涨停: 买不到
        if ld is not None and today <= ld:
            return False                   # 跌停: 卖不出
        return True

    return tradable_of


# --------------------------------------------------------------------------
# 3) 目标池来源
# --------------------------------------------------------------------------
def make_targets_of(store=None, *, live_pool: bool = False,
                    n: int | None = None) -> Callable[[str], list]:
    """构造 `targets_of(day) -> [目标项]`。

    live_pool=False(默认, 用于**历史回放**): 走
        `BacktestRunner._select_targets_hist` —— 候选目录 `C < D` 的盘前视角,
        与 `backtest_engine` 回放同源, 无前视。
    live_pool=True(用于**对照**): 走 `realtime_engine.load_targets` —— 复现
        "此刻读磁盘会拿到哪一档"。**不用于回放**: 该函数依赖调用时刻的磁盘状态,
        事后调用会停在与当时不同的一档(其 docstring 已注明)。
    """
    if live_pool:
        from realtime_engine import load_targets

        def targets_of_live(day: str) -> list:
            top_n, _info, _sel_day = load_targets(day)
            return list(top_n or [])

        return targets_of_live
    from backtest_engine import BacktestRunner
    runner = BacktestRunner.__new__(BacktestRunner)   # 不跑 __init__(不建连接)
    try:
        runner.con = store
    except Exception:  # noqa: BLE001
        pass

    def targets_of_hist(day: str) -> list:
        return list(runner._select_targets_hist(day) or [])

    return targets_of_hist


# --------------------------------------------------------------------------
# 4) 一键入口
# --------------------------------------------------------------------------
def leg_from_targets(name: str, dates: Sequence[str],
                     targets_of: Callable[[str], list],
                     symbols: Sequence[str] | None = None,
                     weight_key: str = "target_weight"):
    """把一路目标池来源变成一条 `StrategyWeights` 腿。"""
    from portfolio_backtest import StrategyWeights
    syms, W = weights_from_targets(dates, targets_of, symbols, weight_key=weight_key)
    return StrategyWeights(name, list(dates), syms, W,
                           note=f"from targets_of={getattr(targets_of, '__name__', '?')}")


def run_live_portfolio_backtest(days: Sequence[str],
                                legs_spec: Iterable[tuple] | None = None, *,
                                strategy_weights: Sequence[float] | None = None,
                                invest_ratio: float | None = None,
                                init_capital: float | None = None,
                                con=None, live_pool: bool = False,
                                save_to: str | None = None) -> dict:
    """在**虚拟盘同源**的目标池上跑多策略组合回测。

    legs_spec : [(腿名, targets_of), ...]; None 时退化为**单腿**(虚拟盘当前池),
                即"组合=只有一条腿", 仍走同一撮合流程 —— 这样它与
                `vnpy_backtest` 的结果是同一口径下的两个数, 而不是两套假设。
    save_to   : 非空则把结果(去掉 trade_log)写 JSON, 供日更/面板读取。

    返回 `portfolio_backtest.run_portfolio_backtest` 的结果 + `source` 元信息。
    """
    from portfolio_backtest import compare_to_single, run_portfolio_backtest

    dates = list(days or [])
    if not dates:
        return {"ok": False, "error": "days 为空"}
    if legs_spec is None:
        legs_spec = [("live_pool", make_targets_of(live_pool=live_pool))]
    legs = [leg_from_targets(nm, dates, tf) for nm, tf in legs_spec]
    legs = [lg for lg in legs if lg.weights.size and lg.symbols]
    if not legs:
        return {"ok": False, "error": "所有腿的目标池都为空(h5i 缺数据或日期越界)",
                "days": dates}

    common = dict(price_of=make_price_of(con),
                  tradable_of=make_tradable_of(con),
                  invest_ratio=invest_ratio, init_capital=init_capital)
    sw = list(strategy_weights) if strategy_weights is not None else [1.0 / len(legs)] * len(legs)
    if len(sw) != len(legs):
        return {"ok": False, "error": f"策略权重数 {len(sw)} 与有效腿数 {len(legs)} 不符"}

    res = run_portfolio_backtest(legs, sw, **common)
    out = {"ok": True, "source": "live_pool" if live_pool else "hist_pit",
           "days": dates, "n_legs": len(legs),
           "leg_names": [lg.name for lg in legs],
           **res}
    if len(legs) > 1:
        try:
            out["versus_single"] = compare_to_single(legs, sw, **common)
        except Exception as e:  # noqa: BLE001
            out["versus_single"] = {"error": f"{type(e).__name__}: {e}"}
    if save_to:
        try:
            import json
            payload = {k: v for k, v in out.items() if k != "trade_log"}
            os.makedirs(os.path.dirname(save_to), exist_ok=True)
            with open(save_to, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            out["saved_to"] = save_to
        except Exception as e:  # noqa: BLE001
            out["save_error"] = f"{type(e).__name__}: {e}"
    return out


def recent_trading_days(n: int = 20, store=None) -> list[str]:
    """最近 n 个交易日(升序)。用于日更里给组合回测取窗口。"""
    try:
        if store is None:
            from h5i_bar_store import H5iBarStore
            store = H5iBarStore()
        days = list(store.trading_days())
    except Exception:  # noqa: BLE001
        return []
    return days[-n:] if len(days) > n else days
