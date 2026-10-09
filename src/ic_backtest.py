# -*- coding: utf-8 -*-
# ic_backtest.py -- 基于全A历史(DuckDB daily_bars)的因子 IC 回算
#
# 数据:   daily_bars(2010~至今), 用 change_pct(前复权日涨跌幅)构建因子与未来收益
# 目标:   在任意历史区间, 计算指定因子在每日横截面上与未来 N 日收益的 Rank IC,
#         输出 IC 时间序列与汇总(均值IC/ICIR/年化IR/胜率), 存到 data/ic/
#
# 因子内置:
#   mom_k     动量: 近K日日涨跌幅累计(复利)  -- selector 的 trend 成分
#   可用 -f 扩展, 或脚本内 FACTORS 表中加
#
# 用法:
#   python ic_backtest.py -K 20 -H 1 3 5 10 20
#   python ic_backtest.py -K 20 -start 2016-01-01 -end 2024-12-31   # 限定区间
#   python ic_backtest.py --plot                                        # 顺带画IC曲线
# ============================================================
import os
import sys
import argparse
import datetime

import duckdb
import numpy as np
import pandas as pd

from config import DUCKDB_PATH, DATA_DIR

IC_DIR = os.path.join(DATA_DIR, "ic")


def spearman_ic(series_x: pd.Series, series_y: pd.Series) -> float:
    """横截面 Rank IC: spearman(x, y)。x=因子, y=未来收益。"""
    pair = pd.concat([series_x, series_y], axis=1, join="inner").dropna()
    if len(pair) < 5:
        return float("nan")
    rx = pair.iloc[:, 0].rank()
    ry = pair.iloc[:, 1].rank()
    corr = rx.corr(ry)
    return float(corr) if not pd.isna(corr) else float("nan")


def build_factor(pct: pd.DataFrame, k: int, factor: str):
    """从已规范为小数的宽表计算 IC 曲线因子。

    与现行 ``build_factor_views`` 的日频视图保持同口径：``mom_20`` 是
    涨跌幅滚动均值，``reversal`` 为其相反数，``vol`` 是滚动总体标准差。
    这里直接对宽表计算，避免依赖已移除的旧 ``factor_library.FACTORS``
    wide 注册表。
    """
    if factor.startswith("mom_"):
        try:
            kk = int(factor.split("_", 1)[1])
        except (IndexError, ValueError):
            raise ValueError("无效动量因子: {}".format(factor)) from None
        if kk <= 0:
            raise ValueError("动量窗口必须为正数: {}".format(kk))
        return pct.rolling(kk, min_periods=kk).mean()
    if factor == "reversal":
        return -pct.rolling(k, min_periods=k).mean()
    if factor == "vol":
        return pct.rolling(20, min_periods=20).std(ddof=0)
    raise ValueError("未知因子: {} (可用: mom_<k>|vol|reversal)".format(factor))


def fwd_return(pct: pd.DataFrame, h: int) -> pd.DataFrame:
    """未来 H 日收益: 从 t 收盘持有 H 日, (prod(1+pct[t+1..t+H])-1)。
    用 shift(-h) 对 rolling 累乘实现, 避免前视(t 之后才开始计收益)。
    """
    ret = (1.0 + pct).rolling(h).apply(np.prod, raw=True) - 1.0
    # ret[t] 是 t-h+1..t 的累计; 我们需要 t+1..t+H -> 用 shift(-h)
    return ret.shift(-h)


def _pivot_change_pct(df: pd.DataFrame) -> pd.DataFrame | None:
    """把日线长表的百分比点涨跌幅规范成 IC 所需的小数宽表."""
    if df is None or df.empty:
        return None
    date_col = "d" if "d" in df.columns else "date"
    required = {date_col, "symbol", "change_pct"}
    if not required.issubset(df.columns):
        raise ValueError("daily_bars 缺少 IC 所需列: {}".format(
            sorted(required - set(df.columns))))
    work = df[[date_col, "symbol", "change_pct"]].copy()
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    work["change_pct"] = pd.to_numeric(work["change_pct"], errors="coerce") / 100.0
    work = work.dropna(subset=[date_col, "symbol", "change_pct"])
    if work.empty:
        return None
    return work.pivot_table(index=date_col, columns="symbol", values="change_pct")


