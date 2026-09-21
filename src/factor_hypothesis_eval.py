# -*- coding: utf-8 -*-
"""⑭ 的**收益评估接线**: 把 `factor_hypothesis.validate_batch` 的
`return_evaluator` 回调接到本仓既有的因子评估器 (路线图 ⑭ 接线)。

为什么需要这一层
----------------
`factor_hypothesis` 的流水线刻意只留了一个 `return_evaluator(h, data) -> dict|bool`
回调, 并在 docstring 里要求"生产接线处必须显式传入自己的门槛参数"。本模块就是
那个生产接线处: 它负责

  1. 把 h5i 的因子截面 + 前向收益拼成评估器要的 **frame**(列: date, symbol, factor, fwd);
  2. 把假设的表达式**求值**成 `factor` 列(复用 `factor_hypothesis.evaluate_expression`,
     不另写求值器);
  3. 调 `factor_mine.evaluator.evaluate` 拿到 IC/ICIR/多空价差/换手/衰减;
  4. 按**显式传入**的门槛给出 `ok` 与理由。

前向收益口径(与虚拟盘一致, 不另造)
----------------------------------
`fwd5 = close[t+5] / close[t] - 1`, 用**复权** close, 跳到第 5 根 bar(不是自然日 5 天)
—— 与 `fml_accumulate.settle_mature` 完全同口径(`i + HOLD`, `p1/p0 - 1`)。
用自然日会跨周末/节假日错位, 且除权日会产生假收益。

不接 `daily_bars` 原始 close 的原因
-----------------------------------
`daily_bars.close` 是**不复权**价, 除权日的假跌会被算成负收益, 使任何因子看起来
都能"预测下跌"。故一律走 `backtest_engine.close_prices_for` 的复权序列。
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Callable, Sequence

import numpy as np
import pandas as pd

#: 本仓因子视图中可直接当"字段"用的列(与 v_factor_scores_daily 一一对应)
VIEW_FACTORS = ("f_signal", "f_trend", "f_govern", "f_liquidity", "f_vol", "f_mom_rev")

#: 需要另取日线的原始字段(供表达式引用)
BAR_FIELDS = ("close", "open", "high", "low", "volume", "amount", "turnover", "change_pct")

_HOLD = 5          # 前向收益期(与 fml_accumulate.HOLD 一致)
_TARGET_KEY = "fwd5"


def views_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p1 = os.path.join(base, "data", "h5i", "views", "v_factor_scores_daily.parquet")
    if os.path.exists(p1):
        return p1
    return os.path.join(base, "data", "views", "parquet", "v_factor_scores_daily.parquet")


def _load_view(path: str | None = None):
    """读因子视图 -> polars DataFrame(只读, 不落盘)。缺失返回 None。"""
    try:
        import polars as pl
    except Exception:  # noqa: BLE001
        return None
    p = path or views_path()
    if not os.path.exists(p):
        return None
    try:
        return pl.read_parquet(p)
    except Exception:  # noqa: BLE001
        return None


def build_frame(dates: Sequence[str], canons: Sequence[str] | None = None, *,
                hold: int = _HOLD, view_path: str | None = None,
                with_bar_fields: bool = True, con=None):
    """拼出评估器要的 frame: 列 = date / symbol / <因子列> / <日线字段> / fwd5。

    dates : 需要**评估日**的列表(YYYY-MM-DD); 前向收益会自动往后多看 hold 根 bar
            —— 数据末尾不足 hold 根的日子会被**丢弃**(不填 NaN、不假装算得出)。
    canons: 限定标的; None = 视图里出现的全部。
    返回 pandas DataFrame; 无数据时返回空 DataFrame(调用方应据此 skip 而不是判 fail)。
    """
    import pandas as pd

    v = _load_view(view_path)
    if v is None or not dates:
        return pd.DataFrame()
    ds = sorted({str(d)[:10] for d in dates})
    if canons:
        want = {str(c).split(".")[0].zfill(6) for c in canons}
        v = v.with_columns(
            __k=v["canon"].cast(str).str.replace(r"\..*$", "").str.zfill(6))
        v = v.filter(v["__k"].is_in(list(want))).drop("__k")
    v = v.with_columns(v["date"].cast(str).str.slice(0, 10).alias("date"))
    v = v.filter(v["date"].is_in(ds))
    if v.is_empty():
        return pd.DataFrame()
    df = v.to_pandas()
    df = df.rename(columns={"canon": "symbol"})
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["date"] = df["date"].astype(str).str.slice(0, 10)

    syms = sorted(df["symbol"].unique().tolist())
    # 价格窗口: 评估日之前 90 个自然日(供长窗口表达式) ~ 之后 30 个自然日(供前向收益)
    lo = (_d(ds[0]) - dt.timedelta(days=90)).isoformat()
    hi = (_d(ds[-1]) + dt.timedelta(days=30)).isoformat()
    bars = _adj_within(_load_bars_matrix(lo, hi, syms)) if with_bar_fields else None

    # 1) 日线字段: 视图里只有 6 个 f_* 因子, 表达式若引用量价字段必须补齐。
    #    补不到就留 NaN —— evidence_check 的覆盖率检查会如实报出来, 不会把
    #    "没有数据"静默当成"数据为 0"。
    if bars is not None and len(bars):
        keys = ["symbol", "d"]
        cols = [c for c in BAR_FIELDS if c in bars.columns]
        keep = bars[keys + cols].rename(columns={"d": "date"})
        # close 用**复权**列覆盖(其它字段仅作表达式输入, 不必复权)
        keep["close"] = bars["adj"].values if "adj" in bars.columns else keep.get("close")
        df = df.merge(keep, on=["date", "symbol"], how="left")
    else:
        for f in BAR_FIELDS:
            if f not in df.columns:
                df[f] = np.nan

    # 2) 前向收益: 逐 symbol 在**复权**序列上跳第 hold 根 bar
    if bars is not None and len(bars):
        fwd = _fwd_from_adj(bars, ds, hold, syms)
        if len(fwd):
            df = df.merge(fwd, on=["date", "symbol"], how="left")
        else:
            df[_TARGET_KEY] = np.nan
    else:
        df[_TARGET_KEY] = np.nan
    if _TARGET_KEY not in df.columns:
        df[_TARGET_KEY] = np.nan
    df = df[df[_TARGET_KEY].notna()].reset_index(drop=True)
    # (时间, 截面) 顺序 —— evidence_check 的分段稳定性依赖这个顺序
    df = df.sort_values(["date", "symbol"]).reset_index(drop=True)
    return df


def _d(x):
    import datetime as dt
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    s = str(x).strip().replace("-", "")[:8]
    return dt.datetime.strptime(s, "%Y%m%d").date()


def _change_pct_to_ratio(chg):
    """把 `change_pct` 列统一成**收益比值**(0.01 = +1%)。

    口径判别(用分位数, 不用中位数)
    ------------------------------
    两种口径都可能出现: 百分数(2.15 = +2.15%)与小数(0.0215 = +2.15%)。
    判别依据是 **A 股涨跌幅限制**这一物理事实:
      · 百分数口径下, |change_pct| 的 p99.9 必然 ~10 上下(主板 ±10%, 创业板/科创
        ±20%, 北交所 ±30%);
      · 小数口径下同一批数据被压缩 100 倍, p99.9 会 ~0.10。

    实测生产 h5i(2026-08 起 194,666 行): p99=10.03、max=261.5(含新股/复牌异常),
    即百分数口径 —— 与该判据一致。

    **为什么不用中位数**: 首次实现用"|chg| 中位数 > 1 => 百分数", 在边界上会判反
    —— 当 `change_pct` 恰好集中在 1.0(即 1%)时中位数 == 1.0, `> 1` 不成立, 于是
    1% 被当成 0.01% 处理, 复权序列被放大 100 倍(实测 fwd5 算出 +3100%)。
    分位数看的是**尾部量级**(受涨跌停约束的物理量), 不依赖数据中心的位置。

    缺失/全 NaN 时按百分数处理(生产口径), 并把非有限值置 NaN 由调用方剔除。
    """
    s = pd.to_numeric(chg, errors="coerce")
    vals = s.dropna().values
    if vals.size == 0:
        return s
    try:
        tail = float(np.nanpercentile(np.abs(vals), 99.9))
    except Exception:  # noqa: BLE001
        tail = 0.0
    is_percent = tail > 0.5
    return s / 100.0 if is_percent else s


def _load_bars_matrix(first_day: str, last_day: str, symbols: Sequence[str]):
    """**一次** SQL 取回 [first_day, last_day] 的全部日线(列名与 h5i 一致)。

    为什么不用 `backtest_engine.close_prices_for`: 那是**逐标的**取全史序列
    (每个 symbol 一次查询, 内部有 `_ADJ_CACHE`)。对单标的调用很合适, 但这里是
    "1000 只 × 25 天"的截面 —— 实测直接调用会退化成上万次查询并超时(第一次
    冒烟测试就是这么挂掉的)。截面场景必须**一批取回**, 否则这个日更步骤跑不完。
    """
    try:
        from h5i_bar_store import H5iBarStore
    except Exception:  # noqa: BLE001
        return None
    try:
        store = H5iBarStore()
        ph = ",".join("'%s'" % str(s) for s in symbols)
        q = (f"SELECT CAST(ts AS DATE) AS d, symbol, open, high, low, close, volume, "
             f"amount, change_pct, turnover FROM daily_bars "
             f"WHERE CAST(ts AS DATE) >= DATE '{first_day}' "
             f"AND CAST(ts AS DATE) <= DATE '{last_day}'")
        if ph:
            q += f" AND symbol IN ({ph})"
        q += " ORDER BY symbol, d"
        return store._db.sql(q).to_pandas()
    except Exception:  # noqa: BLE001
        return None


def _adj_within(bars):
    """给一批日线加一列 `adj`(symbol 内、**时间序**的复权相对价)。

    口径与 `backtest_engine._close_prices_for_h5i` 一致: 用 `change_pct`(数据源
    给出的真实日涨跌幅, 已含除权修正)复利重建。这里改成"**以该段最后一根为锚向前
    回推**" —— 在同一段内各日的**比值**与整史重建完全相同(比值与锚点无关), 却只
    需要这一段的数据。

    为什么不直接用 `close`: `daily_bars.close` 是**不复权**价, 除权日的假跌会被
    算成负收益, 使任何因子看起来都能"预测下跌"。
    """
    import numpy as np
    import pandas as pd

    if bars is None or len(bars) == 0:
        return bars
    b = bars.copy()
    b["d"] = b["d"].astype(str).str.slice(0, 10)
    b = b.sort_values(["symbol", "d"]).reset_index(drop=True)
    if "change_pct" not in b.columns:
        b["adj"] = pd.to_numeric(b["close"], errors="coerce")
        return b
    chg = pd.to_numeric(b["change_pct"], errors="coerce")
    ratio = _change_pct_to_ratio(chg)
    ratio = ratio.where(np.isfinite(ratio), 0.0).clip(lower=-0.999)
    grp = b.groupby("symbol", sort=False)
    b["adj"] = pd.to_numeric(b["close"], errors="coerce")
    # 以每组最后一根 close 为锚, 用后一日的比值把整段拉齐
    out = np.full(len(b), np.nan)
    for _sym, idx in grp.groups.items():
        rows = list(idx)
        c = pd.to_numeric(b.loc[rows, "close"], errors="coerce").values.astype(float)
        r = ratio.loc[rows].values.astype(float)
        adj = np.full(len(rows), np.nan)
        # 从末根向前: adj[i] = adj[i+1] / (1 + r[i+1])
        anchor = c[-1] if np.isfinite(c[-1]) and c[-1] > 0 else None
        if anchor is None:
            for j in range(len(rows) - 1, -1, -1):
                if np.isfinite(c[j]) and c[j] > 0:
                    anchor = c[j]
                    break
        if anchor is None:
            continue
        adj[-1] = anchor
        for j in range(len(rows) - 2, -1, -1):
            denom = 1.0 + (r[j + 1] if np.isfinite(r[j + 1]) else 0.0)
            adj[j] = adj[j + 1] / denom if denom > 1e-9 else adj[j + 1]
        out[rows] = adj
    b["adj"] = out
    return b


def _fwd_from_adj(b, days: Sequence[str], hold: int, symbols: Sequence[str]):
    """按复权序列算 `fwd{hold}` = adj[i+hold]/adj[i] - 1。"""
    import numpy as np
    import pandas as pd

    rows = []
    for sym, g in b.groupby("symbol", sort=False):
        g = g.dropna(subset=["adj"])
        if len(g) < hold + 1:
            continue
        ds = g["d"].tolist()
        ad = g["adj"].values.astype(float)
        pos = {d: i for i, d in enumerate(ds)}
        for d in days:
            i = pos.get(d)
            if i is None or i + hold >= len(ds):
                continue
            p0, p1 = ad[i], ad[i + hold]
            if p0 > 0 and np.isfinite(p0) and np.isfinite(p1):
                rows.append({"date": d, "symbol": sym,
                             _TARGET_KEY: float(p1 / p0 - 1.0)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 收益评估回调(接给 validate_batch 的 return_evaluator)
# --------------------------------------------------------------------------
def make_return_evaluator(*, min_ic: float, min_icir: float,
                          min_obs_days: int, min_abs_spread_pct: float = 0.0,
                          fwd: str = "fwd5") -> Callable:
    """构造 `(h, data) -> dict` 收益评估回调。

    **三个门槛全部必填, 无默认值** —— 这是刻意的: `factor_hypothesis` 的 docstring
    明确要求"生产接线处必须显式传入自己的门槛", 而隐式自由度会让"通过收益评估"
    变成一句无法审计的话。调用方应把它们写进配置。

    判据(全部满足才 ok):
      · IC 均值 >= min_ic (方向已由 evidence 检查过, 这里只要求强度)
      · ICIR >= min_icir (稳定性; 只看 IC 均值会把"靠几天极端值"的因子放进来)
      · 有效天数 >= min_obs_days (样本太短的 ICIR 没有意义)
      · |多空价差年化| >= min_abs_spread_pct (付得出成本的量级)

    返回 {'ok', 'ic_mean', 'icir', 'n_days', 'spread_annual', 'turnover', 'metric',
          'reasons', 'thresholds', 'evaluator'}。
    """
    def _eval(h, data) -> dict:  # noqa: ANN001
        import pandas as pd
        from factor_hypothesis import evaluate_expression
        out = {"ok": False, "reasons": [], "evaluator": "factor_mine.evaluator",
               "thresholds": {"min_ic": min_ic, "min_icir": min_icir,
                              "min_obs_days": min_obs_days,
                              "min_abs_spread_pct": min_abs_spread_pct}}
        if data is None or len(data) == 0:
            out["reasons"].append("无数据帧(前向收益窗口不足或视图缺失)")
            return out
        df = data if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
        if fwd not in df.columns:
            out["reasons"].append(f"数据帧缺 {fwd} 列")
            return out
        # 1) 表达式 -> factor 列
        try:
            vals = np.asarray(evaluate_expression(h, df.to_dict("list")), dtype=float)
        except Exception as e:  # noqa: BLE001
            out["reasons"].append(f"表达式求值失败: {type(e).__name__}: {e}")
            return out
        if vals.shape[0] != len(df):
            out["reasons"].append(f"表达式求值长度 {vals.shape[0]} != 数据帧 {len(df)}")
            return out
        frame = pd.DataFrame({"date": df["date"].values,
                              "symbol": df["symbol"].values,
                              "factor": vals,
                              fwd: df[fwd].values})
        frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
        if frame.empty:
            out["reasons"].append("表达式求值后无有效样本(覆盖率 0)")
            return out
        # 2) 交给本仓既有评估器
        try:
            import factor_mine.evaluator as EV
            rep = EV.evaluate(frame, "factor", fwd=fwd)
        except Exception as e:  # noqa: BLE001
            out["reasons"].append(f"factor_mine.evaluator 异常: {type(e).__name__}: {e}")
            return out
        # 注意口径: `factor_mine.evaluator.evaluate` 把 ic_mean/icir/n_days/t_stat/
        # win_rate 放在**报告根层**(见该函数 report 字典), 不是嵌在 `report['ic']` 里
        # —— 首次接线时按嵌套读, 结果三个门槛全部读到 None 而静默全拒。
        # 这里按**根层**读, 并把整份报告原样留在 metric 里以备审计。
        rep = rep or {}
        ls = rep.get("long_short") or {}
        to = rep.get("turnover") or {}
        out.update({
            "ic_mean": rep.get("ic_mean"), "icir": rep.get("icir"),
            "ic_std": rep.get("ic_std"),
            "ic_t": rep.get("t_stat"), "ic_win_rate": rep.get("win_rate"),
            "n_days": rep.get("n_days"),
            "spread_top_minus_bottom": rep.get("spread_top_minus_bottom"),
            "monotone": rep.get("monotone"),
            "spread_annual": ls.get("spread_annual"),
            "turnover": to.get("avg_turnover"),
            "metric": rep,
        })
        # 3) 显式门槛
        n_days = rep.get("n_days") or 0
        if n_days < min_obs_days:
            out["reasons"].append(f"有效天数 {n_days} < {min_obs_days}")
        icm = rep.get("ic_mean")
        if icm is None or not np.isfinite(float(icm)) or abs(float(icm)) < min_ic:
            out["reasons"].append(f"|IC| {icm} < {min_ic}")
        icir = rep.get("icir")
        if icir is None or not np.isfinite(float(icir)) or float(icir) < min_icir:
            out["reasons"].append(f"ICIR {icir} < {min_icir}")
        sa = ls.get("spread_annual")
        if min_abs_spread_pct > 0 and (sa is None or abs(float(sa)) < min_abs_spread_pct):
            out["reasons"].append(f"|多空年化价差| {sa} < {min_abs_spread_pct}")
        out["ok"] = not out["reasons"]
        return out

    return _eval


def thresholds_from_config() -> dict:
    """从 `config` 读收益评估门槛。**无内置数字** —— 缺配置时抛错。

    为什么抛错而不是给默认值: 收益门槛是本流水线唯一的"自由度", 一个写死的默认
    门槛会让"通过收益评估"变成无法审计的一句话。故要求调用方显式配置, 缺了就
    响亮失败。
    """
    from config import FACTOR_HYPOTHESIS_EVAL as C
    missing = [k for k in ("min_ic", "min_icir", "min_obs_days") if k not in C]
    if missing:
        raise KeyError(f"config.FACTOR_HYPOTHESIS_EVAL 缺键: {missing} (收益门槛必须显式配置)")
    return dict(C)


def run_hypothesis_evaluation(dates: Sequence[str], hypotheses,
                              *, evaluate: bool = True, ledger_path: str | None = None,
                              canons: Sequence[str] | None = None,
                              view_path: str | None = None) -> dict:
    """日更入口: 对一批假设跑完整流水线(lint -> evidence -> 收益)。

    与 `factor_hypothesis.validate_batch` 的唯一差别是**这里给出了数据与门槛**:
    它把 h5i 因子截面拼成 data, 把 `make_return_evaluator(门槛来自 config)` 作为
    回调传进去, 并把每阶段落账本。`evaluate=False` 时只做 lint+evidence
    (用于"先看逻辑是否成立"的日常巡检, 不做收益排序)。
    """
    import factor_hypothesis as FH
    from config import DATA_DIR

    data = build_frame(dates, canons, view_path=view_path)
    if data is None or len(data) == 0:
        return {"ok": False, "skip": True,
                "reason": "因子截面/前向收益为空(视图缺失或末尾数据不足 hold 根)",
                "n_dates": len(list(dates))}
    available = [c for c in data.columns if c not in ("date", "symbol", _TARGET_KEY)]
    ledger = FH.HypothesisLedger(ledger_path) if ledger_path else None
    ev = None
    if evaluate:
        try:
            ev = make_return_evaluator(**thresholds_from_config())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "skip": True,
                    "reason": f"收益门槛未配置: {type(e).__name__}: {e}",
                    "hint": "在 config.py 增加 FACTOR_HYPOTHESIS_EVAL = "
                            "{'min_ic':…, 'min_icir':…, 'min_obs_days':…}"}
    res = FH.validate_batch(list(hypotheses), data, available_fields=available,
                            ledger=ledger, target_key=_TARGET_KEY,
                            return_evaluator=ev)
    res.update({"ok": True, "n_rows": int(len(data)),
                "n_dates": int(data["date"].nunique()) if "date" in data else 0,
                "n_symbols": int(data["symbol"].nunique()) if "symbol" in data else 0,
                "evaluated": bool(evaluate),
                "ledger": (ledger.path if ledger else None),
                "data_range": [str(data["date"].min()), str(data["date"].max())]
                if "date" in data else None})
    return res
