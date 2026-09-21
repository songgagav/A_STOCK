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


# PIT 现场选股的磁盘缓存目录 (2026-09-13)
PIT_SEL_CACHE_DIR = os.path.join(DATA_DIR, "pit", "selection_cache")


def _pit_cache_path(day: str, n: int) -> str:
    # 2026-09-13: 排序/掺入口径不同 -> 选股结果不同, 缓存必须隔离, 否则开关切换会命中旧产物
    mode = os.environ.get("RANK_BY_FUSION", "0")
    tag = ""
    if mode not in ("", "0"):
        tag = f"_rf{mode}a{os.environ.get('FUSION_RANK_ALPHA', '1.0')}"
    # 融合分截尾(FUSION_TRIM_Q)改变的是 score(掺入口径), 与 RANK_BY_FUSION 无关,
    # 因此即使不切排序也要带标签隔离缓存。
    try:
        _q = float(os.environ.get("FUSION_TRIM_Q", "0") or 0.0)
    except (TypeError, ValueError):
        _q = 0.0
    if _q > 0:
        tag += f"_t{_q}"
    return os.path.join(PIT_SEL_CACHE_DIR, f"{day}_n{int(n)}{tag}.json")


def _out_dir(day_dir: str, out_tag: str = "") -> str:
    """回测产物目录. out_tag 非空时隔离到 <day_dir>__<tag>/, 避免参数扫描
    (PBO 的 config×window 矩阵)覆盖正式 OOS 窗口产物。"""
    name = f"{day_dir}__{out_tag}" if out_tag else day_dir
    return os.path.join(DATA_DIR, "vnpy_backtest", name)


#: [路线图 #9] 多场景结果缓存: key = (day, top_n, lookback_days, sel_n, weight_mode,
#: forward, scenario)。同一进程内重复请求同一场景直接复用 —— 选股(PIT)是耗时大头,
#: 而它在各场景间**完全相同**, 重算等于把 CPU 花在重复劳动上。缓存**不跨进程**,
#: 因此不会把上一次的费率假设偷偷带进下一次运行。
_REGIME_CACHE: dict = {}


def run_regime_scenarios(day: str, scenarios=None, **kwargs) -> dict:
    """[路线图 #9] 同一段回测在多个成本/延迟场景下各跑一遍, 给出可比结果。

    "可比"是这里唯一重要的事: 所有场景**共用**同一份目标池、同一份 bar、同一个
    撮合引擎, 唯一变量是费率倍率与信号延迟。因此 `delta_return_pct` =
    "这套信号在压力成本下会损失多少", 而不是两套不同假设下不可比的两个数。

    Parameters
    ----------
    scenarios : 场景名序列; None 时取 `PAPER.regime_scenarios`(标准测试项, 现行
        normal+stress)。非法名会由 `regime_costs.resolve_scenario` **抛错**
        (不静默回退到 normal —— 那会让"压力测试通过了"变成一句空话)。
    **kwargs : 透传 `run_vnpy_backtest`(top_n / lookback_days / sel_n / weight_mode /
        forward / persist_arctic ...)。`scenario` 由本函数接管。

    返回 {'ok','day','scenarios': {name: {...}}, 'comparison': [...],
          'baseline': str, 'kwargs': {...}}
    """
    import regime_costs as _RC

    if scenarios is None:
        scenarios = PAPER.get("regime_scenarios") or [DEFAULT_SCENARIO_NAME]
    names = [str(s) for s in scenarios]
    for n in names:                      # fail fast: 先校验全部场景名
        _RC.resolve_scenario(n)
    base = names[0] if names else "normal"
    if kwargs.pop("scenario", None) is not None:
        raise TypeError("run_regime_scenarios 自行接管 scenario 参数, 不要透传")

    out: dict = {}
    _base_tag = kwargs.pop("out_tag", "") or ""
    for n in names:
        # 每个场景写到独立目录(data/vnpy_backtest/<day>__regime_<name>/):
        # 同一目录会被后一个场景覆盖, 使"压力场景的数字"与"常态场景"无从区分 ——
        # 那正是本功能要消除的失效模式。显式传入的 out_tag 仍作为前缀保留。
        _tag = f"{_base_tag}__regime_{n}" if _base_tag else f"regime_{n}"
        out[n] = run_vnpy_backtest(day, scenario=n, out_tag=_tag, **kwargs)

    def _stat(res: dict, key: str):
        st = (res or {}).get("stats") or {}
        for k in (key, f"{key}_pct"):
            if st.get(k) is not None:
                return st[k]
        return None

    base_res = out.get(base) or {}
    comparison = []
    for n in names:
        res = out.get(n) or {}
        row = {
            "scenario": n,
            "ok": bool(res.get("ok")),
            "total_return_pct": _stat(res, "total_return"),
            "max_drawdown_pct": _stat(res, "max_drawdown"),
            "final_balance": _stat(res, "end_balance") or _stat(res, "final_balance"),
            "regime": (res.get("regime") or {}),
            "fallback": bool(res.get("fallback")),
        }
        b = _stat(base_res, "total_return")
        r = _stat(res, "total_return")
        row["delta_return_pct"] = (round(float(r) - float(b), 4)
                                  if (r is not None and b is not None) else None)
        comparison.append(row)
    return {"ok": all(bool((out.get(n) or {}).get("ok")) for n in names),
            "day": day, "scenarios": out, "comparison": comparison,
            "baseline": base, "kwargs": {k: v for k, v in kwargs.items()}}