def _load_pct_h5i(start: str, end: str) -> pd.DataFrame | None:
    """从 h5i 读取未复权日线; h5i 的 change_pct 单位是百分比点."""
    from h5i_bar_store import H5iBarStore

    store = H5iBarStore()
    try:
        rows = store.closes_window(start, end, positive_close_only=True)
    finally:
        store.close()
    return _pivot_change_pct(rows)


def load_pct(start: str, end: str, use_adj: bool = False) -> pd.DataFrame | None:
    """加载 IC 回算收益，优先使用 h5i，legacy DuckDB 仅作兼容回退.

    ``use_adj=True`` 仍沿用已有 DuckDB ASOF 复权路径；默认未复权路径使用
    h5i 的标准日线接口，避免在 DuckDB 已退役后刷新器直接失败。
    """
    if use_adj:
        return load_adj_pct(start, end)

    h5i_error = None
    if os.environ.get("BAR_STORE", "h5i").strip().lower() != "duck":
        try:
            pct = _load_pct_h5i(start, end)
            if pct is not None and not pct.empty:
                return pct
            h5i_error = "h5i 区间无有效 change_pct"
        except Exception as exc:  # noqa: BLE001
            h5i_error = f"{type(exc).__name__}: {exc}"

    if not os.path.exists(DUCKDB_PATH):
        raise RuntimeError(
            "h5i 未返回有效 IC 数据 ({})，legacy DuckDB 已退役且不存在: {}".format(
                h5i_error or "BAR_STORE=duck", DUCKDB_PATH
            )
        )

    import duckdb
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        df = con.execute(
            """
            SELECT date, symbol, change_pct
            FROM daily_bars
            WHERE date >= ? AND date <= ?
                  AND change_pct IS NOT NULL
            """,
            [start, end],
        ).fetchdf()
    finally:
        con.close()
    return _pivot_change_pct(df)


def load_adj_pct(start, end):
    """用 DuckDB ASOF JOIN 计算前复权日收益。

    语义: 对每只股票的每个交易日, 取 trade_date <= 当日 的最近 adj_factor
        (即当日实际生效的前复权因子), close_adj = close * adj_factor,
        日收益 = close_adj.pct_change()。
    返回宽表 pct: index=date, columns=symbol, 值为小数收益。
    """
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        # DuckDB ASOF JOIN: 等价 pandas merge_asof(direction=backward)
        df = con.execute("""
            SELECT b.date, b.symbol,
                   COALESCE(a.adj_factor, 1.0) AS adj_factor,
                   b.close, b.change_pct
            FROM daily_bars b
            ASOF LEFT JOIN adj_factors a
              ON b.symbol = a.symbol AND b.date >= a.trade_date
            WHERE b.date >= ? AND b.date <= ?
                  AND b.change_pct IS NOT NULL
        """, [start, end]).fetchdf()
    finally:
        con.close()

    if df.empty:
        return None

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0)
    df["close_adj"] = df["close"] * df["adj_factor"]
    df = df.sort_values(["symbol", "date"])
    # 前复权日收益: close_adj 的一阶对数差分
    df["pct"] = df.groupby("symbol")["close_adj"].pct_change()
    df = df[["date", "symbol", "pct"]]
    pct = df.pivot_table(index="date", columns="symbol", values="pct")
    return pct


