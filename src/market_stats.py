# -*- coding: utf-8 -*-
"""市场微观结构统计 (ADV / 波动率) —— 供撮合层的**动态成本**与拆单判定使用。

为什么需要这一层
----------------
`paper_book.buy/sell(avg_daily_volume=..., volatility=...)` 的动态滑点分支
**在生产里从未被走过**: 全部调用点都没传这两个参数, 于是虚拟盘实际吃的是
常量成本 `PAPER.slippage + PAPER.impact_cost` = 7bps(万5+万2), 逐笔
`impact_bps` / `exec_risk_bps` **从来不产生**。后果有两个:

  1. 无法做"实测滑点分布"(它们根本没被算出来);
  2. 无法判断一笔单到底相对 ADV 有多大(参与率), 而拆单判定的唯一输入就是它。

本模块把"取 ADV 与波动率"这件事收敛到一处, 口径与数据源都写死:

  · ADV   —— `h5i daily_bars.amount` 的近 N 日均值, 单位**元**(成交额)。
    这正是 `slippage_model.decompose_slippage(order_size, avg_daily_volume, ...)`
    要求的单位(该函数 docstring: "日均成交额(元)")。
  · 波动率 —— `change_pct` 的样本标准差 × √252, 年化。取不到时回退到
    `PAPER.slippage_vol_fallback`(见下), **不留 NaN 让下游自己猜**。

口径纪律(本仓教训)
------------------
`backtest_engine` 已因"两份实现漂移"吃过亏, 故本模块**只做取数与统计**,
不复制任何成本公式; 成本一律由 `slippage_model` 算。
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

#: 波动率取不到时的回退值(年化)。取 `paper_book` 在 volatility 缺省时用的
#: 日频 0.025 换算到年化: 0.025 × √252 ≈ 0.397。**不是新数字**, 是把既有
#: 缺省值换算到本模块的年化口径, 使"缺省时两处行为一致"。
VOL_FALLBACK_ANNUAL = 0.025 * (252 ** 0.5)

_CACHE: dict = {}


def _store():
    from h5i_bar_store import H5iBarStore
    return H5iBarStore()


def stats_for(canons, *, lookback: int = 20, end_day: str | None = None,
              store=None, use_cache: bool = True) -> dict:
    """一次取回多个标的的 {canon: {'adv': 成交额元, 'vol': 年化波动率, 'n': 样本数}}。

    lookback : 取最近多少个**交易日**的 amount 均值
    end_day  : 只用到该日为止的数据(不传 = 库内最新), 用于回放时避免前视

    取不到的标的**不出现在返回字典里**(而不是给 0) —— 调用方据此走常量成本
    分支, 与 `paper_book` 的 `avg_daily_volume is None` 语义一致。
    """
    syms = []
    for c in canons or []:
        s = str(c).split(".")[0].zfill(6)
        if s not in syms:
            syms.append(s)
    if not syms:
        return {}
    ck = (tuple(syms), int(lookback), str(end_day or ""))
    if use_cache and ck in _CACHE:
        return _CACHE[ck]
    try:
        st = store or _store()
        days = list(st.trading_days())
        if end_day:
            days = [d for d in days if d <= str(end_day)[:10]]
        if not days:
            return {}
        win = days[-int(lookback):] if len(days) > int(lookback) else days
        # change_pct 需要多一根才能算 N 个样本的 std, 故窗口取 lookback+1
        vol_win = days[-(int(lookback) + 1):] if len(days) > int(lookback) + 1 else days
        lo = min(win[0], vol_win[0])
        hi = max(win[-1], vol_win[-1])
        ph = ",".join("'%s'" % s for s in syms)
        q = (f"SELECT symbol, CAST(ts AS DATE) AS d, amount, change_pct FROM daily_bars "
             f"WHERE CAST(ts AS DATE) >= DATE '{lo}' AND CAST(ts AS DATE) <= DATE '{hi}' "
             f"AND symbol IN ({ph})")
        df = st._db.sql(q).to_pandas()
    except Exception:  # noqa: BLE001
        return {}
    if df is None or len(df) == 0:
        return {}
    df["d"] = df["d"].astype(str).str.slice(0, 10)
    out: dict = {}
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values("d")
        amt = g[g["d"].isin(set(win))]["amount"]
        amt = np.asarray(amt, dtype=float)
        amt = amt[np.isfinite(amt) & (amt > 0)]
        if amt.size == 0:
            continue
        adv = float(np.mean(amt))
        chg = np.asarray(g[g["d"].isin(set(vol_win))]["change_pct"], dtype=float)
        chg = chg[np.isfinite(chg)]
        vol = None
        if chg.size >= 5:
            # 与 backtest_engine 同口径: change_pct 为百分数 -> 先 /100 再年化
            r = chg / 100.0 if float(np.nanpercentile(np.abs(chg), 99.9)) > 0.5 else chg
            sd = float(np.std(r, ddof=1))
            if np.isfinite(sd) and sd > 0:
                vol = sd * (252 ** 0.5)
        canon = _to_canon(str(sym))
        out[canon] = {"adv": adv, "vol": (vol if vol else VOL_FALLBACK_ANNUAL),
                      "n_amount": int(amt.size), "n_change": int(chg.size),
                      "vol_source": ("measured" if vol else "fallback")}
    if use_cache:
        _CACHE[ck] = out
    return out


def _to_canon(sym6: str) -> str:
    s = str(sym6).zfill(6)
    if s.startswith(("60", "68", "90")):
        return f"{s}.SH"
    if s.startswith(("0", "3")):
        return f"{s}.SZ"
    if s.startswith(("4", "8")):
        return f"{s}.BSE"
    return f"{s}.SZ"


def clear_cache() -> None:
    _CACHE.clear()


# --------------------------------------------------------------------------
# 拆单判定: 一笔单该不该拆、拆几档
# --------------------------------------------------------------------------
def plan_slices(order_notional: float, adv: float | None, vol: float | None = None, *,
                participation_cap: float, lot: int = 100, price: float | None = None,
                max_slices: int = 20) -> dict:
    """**纯函数**: 给定单笔名义额与 ADV, 判定需要拆几档才能把参与率压到上限内。

    这里的参与率上限 `participation_cap` 是**调用方显式传入**的, 本模块不设默认值
    —— 它是唯一决定"拆不拆"的自由度, 与机构成交纪律同性质, 必须由运维给定并
    写进配置(与 `FACTOR_HYPOTHESIS_EVAL` 的处理一致: 隐式自由度会让结论无法审计)。

    返回 {'slices':int, 'participation':float, 'slices_needed':int,
          'capped_by_max_slices':bool, 'reason':str, 'notional_per_slice':float}
    """
    n = float(order_notional or 0.0)
    out = {"slices": 1, "participation": None, "slices_needed": 1,
           "capped_by_max_slices": False, "notional_per_slice": n, "reason": ""}
    if n <= 0:
        out["reason"] = "名义额非正"
        return out
    if not adv or adv <= 0:
        out["reason"] = "无 ADV(取不到行情) => 不拆, 走常量成本"
        return out
    p = n / float(adv)
    out["participation"] = p
    cap = float(participation_cap or 0.0)
    if cap <= 0:
        out["reason"] = "参与率上限未配置(<=0) => 不拆"
        return out
    if p <= cap:
        out["reason"] = f"参与率 {p * 100:.4f}% <= 上限 {cap * 100:.4f}%, 无需拆单"
        return out
    need = int(np.ceil(p / cap))
    out["slices_needed"] = need
    if need > int(max_slices):
        out["capped_by_max_slices"] = True
        need = int(max_slices)
    out["slices"] = need
    out["notional_per_slice"] = n / need
    out["reason"] = (f"参与率 {p * 100:.4f}% > 上限 {cap * 100:.4f}% => 需 {out['slices_needed']} 档"
                     + (f"(受 max_slices={max_slices} 限制, 实际 {need} 档, 余量跨日)"
                        if out["capped_by_max_slices"] else ""))
    return out


def thresholds_from_paper() -> dict:
    """拆单与动态成本的开关/参数(全部来自 `config.PAPER`)。"""
    try:
        from config import PAPER
    except Exception:  # noqa: BLE001
        return {"exec_split": False, "participation_cap": None,
                "dynamic_cost": False, "temp_coef_scale": None}
    return {
        "exec_split": bool(PAPER.get("exec_split", False)),
        "participation_cap": PAPER.get("participation_cap"),
        "dynamic_cost": bool(PAPER.get("dynamic_cost", False)),
        # 临时冲击系数缩放: 见 config 里对该值的完整说明(用本仓自己的成本假设标定)
        "temp_coef_scale": PAPER.get("impact_temp_coef_scale"),
    }