#: 兼容别名: 默认场景名(避免调用方为了一个字符串去 import regime_costs)
DEFAULT_SCENARIO_NAME = "normal"


def run_regime_batch(days, scenarios=None, *, stop_on_error: bool = False,
                     **kwargs) -> dict:
    """[路线图 #9] 对**多个交易日**跑多场景, 汇总成"成本敏感性"面板。

    这是把 regime 场景接进日常流程的入口: 单日回测只能说明"这一天如何", 而
    成本敏感性是个**分布**性质 —— 必须在多天上平均才有意义。故本函数返回
    逐日逐场景的明细 **以及** 按场景聚合的均值, 后者才是可用于决策的数。

    days : 交易日序列(YYYY-MM-DD)
    stop_on_error : False(默认)时某天失败记录 error 继续跑完其余天 —— 一天数据
        缺失不该让整批无结果; True 则首错即停(供 CI 用)。
    返回 {'ok','days':[...], 'per_day':{day: {scenario: row}}, 'summary':
          {scenario: {'n_days','n_ok','mean_return_pct','mean_delta_pct',
                      'worst_delta_pct','mean_max_dd_pct','degraded_days'}}}
    """
    per_day: dict = {}
    errs: list = []
    for d in days:
        try:
            res = run_regime_scenarios(d, scenarios=scenarios, **kwargs)
        except Exception as e:  # noqa: BLE001
            errs.append({"day": d, "error": f"{type(e).__name__}: {e}"})
            if stop_on_error:
                raise
            continue
        per_day[d] = {row["scenario"]: row for row in res.get("comparison") or []}

    names = sorted({s for rows in per_day.values() for s in rows})
    summary: dict = {}
    for n in names:
        rets, deltas, dds = [], [], []
        for rows in per_day.values():
            row = rows.get(n) or {}
            if row.get("total_return_pct") is not None:
                rets.append(float(row["total_return_pct"]))
            if row.get("delta_return_pct") is not None:
                deltas.append(float(row["delta_return_pct"]))
            if row.get("max_drawdown_pct") is not None:
                dds.append(float(row["max_drawdown_pct"]))

        def _mean(xs):
            return round(sum(xs) / len(xs), 4) if xs else None

        summary[n] = {
            "n_days": len(per_day),
            "n_ok": len(rets),
            "mean_return_pct": _mean(rets),
            "mean_delta_pct": _mean(deltas),
            "worst_delta_pct": round(min(deltas), 4) if deltas else None,
            "mean_max_dd_pct": _mean(dds),
            # 相对基准场景收益变差的交易日占比 —— "压力场景下这套信号还成立吗"
            "degraded_days": sum(1 for x in deltas if x < 0),
        }
    return {"ok": bool(per_day) and not errs, "days": list(days),
            "per_day": per_day, "summary": summary, "scenarios": names,
            "errors": errs}


