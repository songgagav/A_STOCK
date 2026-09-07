# -*- coding: utf-8 -*-
"""策略达标检测 (Strategy Validation).

按系统化交易框架验证标准, 对策略的核心指标做"必须全部达标"式检测:

  收益与风险(必过):
    - 夏普比率 Sharpe        ≥ 1.0
    - 索提诺比率 Sortino     ≥ 1.0
    - 卡玛比率 Calmar        ≥ 1.0
    - 年化复合增长 CAGR      ≥ 10%
    - 最大回撤 MaxDD         ≤ 30%
  交易质量(必过):
    - 恢复因子 Recovery      ≥ 2.0   (总收益 / 最大回撤)
    - 盈亏比 Profit Factor   ≥ 1.5   (需逐笔已实现盈亏)
    - 胜率 Win Rate          ≥ 45%   (需逐笔已实现盈亏)
    - 期望值 Expectancy      ≥ 0     (需逐笔已实现盈亏)
  信息性(辅助参考, 不判过):
    - 样本外留存率 WFE       ≥ 50%
    - 表现一致性 CV          ≤ 20%

数据源适配:
  - vnpy 主链路回测摘要 (data/vnpy_backtest/*/summary.json): 统计量直接映射,
    缺下行偏差时 Sortino 标"缺数据"; 无逐笔时交易类部分指标标"缺数据".
  - 净值曲线 (data/backtest/*/result.json 的 curve / 实盘纸面回执): 由净值
    曲线全量计算权益类指标.
"""

from __future__ import annotations

import glob
import json
import os

import numpy as np

_BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_BASE, "data")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
BACKTEST_DIR = os.path.join(DATA_DIR, "backtest")
VNPY_DIR = os.path.join(DATA_DIR, "vnpy_backtest")

TRADING_DAYS = 252.0

# ------------------------------------------------------------------
# 阈值表
# ------------------------------------------------------------------
# direction: "gte" = 大于等于通过, "lte" = 小于等于通过
THRESHOLDS = {
    # 收益与风险 (必过)
    "sharpe":         {"limit": 1.0,  "direction": "gte", "label": "夏普比率"},
    "sortino":        {"limit": 1.0,  "direction": "gte", "label": "索提诺比率"},
    "calmar":         {"limit": 1.0,  "direction": "gte", "label": "卡玛比率"},
    "cagr":           {"limit": 0.10, "direction": "gte", "label": "年化复合增长"},
    "max_drawdown":   {"limit": 0.30, "direction": "lte", "label": "最大回撤"},
    # 交易质量 (必过)
    "recovery":       {"limit": 2.0,  "direction": "gte", "label": "恢复因子"},
    "profit_factor":  {"limit": 1.5,  "direction": "gte", "label": "盈亏比"},
    "win_rate":       {"limit": 0.45, "direction": "gte", "label": "胜率"},
    "expectancy":     {"limit": 0.0,  "direction": "gte", "label": "期望值"},
    # 信息性 (辅助)
    "wfe":            {"limit": 0.50, "direction": "gte", "label": "样本外留存率", "aux": True},
    "cv":             {"limit": 0.20, "direction": "lte", "label": "表现一致性", "aux": True},
}

REQUIRED_METRICS = [k for k, v in THRESHOLDS.items() if not v.get("aux")]


def _round(x: float | None, nd: int = 4):
    if x is None:
        return None
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------
# 净值曲线 -> 指标
# ------------------------------------------------------------------
def _as_equity(equity) -> np.ndarray:
    a = np.asarray([float(x) for x in equity], dtype=np.float64)
    a = a[np.isfinite(a) & (a > 0)]
    return a