def run(start: str, end: str, k: int, holds, factor: str, use_adj: bool,
        write_curve: bool = True):
    """全历史/区间因子 IC 回算。

    write_curve=False 时仅返回 ic_series 与默认文件路径, 不落盘
    (供 ic_curve_refresh 增量合并: 先算近端, 再与既有历史合并后统一写)。
    """
    if use_adj:
        pct = load_adj_pct(start, end)
        if pct is None:
            print("区间内无 daily_bars 数据")
            return None, None
        print("(adj) 前复权收益 pivot 形状: {} 天 x {} 只".format(pct.shape[0], pct.shape[1]))
    else:
        pct = load_pct(start, end, use_adj=False)
        if pct is None or pct.empty:
            print("区间内无 daily_bars 数据")
            return None, None
        print("pivot 形状: {} 天 x {} 只".format(pct.shape[0], pct.shape[1]))

    sig = build_factor(pct, k, factor)

    # 逐持有期算 IC 时间序列
    ic_series = {}
    for h in holds:
        fwd = fwd_return(pct, h)
        # 对齐 sig 与 fwd 的索引
        common_idx = sig.index.intersection(fwd.index)
        s = sig.loc[common_idx]
        f = fwd.loc[common_idx]
        ics = []
        for dt in common_idx:
            ic = spearman_ic(s.loc[dt], f.loc[dt])
            if not np.isnan(ic):
                ics.append((dt, ic))
        ic_series[h] = pd.Series(dict(ics)).sort_index()

    # 落 IC 曲线 (write_curve=False 时跳过, 由调用方负责合并写盘)
    os.makedirs(IC_DIR, exist_ok=True)
    curve = pd.DataFrame(ic_series)
    curve = curve.rename(columns=lambda hh: "ic_h{}".format(hh)).reset_index()
    curve.columns = ["day"] + list(curve.columns[1:])
    curve["day"] = curve["day"].dt.strftime("%Y%m%d")
    curve_file = os.path.join(IC_DIR, "ic_curve_{}_k{}.csv".format(factor, k))
    if write_curve:
        curve.to_csv(curve_file, index=False)
        print("IC 曲线已写: {}".format(curve_file))

    # 汇总
    print("\n===== IC 汇总 (factor={}, K={}) =====".format(factor, k))
    for h in holds:
        s = ic_series[h].dropna()
        if s.empty:
            print("hold={}: 无数据".format(h))
            continue
        ic_mean = s.mean()
        ic_std = s.std(ddof=1)
        icir = (ic_mean / ic_std) if ic_std > 0 else float("nan")
        n = len(s)
        positive = (s > 0).mean()
        t_stat = (ic_mean / (ic_std / np.sqrt(n))) if n > 1 and ic_std > 0 else float("nan")
        print("hold={:>2d}: IC mean={:+.4f} std={:.4f} ICIR={:+.3f} 胜率={:.1%} "
              "n={} t={:+.2f} 近20日均值={:+.4f}".format(
                  h, ic_mean, ic_std, icir, positive, n, t_stat,
                  s.tail(20).mean()))

    return ic_series, curve_file


def main():
    ap = argparse.ArgumentParser(description="全A历史因子 IC 回算")
    today = datetime.date.today().isoformat()
    ap.add_argument("-K", "--lookback", type=int, default=20, help="动量回看天数(因子参数)")
    ap.add_argument("-H", "--hold", type=int, nargs="+", default=[1, 3, 5, 10, 20],
                    help="未来持有期(交易日)")
    ap.add_argument("-f", "--factor", default="mom_20",
                    help="因子: mom_<k>|reversal|vol")
    ap.add_argument("-start", default="2013-01-01", help="回算起始日")
    ap.add_argument("-end", default=today, help="回算结束日")
    ap.add_argument("--plot", action="store_true", help="顺带画IC曲线图(需matplotlib)")
    ap.add_argument("--use-adj", action="store_true",
                    help="用 adj_factors 前复权价算收益(默认关, 用原始change_pct)")
    args = ap.parse_args()

    holds = sorted(set(args.hold))
    # 因子名以 mom_ 前缀与 K 参数联动: 若用户写 mom_20 则 K=20
    if args.factor.startswith("mom_"):
        k = int(args.factor.split("_")[1])
    else:
        k = args.lookback
        args.factor = "mom_{}".format(k) if args.factor == "mom_20" else args.factor

    ic_series, curve_file = run(args.start, args.end, k, holds, args.factor, args.use_adj)

    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            # 每只股票的去中心化IC曲线会太密, 画20日均值平滑
            fig, axes = plt.subplots(len(holds), 1, figsize=(10, 2.4 * len(holds)),
                                     sharex=True, squeeze=False)
            for ax, h in zip(axes, holds):
                s = ic_series[h].dropna()
                if s.empty:
                    continue
                ax.axhline(0, color="gray", lw=0.8)
                ax.plot(s.index, s.rolling(20).mean(), label="IC_mean20(h={})".format(h))
                ax.legend(loc="upper right", fontsize=8)
                ax.set_ylabel("IC20")
            plt.tight_layout()
            png = curve_file.replace(".csv", ".png")
            plt.savefig(png, dpi=120)
            print("IC 曲线图已写: {}".format(png))
        except ImportError:
            print("matplotlib 未安装, 跳过画图")


if __name__ == "__main__":
    sys.exit(main())