def _make_weights(targets: list[dict], mode: str = "equal") -> list[float]:
    """按配置构造目标权重(和为 1)。PBO 参数扫描的第二根轴。

    equal  : 等权(实盘默认, 与改动前一致)
    signal : 按候选 signal 归一化(信号越强仓位越重)
    rank   : 按 signal 降序线性递减加权(最强 n 份 ... 最弱 1 份)

    signal 缺失或非正时回退等权, 保证任何情况下权重都合法(非负且和为 1)。
    """
    n = len(targets)
    if n == 0:
        return []
    if mode in ("signal", "rank"):
        s = np.array([float(t.get("signal") or 0.0) for t in targets], dtype=np.float64)
        s = np.where(np.isfinite(s), s, 0.0)
        if mode == "signal":
            s = np.clip(s, 0.0, None)
            tot = float(s.sum())
            if tot > 1e-12:
                return [float(x) for x in (s / tot)]
        elif np.ptp(s) > 1e-12:
            order = np.argsort(-s, kind="mergesort")
            lin = np.arange(n, 0, -1, dtype=np.float64)     # n, n-1, ..., 1
            w = np.empty(n, dtype=np.float64)
            w[order] = lin
            return [float(x) for x in (w / w.sum())]
    return [1.0 / n] * n


def _load_pit_selection_cached(day: str, n: int = 10) -> list[dict]:
    """PIT 现场选股(带按 day 的磁盘缓存), 返回 top_n 列表; 失败返回 [].

    为何缓存: PBO 参数扫描需要对同一窗口跑多组配置, 而 PIT 选股占单次回测耗时的
    大头(实测 ~85~270s, 而纯仿真仅 ~10s), 且选股结果与 top_n/lookback_days 无关
    (top_n 只是事后切片) -> 按 day 缓存后, 同窗口的第 2..N 组配置可完全复用。
    缓存 key 含 n: 选股深度不同(如为 top_n 扫描取 20)会得到不同的列表, 取足够大
    的 n 后各配置按需切片即可。
    """
    fp = _pit_cache_path(day, n)
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("targets"):
                return d["targets"]
        except Exception:  # noqa: BLE001  缓存损坏则重算
            pass
    try:
        from db import StockDB
        from selector import RotationSelector
        sel = RotationSelector(StockDB(), n=int(n)).select(hist_day=day)
        if not sel or sel.get("error"):
            return []
        targets = sel.get("top_n") or []
    except Exception as e:  # noqa: BLE001
        _log(f"PIT 现场选股回退失败: {type(e).__name__}: {str(e)[:150]}")
        return []
    if targets:
        try:
            os.makedirs(PIT_SEL_CACHE_DIR, exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump({"day": day, "n": int(n), "targets": targets,
                           "generated_at": dt.datetime.now().isoformat()},
                          f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001  缓存写失败不影响回测
            pass
    return targets


_BARS_WARNED: dict = {}


def _warn_bars_once(key: str, msg: str) -> None:
    """日线读取失败只告警一次 (2026-09-13).

    原实现两路取数失败都静默返回空表, 上层若不校验只数就会用 1~2 只标的算均值
    (历史上出现过"篮子收益严重失真"), 或把数据源故障误读成"区间无数据"。
    """
    if not _BARS_WARNED.get(key):
        _BARS_WARNED[key] = True
        print(msg, file=sys.stderr)


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
    except Exception as e:  # noqa: BLE001
        _warn_bars_once("duck", f"[bars] duckdb 源不可用({type(e).__name__}: {e}); 转 h5i 源")

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
    except Exception as e:  # noqa: BLE001
        _warn_bars_once("h5i", f"[bars] h5i 源不可用({type(e).__name__}: {e}) -> 返回空表; "
                                f"调用方必须校验只数, 否则篮子均值会失真")
        return pd.DataFrame()


def _build_adj_close_inplace(df: pd.DataFrame) -> None:
    """用 build_adj_close 在 df 中增加 adj_close 列 (原地修改)."""
    from factor_library import build_adj_close
    df["adj_close"] = build_adj_close(df)


# ---------------------------------------------------------------------------
# 前向窗口 (2026-09-13): 修正原"回测窗口在决策日之前"的前视问题
#
# 原实现用 _load_bars(s6, day, 120) 取 [day-120, day] 的日线, 而目标池是
# select(hist_day=day)(特征截至 day) —— 评估期整体落在决策日之前, 选股因此
# "知道"了整个评估期的走势。实测对照(见 check_lookahead):
#   2022-12-30 窗口回测 +98.08% vs 所选 10 只在该区间自身的等权涨幅 +93.47%
#   2021-07-02 窗口回测 +73.25% vs +78.48%
#   2024-07-03 窗口回测 +23.74% vs +27.67%
# 即回测收益 ≈ "用已知结果挑出的动量篮子"在该区间的涨幅, 属机械前视。
#
# 修正: 窗口改为 [决策日, 决策日 + N 个交易日], 选股仍为 PIT(数据 <= 决策日),
# 持有期全部在决策日之后 -> 无前视。
# ---------------------------------------------------------------------------
_CAL_CACHE: dict = {}


def _cache_calendar(ds: list) -> list:
    """仅在**非空**时写入进程缓存并返回.

    2026-09-13 修正: 原实现无条件 `_CAL_CACHE["cal"] = ds`, 于是首次调用一旦失败
    (h5i 客户端缺失 / 被瞬时占用), 该进程之后**全部** forward_window_days 都返回
    [], 上层把它显示成"前向窗口未来数据不足" —— 真实故障被伪装成数据不全。
    空结果一律视为失败, 不缓存 (下次调用会重新尝试)。
    """
    if ds:
        _CAL_CACHE["cal"] = ds
    return ds


def _calendar_from_static_file() -> list:
    """data/trade_calendar.json -> 升序交易日列表 'YYYY-MM-DD' (项目自带官方日历).

    注意: 该文件的 days 是 **'YYYYMMDD'** 无分隔格式, 必须归一化后再返回, 否则
    与 'YYYY-MM-DD' 混用会破坏 bisect 的字符串比较(窗口会全空)。
    """
    try:
        with open(os.path.join(DATA_DIR, "trade_calendar.json"), encoding="utf-8") as f:
            days = (json.load(f) or {}).get("days") or []
    except Exception:  # noqa: BLE001
        return []

    def _norm(d) -> str:
        s = str(d).strip()
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        return s[:10]

    return sorted({_norm(d) for d in days if str(d).strip()})


def _full_calendar() -> list:
    """全部交易日(升序, 'YYYY-MM-DD'), 进程内缓存(仅缓存非空结果).

    数据源优先级:
      1) h5i daily_bars 的 distinct ts (实盘主源, 范围与真实日线一致)
      2) DuckDB daily_bars (已退役, 仅当文件仍在时可用)
      3) data/trade_calendar.json (项目自带官方日历, 覆盖 1990~2026)
    三源全空时打印明确告警, 便于区分"数据源故障"与"未来数据不足"。
    """
    cal = _CAL_CACHE.get("cal")
    if cal:
        return cal

    ds: list = []
    try:
        from h5i_bar_store import H5iBarStore
        ds = sorted(H5iBarStore().trading_days())
    except Exception as e:  # noqa: BLE001
        print(f"[calendar] h5i 交易日历不可用: {type(e).__name__}: {e}", file=sys.stderr)

    if not ds:
        try:
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            try:
                rows = con.execute(
                    "SELECT DISTINCT date FROM daily_bars ORDER BY date").fetchall()
            finally:
                con.close()
            ds = [str(r[0])[:10] for r in rows]
        except Exception:  # noqa: BLE001
            ds = []

    if not ds:
        ds = _calendar_from_static_file()
        if ds:
            print(f"[calendar] 已回退 data/trade_calendar.json ({len(ds)} 个交易日, "
                  f"{ds[0]} ~ {ds[-1]}); 该源非实盘主源, 若区间内个股日线缺失会另行报错",
                  file=sys.stderr)

    if not ds:
        print("[calendar] 警告: h5i / duckdb / trade_calendar.json 三个日历源全部不可用, "
              "前向窗口将全部被判为'未来数据不足'; 请检查 h5i_db 客户端是否已安装",
              file=sys.stderr)
    return _cache_calendar(ds)


def forward_window_days(start_day: dt.date, days: int) -> list:
    """返回 [start_day, 之后 days 个交易日] 的交易日列表; 未来数据不足则返回 [].

    交易日以"首个 >= start_day 的交易日"为起点, 保证窗口不包含决策日之前的数据。
    """
    cal = _full_calendar()
    if not cal:
        return []
    import bisect
    i = bisect.bisect_left(cal, start_day.strftime("%Y-%m-%d"))
    seg = cal[i:i + days]
    return seg if len(seg) >= days else []


def _load_bars_forward(symbol6: str, start_day: dt.date, days: int) -> pd.DataFrame:
    """加载 [start_day 起 days 个交易日] 的日线(决策日之后, 不含决策日之前).

    与 _load_bars 的区别: 后者取"截至 end_day 的最后 days 根", 用于回溯评估;
    本函数取"自 start_day 起的前 days 根", 用于无前视的前向评估。
    复权口径与 _load_bars 一致(同走 build_adj_close)。
    """
    seg = forward_window_days(start_day, days)
    if not seg:
        return pd.DataFrame()
    end_day = dt.date.fromisoformat(seg[-1])
    # 多取一些以覆盖个股停牌造成的缺口, 再截到 [start_day, end_day]
    df = _load_bars(symbol6, end_day, days * 3 + 40)
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    df = df[df["date"] >= pd.Timestamp(start_day)].reset_index(drop=True)
    if df.empty:
        return df
    return df.head(days).reset_index(drop=True)


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


def run_vnpy_backtest(day: str, top_n: int = 10, lookback_days: int = 120,
                      sel_n: int = 0, out_tag: str = "",
                      persist_arctic: bool = True,
                      weight_mode: str = "equal",
                      forward: bool = False,
                      scenario: str | None = None) -> dict:
    """vnpy 回测. day 语义随 forward 变化:

    forward=False (默认, 历史行为): day = 窗口**终点**; 窗口为 [day-lookback_days, day];
        目标池为 as-of day 的选股。注意: 评估期落在决策日之前, 存在前视
        (见 _load_bars_forward 上方注释), 仅用于回溯归因, 不可用于样本外评估。
    forward=True: day = **决策日**; 目标池为 PIT(数据 <= day) 的选股; 持有期
        [day, day+lookback_days 个交易日], 全部在决策日之后 -> 无前视, 用于 OOS。
    scenario: [路线图 #9] 成本/延迟场景(normal / stress); None 时按
        `PAPER.regime_scenario` -> 环境变量 REGIME_SCENARIO -> normal 解析。
        **默认 normal 的倍率全为 1、延迟 0, 与既有单场景结果逐位一致**;
        stress 只在显式指定时生效, 绝不静默混入既有产物。
    """
    day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
    day_dir = day.replace("-", "")

    # [路线图 #9] 场景解析与缓存放在最前面(在昂贵的选股之前): 多场景回测下
    # 同一 (day, 参数, 场景) 只应真跑一次。缓存命中时**返回深拷贝** ——
    # 否则调用方对返回值的任何就地修改都会污染缓存, 使后续场景拿到被改过的数。
    import regime_costs as _RC
    _sc = _RC.resolve_scenario(
        scenario if scenario is not None
        else (PAPER.get("regime_scenario") or None))
    _ck = (day, int(top_n), int(lookback_days), int(sel_n), str(weight_mode),
           bool(forward), _sc["name"])
    if _ck in _REGIME_CACHE:
        import copy as _copy
        _log(f"[regime:{_sc['name']}] 命中场景缓存, 复用本进程内已有结果")
        return _copy.deepcopy(_REGIME_CACHE[_ck])

    # 前向模式先在昂贵的选股之前校验未来数据是否充足(fail fast, 且不依赖 vnpy)
    if forward and not forward_window_days(day_dt, lookback_days):
        return {"ok": False,
                "error": f"前向窗口未来数据不足: 决策日 {day} 起需 "
                         f"{lookback_days} 个交易日, 已达数据末端",
                "rows": 0}

    from vnpy.trader.constant import Exchange

    targets = _load_selection(day_dir)
    pool_source = "selection"
    if not targets:
        # 2026-09-12: 虚拟盘运行前的历史窗口没有 selection/target_plan 产物, 此前直接
        # 判 FAIL, 导致非重叠滚动样本外验证无法覆盖历史区间(过拟合检测要求 >=4 窗口).
        # 现回退到 PIT 现场选股(selector._select_hist: 行情/财务/估值全部 <= day,
        # 无前视), 使任意历史交易日都能重建当日目标池.
        # 2026-09-13: 改走 _load_pit_selection_cached(按 day 落盘缓存). PBO 参数扫描
        # 需对同一窗口跑多组配置, 而选股占单次回测耗时大头且与 top_n/lookback 无关,
        # 缓存后同窗口第 2..N 组配置仅需纯仿真(~10s)。sel_n 用于取足够深的选股列表。
        _n = max(int(sel_n), int(top_n), 10)
        _hit = os.path.exists(_pit_cache_path(day, _n))
        targets = _load_pit_selection_cached(day, _n)
        if targets:
            pool_source = "onsite_pit"
            _log(f"无当日 selection 产物, 已回退 PIT 现场选股: {len(targets)} 只 "
                 f"(n={_n}, {'缓存命中' if _hit else '现场计算'})")
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
    weights = _make_weights(targets, weight_mode)

    bar_map: dict[str, pd.DataFrame] = {}
    for s6 in symbol6_list:
        df = (_load_bars_forward(s6, day_dt, lookback_days) if forward
              else _load_bars(s6, day_dt, lookback_days))
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
        # [路线图 #9] Regime 场景投影: normal 恒等(倍率 1/延迟 0), stress 按
        # 滑点×4/费×2 放大。slippage_share 显式给出, 使"费"的部分只吃 fee_mult、
        # "滑点"的部分只吃 slippage_mult —— 否则一次放大两块, 归因不出来。
        # `_SCEN` 已在函数入口解析(并为缓存建键), 此处只做投影。
        _SCEN = _sc
        _slip_abs = dyn_slippage + impact
        _slip_share = (_slip_abs / BUY_RATE) if BUY_RATE > 0 else None
        _proj = _RC.project_rates(BUY_RATE, SELL_RATE, _SCEN, slippage_share=_slip_share)
        BUY_RATE, SELL_RATE = _proj["buy_rate"], _proj["sell_rate"]
        # 将动态滑点率写入 summary 供审计追溯
        dynamic_slippage_rate = dyn_slippage
        _log(f"[regime:{_SCEN['name']}] {_RC.describe(_SCEN)} | "
             f"买费率 {BUY_RATE:.6f} 卖费率 {SELL_RATE:.6f} "
             f"(滑点占比 {(_slip_share or 0)*100:.1f}%)")
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
        # [路线图 #9] 延迟成交: 引擎只在 dt 当根 bar 上消费 datetime==dt 的信号
        # (`BacktestingEngine.get_signal` 是 filter(datetime == dt), 且 bar 是逐根
        # 喂入的, 所以**把信号行的日期整体后移 N 根 bar 即等于"决策后 N 根 bar 才
        # 成交"**)。末 N 根的信号被推出窗口 -> 不成交, 这正是延迟的真实代价, 不补。
        _lat = int(_proj.get("latency_bars", 0) or 0)
        _axis = sorted({pd.Timestamp(d) for df in bar_map.values() for d in df["date"]})
        _shift: dict = {}
        for _i, _d in enumerate(_axis):
            _shift[_d] = _axis[_i + _lat] if (_lat and _i + _lat < len(_axis)) else None
        sig_rows = []
        _dropped = 0
        for s6, vt in zip(symbol6_list, vt_symbols):
            if s6 not in bar_map:
                continue
            for d in bar_map[s6]["date"]:
                _ts = pd.Timestamp(d)
                if _lat:
                    _ts = _shift.get(_ts)
                    if _ts is None:      # 后移后落在窗口外 -> 当次决策没有执行日
                        _dropped += 1
                        continue
                sig_rows.append({"datetime": _ts, "symbol": vt, "signal": 1.0})
        if _lat:
            _log(f"[regime:{_SCEN['name']}] 信号延迟 {_lat} 根 bar: "
                 f"窗口末 {_dropped} 条信号无执行 bar, 已如实丢弃(不补做)")
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

        out_dir = _out_dir(day_dir, out_tag)
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
            "weight_mode": weight_mode,
            "stats": stats_dict,
            "curve_points": len(curve),
            "dynamic_slippage": round(dynamic_slippage_rate, 6),
            # [路线图 #9] Regime 场景留痕: 场景名/倍率/延迟/实际费率 一并落盘,
            # 使"这份 summary 是在什么成本假设下跑的"可自证, 不必去翻日志。
            "regime": {
                "scenario": _SCEN.get("name"),
                "label": _SCEN.get("label", ""),
                "slippage_mult": _proj.get("slippage_mult"),
                "fee_mult": _proj.get("fee_mult"),
                "latency_bars": _proj.get("latency_bars"),
                "buy_rate": round(BUY_RATE, 8),
                "sell_rate": round(SELL_RATE, 8),
                "slippage_share": (round(_slip_share, 6)
                                   if _slip_share is not None else None),
                "signals_dropped_by_latency": _dropped if _lat else 0,
            },
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
        # 参数扫描(out_tag 非空)时跳过持久化, 避免把扫描产物写进正式台账
        if persist_arctic:
            _persist_to_arctic(
                day=day,
                bar_map=bar_map,
                strategy=strategy_cls,
                summary=summary,
                engine=eng,
            )
        _log(f"vnpy 回测完成: {len(bar_map)} 标的, {len(all_bars)} bars, "
             f"curve={len(curve)}pts, fallback={summary.get('fallback', False)}")
        # [路线图 #9] 落场景缓存(仅成功路径): 多场景回测下同场景不重复跑。
        # 缓存值自身不可被调用方就地修改 —— 由入口处的 deepcopy 保证。
        _REGIME_CACHE[_ck] = summary
        return summary
    except Exception as e:
        import traceback
        tb = traceback.format_exc(limit=3)
        _log(f"vnpy 回测异常: {type(e).__name__}: {e}\n{tb}")
        # 主链路失败, 直接 fallback
        try:
            fallback = _fallback_backtest(bar_map, symbol6_list, weights, day_dt)
            out_dir = _out_dir(day_dir, out_tag)
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