def _years_from_dates(dates) -> float | None:
    try:
        import datetime as _dt
        fmt = "%Y-%m-%d" if "-" in str(dates[0]) else "%Y%m%d"
        d0 = _dt.datetime.strptime(str(dates[0])[:10].replace("/", "-"), fmt)
        d1 = _dt.datetime.strptime(str(dates[-1])[:10].replace("/", "-"), fmt)
        y = (d1 - d0).days / 365.25
        return y if y > 0 else None
    except Exception:
        return None


def curve_metrics(
    equity,
    dates=None,
    start_equity: float | None = None,
    n_annual: float = TRADING_DAYS,
) -> dict:
    """由净值曲线计算权益类指标.

    Args:
        equity: 净值序列 (升序). 首点建议 = 初始资金/首个结算净值.
        dates:  可选日期序列 (与 equity 等长), 用于按自然日年化 CAGR.
        start_equity: 若首点非初始资金, 用该值做总收益/回撤分母基准.
        n_annual: 年化交易日数.

    Returns:
        dict: {cagr, sharpe, sortino, calmar, max_drawdown, recovery,
               total_return, wfe, cv, n_points, ...} (None 表示无法计算).
    """
    eq = _as_equity(equity)
    if len(eq) < 3:
        return {"n_points": len(eq)}
    base = float(start_equity) if (start_equity and start_equity > 0) else float(eq[0])

    r = np.diff(eq) / eq[:-1]                     # 日收益
    r = r[np.isfinite(r)]
    total_return = float(eq[-1] / base - 1.0)

    # 年化
    years = _years_from_dates(dates) if dates else None
    if years and years > 0:
        cagr = float((1.0 + total_return) ** (1.0 / years) - 1.0) if total_return > -1 else None
    elif len(r) >= 2:
        n = len(r)
        cagr = float((1.0 + total_return) ** (n_annual / n) - 1.0) if total_return > -1 else None
    else:
        cagr = None

    # 波动与下行偏差
    std = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    downside = float(np.sqrt(np.mean(np.clip(r, None, 0.0) ** 2)))
    mean_r = float(r.mean())

    sharpe = (mean_r / std * np.sqrt(n_annual)) if std > 1e-12 else None
    sortino = (mean_r / downside * np.sqrt(n_annual)) if downside > 1e-12 else (
        None if mean_r <= 0 else None)

    # 最大回撤 (基于基准 base 的相对净值)
    nav = eq / base
    peak = np.maximum.accumulate(nav)
    max_dd = float(np.max((peak - nav) / peak)) if len(nav) else 0.0

    calmar = (cagr / max_dd) if (cagr is not None and max_dd > 1e-12) else None
    recovery = (total_return / max_dd) if max_dd > 1e-12 else None

    # ---- 辅助: WFE (前 50% 样本内 vs 后 50% 样本外) ----
    wfe = None
    if len(eq) >= 8:
        mid = len(eq) // 2
        seg_a, seg_b = eq[:mid + 1], eq[mid:]
        n_a, n_b = max(len(seg_a) - 1, 1), max(len(seg_b) - 1, 1)
        ra = float(seg_a[-1] / seg_a[0] - 1.0)
        rb = float(seg_b[-1] / seg_b[0] - 1.0)
        cagr_a = (1.0 + ra) ** (n_annual / n_a) - 1.0 if ra > -1 else 0.0
        cagr_b = (1.0 + rb) ** (n_annual / n_b) - 1.0 if rb > -1 else 0.0
        if cagr_a > 1e-9:
            wfe = cagr_b / cagr_a
        elif cagr_b > 0:
            wfe = None  # 样本内不盈利, 样本外留存无从比较

    # ---- 辅助: CV (等段 CAGR 变异系数) ----
    cv = None
    n_seg = max(2, len(eq) // 20)
    if len(eq) >= n_seg * 3 and n_seg >= 2:
        edges = np.linspace(0, len(eq), n_seg + 1).astype(int)
        seg_cagr = []
        for i in range(n_seg):
            a, b = eq[edges[i]], eq[edges[i + 1] - 1]
            if a > 0 and b > 0 and (edges[i + 1] - 1) > edges[i]:
                nn = (edges[i + 1] - 1) - edges[i]
                sr = float(b / a - 1.0)
                seg_cagr.append((1.0 + sr) ** (n_annual / nn) - 1.0 if sr > -1 else 0.0)
        seg_cagr = np.asarray(seg_cagr)
        seg_cagr = seg_cagr[np.isfinite(seg_cagr)]
        if len(seg_cagr) >= 2 and abs(seg_cagr.mean()) > 1e-9:
            cv = float(seg_cagr.std(ddof=1) / abs(seg_cagr.mean()))

    return {
        "n_points": int(len(eq)),
        "total_return": _round(total_return, 6),
        "cagr": _round(cagr, 6),
        "sharpe": _round(sharpe, 4),
        "sortino": _round(sortino, 4),
        "max_drawdown": _round(max_dd, 6),
        "calmar": _round(calmar, 4),
        "recovery": _round(recovery, 4),
        "wfe": _round(wfe, 4),
        "cv": _round(cv, 4),
    }


# ------------------------------------------------------------------
# 逐笔已实现盈亏 -> 交易质量指标
# ------------------------------------------------------------------
def trade_metrics(pnls) -> dict:
    """由逐笔已实现盈亏 (元) 计算交易质量指标.

    Returns:
        dict: {n_trades, win_rate, profit_factor, expectancy,
               gross_profit, gross_loss}. 样本不足时相关字段 None.
    """
    arr = np.asarray([float(x) for x in pnls], dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return {"n_trades": 0, "win_rate": None, "profit_factor": None,
                "expectancy": None, "gross_profit": 0.0, "gross_loss": 0.0}
    gross_profit = float(arr[arr > 0].sum())
    gross_loss = float(-arr[arr < 0].sum())
    win_rate = float((arr > 0).mean())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-9 else (
        None if gross_profit <= 0 else 999.0)
    return {
        "n_trades": int(len(arr)),
        "win_rate": _round(win_rate, 4),
        "profit_factor": _round(profit_factor, 4),
        "expectancy": _round(float(arr.mean()), 2),
        "gross_profit": _round(gross_profit, 2),
        "gross_loss": _round(gross_loss, 2),
    }


# ------------------------------------------------------------------
# 达标判定
# ------------------------------------------------------------------
def evaluate(metrics: dict) -> dict:
    """对指标字典做达标判定.

    Args:
        metrics: 形如 {metric_key: value_or_None, ...}.

    Returns:
        dict: {
            "rows": [{key,label,value,limit,direction,required,aux,status,source}],
            "missing_required": [key,...],
            "failed_required": [key,...],
            "passed_required": [key,...],
            "verdict": "PASS" | "FAIL" | "INSUFFICIENT_DATA",
        }
    """
    rows = []
    missing, failed, passed = [], [], []
    for key, cfg in THRESHOLDS.items():
        val = metrics.get(key)
        source = metrics.get("_meta", {}).get("source", "")
        row = {
            "key": key, "label": cfg["label"],
            "value": _round(val, 4) if val is not None else None,
            "limit": cfg["limit"], "direction": cfg["direction"],
            "required": not cfg.get("aux"), "aux": bool(cfg.get("aux")),
            "status": "missing", "source": source,
        }
        if val is None:
            row["status"] = "missing"
            if row["required"]:
                missing.append(key)
        else:
            ok = (val >= cfg["limit"]) if cfg["direction"] == "gte" else (val <= cfg["limit"])
            row["status"] = "pass" if ok else "fail"
            if row["required"]:
                (passed if ok else failed).append(key)
        rows.append(row)

    if missing:
        verdict = "INSUFFICIENT_DATA"
    elif failed:
        verdict = "FAIL"
    else:
        verdict = "PASS"
    return {
        "rows": rows,
        "missing_required": missing,
        "failed_required": failed,
        "passed_required": passed,
        "verdict": verdict,
    }


# ------------------------------------------------------------------
# 数据源适配
# ------------------------------------------------------------------
def metrics_from_vnpy_summary(summ: dict) -> dict:
    """从 vnpy 主链路回测 summary.json 提取可用指标.

    注: 摘要不含逐笔/下行偏差 -> sortino 与部分交易类指标标缺数据.
    """
    s = summ.get("stats") or {}
    f = lambda k: float(s[k]) if k in s and s[k] is not None else None  # noqa: E731

    annual_return = f("annual_return")          # 百分数 (如 4.77)
    max_dd_pct = f("max_ddpercent")             # 负数百分数
    net_pnl = f("total_net_pnl")
    max_dd_money = f("max_drawdown")            # 负数金额
    capital = f("capital") or 100000.0

    max_dd = (abs(max_dd_pct) / 100.0) if max_dd_pct is not None else None
    cagr = (annual_return / 100.0) if annual_return is not None else None
    # 2026-09-07: summary 现已写 calmar(精确) 与 sortino_ratio(_approx 等权链路近似)
    calmar_stat = f("calmar")
    calmar = calmar_stat if calmar_stat is not None else (
        (cagr / max_dd) if (cagr is not None and max_dd and max_dd > 1e-9) else None)
    recovery = None
    if net_pnl is not None and max_dd_money and abs(max_dd_money) > 1e-9:
        recovery = float(net_pnl / abs(max_dd_money))

    sortino = f("sortino_ratio")
    sortino_approx = f("sortino_ratio_approx")
    sortino_val = sortino if sortino is not None else sortino_approx
    _approx = (sortino is None and sortino_approx is not None)

    out = {
        "cagr": _round(cagr, 6),
        "sharpe": _round(f("sharpe_ratio"), 4),
        "sortino": _round(sortino_val, 4),
        "max_drawdown": _round(max_dd, 6),
        "calmar": _round(calmar, 4),
        "recovery": _round(recovery, 4),
        # 交易类: 无逐笔数据
        "profit_factor": None, "win_rate": None,
    }
    # 期望值代理: 净盈亏 / 成交笔数 (整段), 仅作参考口径
    n_trades = None
    try:
        n_trades = int(float(s["total_trade_count"]))
    except Exception:
        pass
    if n_trades and net_pnl is not None:
        out["expectancy"] = _round(net_pnl / n_trades, 2)
        out["_meta"] = {
            "source": f"vnpy summary (net_pnl/{n_trades}笔 代理期望值)",
            "n_trades": n_trades,
            "sortino_approx": bool(_approx),
            "note": "盈亏比/胜率需逐笔成交, 当前摘要缺数据",
        }
    else:
        out["expectancy"] = None
        out["_meta"] = {"source": "vnpy summary", "sortino_approx": bool(_approx)}
    return out


def metrics_from_curve(curve: list, start_equity: float | None = None) -> dict:
    """从 [{day, equity}...] 或数值序列净值曲线计算指标."""
    if curve and isinstance(curve[0], dict):
        equity = [c.get("equity") for c in curve]
        dates = [c.get("day") or c.get("date") for c in curve]
        eq_ok = _as_equity(equity)
        if len(eq_ok) != len(equity):          # 混入空值时逐点重建
            equity, dates = [], []
            for c in curve:
                e = c.get("equity")
                if e is not None and np.isfinite(float(e)) and float(e) > 0:
                    equity.append(float(e))
                    dates.append(c.get("day") or c.get("date"))
        dates = dates if len(dates) == len(equity) else None
        m = curve_metrics(equity, dates=dates, start_equity=start_equity)
    else:
        m = curve_metrics(curve, start_equity=start_equity)
    m["_meta"] = {"source": "equity curve", "n_points": m.get("n_points")}
    return m


# ------------------------------------------------------------------
# 便捷: 对一组指标做完整检测报告
# ------------------------------------------------------------------
def run_detection(metrics: dict, meta: dict | None = None) -> dict:
    """组装单一数据源的完整检测结果."""
    mm = dict(metrics)
    mm["_meta"] = dict(meta or {})
    ev = evaluate(mm)
    return {"ok": True, "meta": meta or {}, "metrics": {
        k: v for k, v in mm.items() if not k.startswith("_")
    }, **ev}


# ------------------------------------------------------------------
# 数据加载 (懒导入)
# ------------------------------------------------------------------
def latest_vnpy_summary() -> dict | None:
    """取最新的 vnpy 主链路回测 summary (按目录名排序)."""
    dirs = sorted(glob.glob(os.path.join(VNPY_DIR, "*")), reverse=True)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        sp = os.path.join(d, "summary.json")
        if os.path.exists(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("ok") and data.get("stats"):
                    return {"day": os.path.basename(d), "summary": data}
            except Exception:
                continue
    return None


def load_curve_json(path: str) -> list | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    curve = data.get("curve")
    return curve if isinstance(curve, list) else None


def load_paper_equities() -> dict | None:
    """实盘纸面净值序列: data/daily/*/daily_summary.json -> steps.paper.equity."""
    try:
        from performance_report import load_daily_equities
        rows = load_daily_equities()
    except Exception:
        rows = []
    if not rows:
        return None
    equity = [r["equity"] for r in rows]
    eq = _as_equity(equity)
    if len(eq) < 2:
        return None
    init = None
    for r in rows:
        if r.get("init_capital") and r["init_capital"] > 0:
            init = float(r["init_capital"])
            break
    return {"equity": [float(v) for v in eq], "dates": [r["day"] for r in rows if np.isfinite(float(r["equity"])) and float(r["equity"]) > 0], "init": init}


def load_paper_after_gate() -> dict | None:
    """带门控净值账本 (data/paper_after_gate.json, 由 refresh_gate_ic 盘后追加)."""
    p = os.path.join(DATA_DIR, "paper_after_gate.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        rows = d.get("rows") or []
        if len(rows) < 2:
            return None
        return d
    except Exception:
        return None


# ------------------------------------------------------------------
# 检测入口
# ------------------------------------------------------------------
def detect_source(source: str = "vnpy") -> dict:
    """按数据源执行检测.

    source:
      - "vnpy":            最新 vnpy 主链路回测摘要 (缺数据的指标如实标注)
      - "backtest_latest": data/backtest_latest.json (m4_smoke_f)
      - "cost_after_fee1": data/backtest/cost_after_fee1/result.json
      - "paper":          实盘纸面净值回执
      - "paper_after_gate": 带门控纸面净值账本 (路径A, 需≥21交易日样本)
      - "all":            依次执行以上全部
    """
    if source == "all":
        out = {"ok": True, "results": {}}
        for s in ["vnpy", "backtest_latest", "cost_after_fee1",
                  "paper", "paper_after_gate"]:
            r = detect_source(s)
            out["results"][s] = r
        return out

    if source == "vnpy":
        hit = latest_vnpy_summary()
        if not hit:
            return {"ok": False, "error": "无 vnpy summary 数据"}
        m = metrics_from_vnpy_summary(hit["summary"])
        return run_detection(m, {
            "source": "vnpy 主链路回测", "tag": hit["day"],
            "sample": f"{hit['summary']['stats'].get('total_days')} 个交易日",
        })

    if source == "paper_after_gate":
        hit = load_paper_after_gate()
        if not hit:
            return {"ok": False,
                    "error": "paper_after_gate 账本不存在或样本<2 (需盘后运行 refresh_gate_ic.py)"}
        rows = hit.get("rows") or []
        curve = [{"day": r.get("date"), "equity": r.get("equity")} for r in rows]
        m = metrics_from_curve(curve)
        n_act = int(sum(1 for r in rows if r.get("gate_active")))
        return run_detection(m, {
            "source": "带门控纸面净值 (paper_after_gate)",
            "tag": f"{hit.get('start')}..{hit.get('end')}",
            "sample": f"{len(rows)} 个交易日 (gate_active={n_act})",
            "note": "路径A: 门控生效样本; 未满 21 交易日时结论仅供参考",
        })

    path_map = {
        "backtest_latest": os.path.join(DATA_DIR, "backtest_latest.json"),
        "cost_after_fee1": os.path.join(BACKTEST_DIR, "cost_after_fee1", "result.json"),
        "paper": None,
    }
    if source == "paper":
        hit = load_paper_equities()
        if not hit:
            return {"ok": False, "error": "无实盘纸面净值数据"}
        m = metrics_from_curve(
            [{"day": d, "equity": e} for d, e in zip(hit["dates"], hit["equity"])],
            start_equity=hit.get("init"))
        tag = f"{hit['dates'][0]}..{hit['dates'][-1]}"
        return run_detection(m, {
            "source": "实盘纸面净值", "tag": tag,
            "sample": f"{len(hit['equity'])} 个交易日",
        })

    path = path_map.get(source)
    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"数据源不存在: {source}"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    curve = data.get("curve")
    if not curve:
        return {"ok": False, "error": f"{source} 无 curve"}
    m = metrics_from_curve(curve, start_equity=data.get("init_capital"))
    return run_detection(m, {
        "source": "回测净值曲线", "tag": data.get("tag") or source,
        "sample": f"{data.get('trade_days') or len(curve)} 个交易日",
    })


def save_report(result: dict, path: str | None = None) -> str:
    path = path or os.path.join(DATA_DIR, "strategy_validation_latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return path


def _fmt_status(s: str) -> str:
    return {"pass": "✅通过", "fail": "❌未达标", "missing": "—缺数据"}.get(s, s)


def _print_result(res: dict) -> None:
    if not res.get("ok"):
        print(f"检测失败: {res.get('error')}")
        return
    meta = res.get("meta") or {}
    print(f"\n=== 策略达标检测 [{meta.get('source')} {meta.get('tag') or ''}]"
          f" 样本={meta.get('sample')} ===")
    for row in res.get("rows", []):
        val = row["value"]
        vstr = "—" if val is None else (f"{val * 100:.2f}%" if row["key"] in ("cagr", "max_drawdown", "win_rate", "wfe", "cv") else (f"{val:,.2f}元" if row["key"] == "expectancy" else f"{val:.4f}"))
        lstr = (f"{row['limit'] * 100:.0f}%" if row["key"] in ("cagr", "max_drawdown", "win_rate", "wfe", "cv") else (f"≥0元" if row["key"] == "expectancy" else f"{row['direction']} {row['limit']}"))
        aux = " (辅助)" if row.get("aux") else ""
        print(f"  [{_fmt_status(row['status'])}] {row['label']}{aux:<4} = {vstr:>12}  阈值: {lstr}")
    print(f"  结论: {res.get('verdict')} | 通过={len(res.get('passed_required', []))} 未达标={len(res.get('failed_required', []))} 缺数据={len(res.get('missing_required', []))}")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="策略达标检测 (Strategy Validation)")
    ap.add_argument("--source", default="all",
                    choices=["vnpy", "backtest_latest", "cost_after_fee1",
                             "paper", "paper_after_gate", "all"])
    ap.add_argument("--save", action="store_true", help="结果写入 data/strategy_validation_latest.json")
    args = ap.parse_args()

    res = detect_source(args.source)
    if args.source == "all":
        for name, r in (res.get("results") or {}).items():
            _print_result(r)
        if args.save:
            p = save_report(res)
            print(f"\n结果已保存: {p}")
    else:
        _print_result(res)
        if args.save:
            p = save_report(res)
            print(f"\n结果已保存: {p}")


if __name__ == "__main__":
    main()
