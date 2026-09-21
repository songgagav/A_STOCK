# -*- coding: utf-8 -*-
"""多策略投资组合回测 (路线图 #11/⑪): 多策略 target qty 按权重聚合, 走同一撮合流程.

问题
----
本仓至今是**单策略**回测: `vnpy_backtest.run_vnpy_backtest()` 读死一份
`selection.json` / PIT 选股得到**一个**目标池, `backtest_engine.BacktestRunner`
同样只有一个目标池。于是"把 fusion + gp4 + 新信号合起来会怎样"这个问题
**没有可执行的回答** —— 只能在代码里换掉那一份池, 得到的是"另一个单策略结果",
不是"组合结果"。

本模块的定位
------------
把 N 个策略各自的**目标权重矩阵**聚合成一个组合权重, 再喂给**同一个**撮合流程
(`paper_book.PaperBook`)走一遍, 从而:

  · 组合结果与单策略结果**成本口径完全一致**(同一份 PAPER 费率/T+1/整手/涨跌停),
    因此"加了这个策略到底改善了多少"是可比的两个数, 而不是两套假设下的数;
  · 逐策略贡献可归因(每条腿的权重 × 权重矩阵 → 名义暴露),
    使"某条腿其实是噪声"这类结论有据可依。

为什么用权重聚合而不是"信号投票"
--------------------------------
投票法丢弃强度信息: 两个策略都说买、但一个 0.9 一个 0.1, 投票与两者都 0.5
不可区分。而权重聚合是可微的、可算风险预算的, 并且能**直接落到 target qty**
(与 `target_weighting.py` 既有的 "target_mv = total * w" 口径同构)。
投票法本模块不做 —— 需要的话是另一个函数, 不在本文件假装支持。

单位与口径(与 config 对齐, 不另造)
----------------------------------
· 权重相对**总资产**, 每列 `sum(w) <= invest_ratio`(= `MAX_POS_RATIO`), 与
  `target_weighting.allocate_target_weights` 的契约一致;
· 撮合价由调用方给的 `price_of(day, canon)` 决定 —— 本模块**不自己取数**,
  因此可在测试里用合成价格完全确定性地验证(不需要数据库)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import numpy as np


# --------------------------------------------------------------------------
# 1) 权重矩阵装配与聚合
# --------------------------------------------------------------------------
@dataclass
class StrategyWeights:
    """一条策略腿的交割: dates × symbols 的目标权重(0..1, 相对总资产)。

    weights[i, j] = 第 i 天、第 j 个标的的目标权重; 缺失用 0.0(不是 NaN) ——
    "不持有"与"未知"在组合层是同一件事: 都给 0 权重。
    """
    name: str
    dates: list[str]
    symbols: list[str]
    weights: np.ndarray
    note: str = ""

    def __post_init__(self):
        w = np.asarray(self.weights, dtype=np.float64)
        if w.ndim != 2:
            raise ValueError(f"[{self.name}] weights 必须是二维 (days, symbols), "
                             f"实际 ndim={w.ndim}")
        if w.shape != (len(self.dates), len(self.symbols)):
            raise ValueError(f"[{self.name}] weights 形状 {w.shape} 与 "
                             f"(days={len(self.dates)}, symbols={len(self.symbols)}) 不符")
        self.weights = w

    def row(self, day: str) -> np.ndarray:
        try:
            return self.weights[self.dates.index(day)]
        except ValueError:
            return np.zeros(len(self.symbols), dtype=np.float64)


def align_weights(legs: Sequence[StrategyWeights],
                  dates: Sequence[str] | None = None,
                  symbols: Sequence[str] | None = None) -> tuple[list[str], list[str], np.ndarray]:
    """把多条腿对齐到**共同的** 日期轴与标的轴。

    默认取各腿日期/标的的**并集并排序**(不是交集): 组合里某条腿只在部分日期
    有意见是常态, 取交集会把组合可回测区间压到最短的那条腿上, 而这恰恰掩盖了
    "某条腿经常不出手"这一事实(该事实应在 `coverage` 里如实体现, 而不是被裁掉)。

    返回 (dates, symbols, cube) —— cube.shape = (n_legs, n_days, n_symbols)。
    """
    if not legs:
        raise ValueError("legs 不能为空")
    d_all = list(dates) if dates is not None else sorted({d for lg in legs for d in lg.dates})
    s_all = list(symbols) if symbols is not None else sorted({s for lg in legs for s in lg.symbols})
    if not d_all or not s_all:
        raise ValueError("日期轴或标的轴为空")
    cube = np.zeros((len(legs), len(d_all), len(s_all)), dtype=np.float64)
    for li, lg in enumerate(legs):
        d_pos = {d: i for i, d in enumerate(lg.dates)}
        s_pos = {s: j for j, s in enumerate(lg.symbols)}
        for di, d in enumerate(d_all):
            if d not in d_pos:
                continue
            r = lg.weights[d_pos[d]]
            for sj, s in enumerate(s_all):
                if s in s_pos:
                    cube[li, di, sj] = r[s_pos[s]]
    return d_all, s_all, cube


def normalize_weights(legs: Sequence[StrategyWeights]) -> list[StrategyWeights]:
    """把每条腿的**每天**权重归一化到 sum=1(全 0 的日子保持全 0)。

    为什么要归一: 不同策略给出的权重标度可能不同(一个是 0..1 的分数占比,
    另一个是 0..10 的原始打分)。不归一直接加权平均, 等于让"标度大的那条腿"
    独占组合 —— 那不是多策略, 是单策略加了个噪声项。
    全 0 的日子**保持全 0**(而不是补等权): "这条腿今天没意见"必须与"这条腿今天
    平均看好所有票"区分开。
    """
    out: list[StrategyWeights] = []
    for lg in legs:
        w = lg.weights.copy()
        rs = w.sum(axis=1, keepdims=True)
        nz = rs[:, 0] > 0
        w[nz] = w[nz] / rs[nz]
        out.append(StrategyWeights(lg.name, list(lg.dates), list(lg.symbols), w, lg.note))
    return out


def aggregate(cube: np.ndarray, strategy_weights: Sequence[float] | np.ndarray,
              *, normalize: bool = True) -> np.ndarray:
    """按权重把多腿聚合成一条组合权重曲线。

    cube shape = (n_legs, n_days, n_symbols); strategy_weights 长度 n_legs。
    权重必须**非负**且和 > 0; 负权重(做空腿)本模块不支持 —— 隐式接受负权重会让
    `sum(w)` 的"总暴露"语义崩掉, 故显式抛错而不是让它悄悄算出一个看不懂的数。
    返回 shape = (n_days, n_symbols)。
    """
    c = np.asarray(cube, dtype=np.float64)
    sw = np.asarray(strategy_weights, dtype=np.float64).ravel()
    if c.ndim != 3:
        raise ValueError(f"cube 必须是三维, 实际 ndim={c.ndim}")
    if sw.size != c.shape[0]:
        raise ValueError(f"策略权重数 {sw.size} 与腿数 {c.shape[0]} 不符")
    if not np.all(np.isfinite(sw)):
        raise ValueError("策略权重含 NaN/inf")
    if np.any(sw < 0):
        raise ValueError("策略权重不得为负(本模块不支持做空腿)")
    tot = float(sw.sum())
    if tot <= 0:
        raise ValueError("策略权重之和必须 > 0")
    sw = sw / tot
    comb = np.tensordot(sw, c, axes=(0, 0))
    if normalize:
        rs = comb.sum(axis=1, keepdims=True)
        nz = rs[:, 0] > 0
        comb[nz] = comb[nz] / rs[nz]
    return comb


def leg_exposure(cube: np.ndarray, strategy_weights: Sequence[float]) -> list[dict]:
    """逐腿贡献(名义暴露占比): 权重 × 该腿平均总暴露。供"这条腿到底贡献了什么"。"""
    c = np.asarray(cube, dtype=np.float64)
    sw = np.asarray(strategy_weights, dtype=np.float64).ravel()
    tot = float(sw.sum()) or 1.0
    out = []
    for i, lg_i in enumerate(range(c.shape[0])):
        out.append({
            "leg": lg_i,
            "weight": float(sw[lg_i] / tot),
            "mean_gross": float(c[lg_i].sum(axis=1).mean()),
            "mean_names": float((c[lg_i] > 0).sum(axis=1).mean()),
            "contrib": float(sw[lg_i] / tot * c[lg_i].sum(axis=1).mean()),
        })
    return out


# --------------------------------------------------------------------------
# 2) 同一撮合流程回测
# --------------------------------------------------------------------------
def simulate_portfolio(dates: Sequence[str], symbols: Sequence[str], weights: np.ndarray,
                       *, price_of: Callable[[str, str], float],
                       tradable_of: Callable[[str, str], bool] | None = None,
                       book_factory: Callable[[], object] | None = None,
                       invest_ratio: float | None = None,
                       init_capital: float | None = None,
                       rebalance_band: float = 0.0) -> dict:
    """用 `paper_book.PaperBook` 走一遍日频撮合, 得到组合净值曲线。

    Parameters
    ----------
    weights : (n_days, n_symbols) 目标权重(相对总资产)。每日按它调仓。
    price_of : callable(day, canon) -> 收盘价; 返回 <=0 视为当天无价(停牌)跳过。
    tradable_of : callable(day, canon) -> bool|None; None 表示"不判定, 视为可交易"。
        涨停不可买 / 跌停不可卖由调用方在此提供, 因为那需要**前一交易日**收盘价,
        属于数据层职责, 本模块不假装知道。
    book_factory : 建账本的可调用对象(默认 `paper_book.PaperBook`);
        参数化是为了测试里能注入一个受控账本, 同时生产路径用的仍是同一个类。
    rebalance_band : 权重变化小于该值就不动手(降摩擦)。**默认 0 = 不动既有行为**;
        生产接线处显式传 config 的值。

    返回 {'curve','final_equity','total_return_pct','max_drawdown_pct',
          'trades','turnover_pct','orders_skipped','leg_exposure'} 等。
    撮合顺序: **先卖后买** —— 卖出释放的现金才能被买入用上, 与
    `paper_book.rebalance` 的顺序一致(反过来会在满仓时静默少买)。
    """
    from config import INIT_CAPITAL, MAX_POS_RATIO, PAPER

    cap = float(init_capital if init_capital is not None else INIT_CAPITAL)
    ir = float(invest_ratio if invest_ratio is not None else MAX_POS_RATIO)

    if book_factory is None:
        from paper_book import PaperBook
        book_factory = lambda: PaperBook(init_capital=cap)  # noqa: E731
    pb = book_factory()

    w = np.asarray(weights, dtype=np.float64)
    if w.shape != (len(dates), len(symbols)):
        raise ValueError(f"weights 形状 {w.shape} 与 (days={len(dates)}, "
                         f"symbols={len(symbols)}) 不符")

    curve: list[dict] = []
    trade_log: list[dict] = []
    skipped = 0
    turnover_notional = 0.0

    for di, day in enumerate(dates):
        # 账本日推进(paper_book 用 trade_date 做 T+1 锁定判定)
        try:
            pb.trade_date = day
            pb.day = day
        except Exception:  # noqa: BLE001
            pass
        row = w[di]
        # 当日价格快照: 只取权重非 0 或已持仓的标的 (省调用)
        need = {symbols[j] for j in np.nonzero(row)[0]}
        need |= set(getattr(pb, "positions", {}).keys())
        px: dict[str, float] = {}
        for s in need:
            try:
                p = float(price_of(day, s) or 0.0)
            except Exception:  # noqa: BLE001
                p = 0.0
            if p > 0 and math.isfinite(p):
                px[s] = p
        pb.d_price = dict(px)
        eq = float(pb.cash) + float(pb.market_value())
        target_mv = {symbols[j]: eq * ir * float(row[j]) for j in range(len(symbols))
                     if row[j] > 0}

        def _ok(canon: str) -> bool:
            if tradable_of is None:
                return True
            try:
                t = tradable_of(day, canon)
            except Exception:  # noqa: BLE001
                return True          # 判定异常不阻断(否则一个 bug 就让回测静默停手)
            return t is not False

        # --- 1) 先卖: 权重降到目标以下 / 离场的 ---
        for canon in list(getattr(pb, "positions", {}).keys()):
            pr = px.get(canon, 0.0)
            if pr <= 0 or not _ok(canon):
                skipped += 1
                continue
            cur_qty = pb.positions[canon]["qty"]
            tgt = target_mv.get(canon, 0.0)
            cur_mv = cur_qty * pr
            if cur_mv - tgt <= pr * 100 * (1.0 + rebalance_band):
                continue
            want_mv = max(cur_mv - tgt, 0.0)
            qty = int(want_mv // pr // 100) * 100
            if canon not in target_mv:
                qty = cur_qty          # 完全离场: 卖光(整手约束由账本处理)
            if qty <= 0:
                continue
            r = pb.sell(canon, qty, pr)
            if r:
                turnover_notional += float(r["qty"]) * pr
                trade_log.append({"day": day, "canon": canon, "side": "sell",
                                  "qty": r["qty"], "price": pr})

        # --- 2) 后买: 未达目标的补足 ---
        for canon, tgt in target_mv.items():
            pr = px.get(canon, 0.0)
            if pr <= 0 or not _ok(canon):
                skipped += 1
                continue
            cur_mv = (pb.positions[canon]["qty"] * pr) if canon in pb.positions else 0.0
            diff = tgt - cur_mv
            if diff <= pr * 100 * (1.0 + rebalance_band):
                continue
            qty = int(diff // (pr * 100)) * 100
            if qty < 100:
                continue
            r = pb.buy(canon, qty, pr)
            if r:
                turnover_notional += float(r["qty"]) * pr
                trade_log.append({"day": day, "canon": canon, "side": "buy",
                                  "qty": r["qty"], "price": pr})

        snap = pb.snapshot()
        curve.append({"day": day, "equity": round(float(snap["equity"]), 2),
                      "pnl_pct": round((float(snap["equity"]) / cap - 1.0) * 100.0, 4),
                      "positions": int(snap.get("open_positions") or 0)})

    final_eq = float(pb.snapshot()["equity"])
    return {
        "curve": curve,
        "final_equity": round(final_eq, 2),
        "total_return_pct": round((final_eq / cap - 1.0) * 100.0, 4),
        "max_drawdown_pct": max_drawdown_pct([c["equity"] for c in curve]),
        "trades": len(trade_log),
        "trade_log": trade_log,
        "turnover_notional": round(turnover_notional, 2),
        "turnover_pct": round(turnover_notional / cap * 100.0, 4),
        "orders_skipped": skipped,
        "invest_ratio": ir,
        "init_capital": cap,
    }


# --------------------------------------------------------------------------
# 3) 指标
# --------------------------------------------------------------------------
def max_drawdown_pct(equity: Iterable[float]) -> float:
    """最大回撤(百分点, 正数)。空/单点序列返回 0.0。"""
    peak, mdd = -float("inf"), 0.0
    for e in equity:
        e = float(e)
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > mdd:
                mdd = dd
    return round(mdd, 4)


def daily_returns(equity: Sequence[float]) -> np.ndarray:
    """净值序列 -> 日收益序列(长度 n-1)。非正净值处返回 0(不产生 inf)。"""
    e = np.asarray(list(equity), dtype=np.float64)
    if e.size < 2:
        return np.zeros(0, dtype=np.float64)
    prev = e[:-1]
    prev = np.where(prev > 0, prev, 1.0)
    return e[1:] / prev - 1.0


def sharpe(r: np.ndarray, trading_days: int = 252) -> float:
    """年化 Sharpe(无风险利率 0)。样本 < 3 或零波动 -> NaN(不是 0)。"""
    a = np.asarray(r, dtype=np.float64).ravel()
    a = a[np.isfinite(a)]
    if a.size < 3:
        return float("nan")
    sd = float(np.std(a, ddof=1))
    if not math.isfinite(sd) or sd <= 1e-12:
        return float("nan")
    return float(np.mean(a) / sd * math.sqrt(trading_days))


def metrics(curve: Sequence[dict]) -> dict:
    """净值曲线 -> 标准指标集。"""
    eqs = [float(c.get("equity", 0.0)) for c in curve or []]
    r = daily_returns(eqs)
    out = {
        "n_days": len(eqs),
        "total_return_pct": round((eqs[-1] / eqs[0] - 1.0) * 100.0, 4) if len(eqs) > 1 and eqs[0] > 0 else 0.0,
        "max_drawdown_pct": max_drawdown_pct(eqs),
        "sharpe": None,
        "vol_annual_pct": None,
    }
    s = sharpe(r)
    if math.isfinite(s):
        out["sharpe"] = round(s, 4)
    if r.size >= 3:
        out["vol_annual_pct"] = round(float(np.std(r, ddof=1) * math.sqrt(252) * 100.0), 4)
    return out


# --------------------------------------------------------------------------
# 4) 一键入口
# --------------------------------------------------------------------------
def run_portfolio_backtest(legs: Sequence[StrategyWeights],
                           strategy_weights: Sequence[float],
                           *, price_of: Callable[[str, str], float],
                           tradable_of: Callable[[str, str], bool] | None = None,
                           normalize_legs: bool = True,
                           normalize_combined: bool = True,
                           book_factory: Callable[[], object] | None = None,
                           invest_ratio: float | None = None,
                           init_capital: float | None = None,
                           rebalance_band: float = 0.0,
                           dates: Sequence[str] | None = None,
                           symbols: Sequence[str] | None = None) -> dict:
    """多策略组合回测主入口: 对齐 -> 归一 -> 聚合 -> 同一撮合流程 -> 指标。

    返回 dict, 其中 `legs` 段给出逐腿贡献(见 `leg_exposure`),
    `combined_weights` 给出聚合后的权重矩阵 —— 落盘后可供审计"组合到底持了什么"。
    """
    lg = normalize_weights(legs) if normalize_legs else list(legs)
    d_all, s_all, cube = align_weights(lg, dates=dates, symbols=symbols)
    comb = aggregate(cube, strategy_weights, normalize=normalize_combined)
    sim = simulate_portfolio(d_all, s_all, comb, price_of=price_of,
                             tradable_of=tradable_of, book_factory=book_factory,
                             invest_ratio=invest_ratio, init_capital=init_capital,
                             rebalance_band=rebalance_band)
    sim["metrics"] = metrics(sim["curve"])
    sim["legs"] = leg_exposure(cube, strategy_weights)
    sim["dates"] = list(d_all)
    sim["symbols"] = list(s_all)
    sim["combined_weights"] = comb.tolist()
    sim["strategy_weights"] = [float(x) for x in np.asarray(strategy_weights).ravel()]
    return sim


# --------------------------------------------------------------------------
# 5) 与单策略对照
# --------------------------------------------------------------------------
def compare_to_single(legs: Sequence[StrategyWeights],
                      strategy_weights: Sequence[float] | None = None,
                      **kwargs) -> dict:
    """组合 vs 每一条单腿: 同一撮合流程下逐条跑一遍, 给出可比结果。

    这正是本模块存在的理由 —— "加了这个策略改善了多少"只有在**同一撮合口径**下
    才是可比的数。strategy_weights 为 None 时只跑单腿对照。
    """
    single: dict[str, dict] = {}
    for i, one in enumerate(legs):
        r = run_portfolio_backtest([one], [1.0], **kwargs)
        single[one.name] = {"metrics": r["metrics"],
                            "total_return_pct": r["total_return_pct"],
                            "max_drawdown_pct": r["max_drawdown_pct"],
                            "turnover_pct": r["turnover_pct"],
                            "trades": r["trades"]}
    out = {"single_legs": single}
    if strategy_weights is not None:
        comb = run_portfolio_backtest(legs, strategy_weights, **kwargs)
        out["portfolio"] = {"metrics": comb["metrics"],
                            "total_return_pct": comb["total_return_pct"],
                            "max_drawdown_pct": comb["max_drawdown_pct"],
                            "turnover_pct": comb["turnover_pct"],
                            "trades": comb["trades"],
                            "legs": comb["legs"]}
        # 与"最好的那条单腿"比: 组合若跑不过最好的单腿, 必须如实显示为负
        best = max(single.items(), key=lambda kv: kv[1]["total_return_pct"]) if single else None
        if best:
            out["versus_best_single"] = {
                "best_leg": best[0],
                "best_return_pct": best[1]["total_return_pct"],
                "excess_pct": round(out["portfolio"]["total_return_pct"]
                                    - best[1]["total_return_pct"], 4),
            }
    return out
