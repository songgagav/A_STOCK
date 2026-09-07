# -*- coding: utf-8 -*-
"""归因分析 (日频数据) — 收益时段分解 / 融合因子滚动 IC / DRL 仓位行为审计.

针对 121 日主链路窗口 (2026-03-12 .. 2026-09-04):

1. 收益时段分解 (segment attribution):
   输入逐日净值 -> 输出单日/连续时段 Top3 盈与亏、盈利日占比、
   Top5 盈利日对总盈利的贡献集中度 (检验"盈利是否集中在少数几天").

2. 融合因子失效检测 (factor IC):
   复用 factor_fusion.score_series_hist 对窗口逐日重放融合因子
   (pb_inv + ep + ocf_ps + roe_yy_chg反转, ICIR 加权) 的截面 z,
   与 fwd1/fwd5 计算逐日 RankIC; 再做 20 日滚动均值/ICIR/负占比,
   输出整窗 / 前中后三段 / 最新 20 日诊断 (持续走低或转负 = 失效信号).

3. DRL 行为审计 (behavior audit):
   - 实盘纸面回执: 日均仓位占比、持仓只数、仓位波动 (换手/持仓周期需成交明细,
     能算则按  |ΔMV|/(2×equity) 近似).
   - vnpy 121 日主链路摘要: 权重结构/成交笔数/换手总量 -> 平均持仓周期与活动度代理.

输出: data/factor_mine/attribution_121d.json + CLI 摘要.
"""

from __future__ import annotations

import glob
import json
import os
import time

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_BASE, "data")
FUSION_IC_CACHE = os.path.join(DATA_DIR, "factor_mine", "fusion_ic_121d.json")
OUT_JSON = os.path.join(DATA_DIR, "factor_mine", "attribution_121d.json")

FUSION_FACTOR_DESC = "pb_inv + ep + ocf_ps + roe_yy_chg(反转) 融合(ICIR加权, 行业+市值中性化)"


# ------------------------------------------------------------------
# 通用工具
# ------------------------------------------------------------------
def _r(x, nd=4):
    if x is None:
        return None
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _spearman(x: list, y: list) -> float | None:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 10 or np.unique(x).size < 2 or np.unique(y).size < 2:
        return None

    def _rank(a):
        order = a.argsort()
        ranks = np.empty_like(order, dtype=float)
        ranks[order] = np.arange(1, len(a) + 1)
        # 平局取均值
        s = np.sort(a)
        i = 0
        while i < len(a):
            j = i
            while j + 1 < len(a) and s[j + 1] == s[i]:
                j += 1
            if j > i:
                ranks[a == s[i]] = (i + 1 + j + 1) / 2.0
            i = j + 1
        return ranks

    rx, ry = _rank(x), _rank(y)
    rx_, ry_ = rx - rx.mean(), ry - ry.mean()
    denom = np.sqrt((rx_ ** 2).sum() * (ry_ ** 2).sum())
    if denom < 1e-12:
        return None
    rho = float((rx_ * ry_).sum() / denom)
    return rho if np.isfinite(rho) else None


# ------------------------------------------------------------------
# 1. 收益时段分解
# ------------------------------------------------------------------
def daily_returns_from_equity(records: list) -> tuple[list, list]:
    """records: [{day, equity}...] 或纯数值序列. 返回 (days, daily_pct)."""
    days, eq = [], []
    for r in records:
        if isinstance(r, dict):
            e = r.get("equity")
            if e is None or not np.isfinite(float(e)) or float(e) <= 0:
                continue
            days.append(str(r.get("day") or r.get("date") or ""))
            eq.append(float(e))
        else:
            e = float(r)
            if np.isfinite(e) and e > 0:
                days.append("")
                eq.append(e)
    if len(eq) < 2:
        return [], []
    eq = np.asarray(eq)
    pct = np.diff(eq) / eq[:-1]
    return days[1:], pct


def segment_report(records: list, top_n: int = 3) -> dict:
    """逐日净值 -> 时段归因报告.

    Returns:
        dict: {ok, n_days, total_return, pos_day_share, top_gain_days,
               top_loss_days, concentration, best_worst_runs, note}
    """
    days, pct = daily_returns_from_equity(records)
    if len(pct) < 2:
        return {"ok": False, "error": "净值点不足(<2)"}
    eq0, eq1 = records[0]["equity"] if isinstance(records[0], dict) else records[0], \
        records[-1]["equity"] if isinstance(records[-1], dict) else records[-1]
    total = float(eq1 / eq0 - 1.0)

    pairs = sorted(zip(days, pct.tolist()), key=lambda kv: (kv[1] is None, kv[1]))
    gains = [p for p in pairs if p[1] > 0]
    losses = [p for p in pairs if p[1] < 0]
    top_gain = [{"day": d, "pnl_pct": _r(v * 100, 3)} for d, v in
                sorted(pairs, key=lambda kv: kv[1], reverse=True)[:top_n]]
    top_loss = [{"day": d, "pnl_pct": _r(v * 100, 3)} for d, v in
                sorted(pairs, key=lambda kv: kv[1])[:top_n]]

    total_gain = float(sum(v for _, v in gains))
    top5 = sorted(gains, key=lambda kv: kv[1], reverse=True)[:5]
    top5_share = (float(sum(v for _, v in top5)) / total_gain) if total_gain > 0 else None

    # 连续最好/最差 5 日滑动段 (净值 5 日累计收益最大/最小)
    def _run2(records, k=5, best=True):
        eqs = []
        for r in records:
            e = r.get("equity") if isinstance(r, dict) else r
            if e and np.isfinite(float(e)) and float(e) > 0:
                eqs.append(float(e))
        if len(eqs) <= k:
            return None
        rets = np.diff(eqs) / eqs[:-1]
        found = None
        for i in range(0, len(rets) - k + 1):
            s = float(rets[i:i + k].sum())
            if found is None or (s > found[0] if best else s < found[0]):
                found = (s, i)
        if not found:
            return None
        s, i = found
        d0 = days[i] if days and i < len(days) else ""
        d1 = days[min(i + k - 1, len(days) - 1)] if days else ""
        return {"type": "best" if best else "worst", "start": d0, "end": d1,
                "days": k, "pnl_pct": _r(s * 100, 3)}
    best_run = _run2(records, 5, True)
    worst_run = _run2(records, 5, False)

    return {
        "ok": True,
        "n_days": int(len(pct)),
        "total_return_pct": _r(total * 100, 3),
        "pos_day_share": _r(len(gains) / len(pct), 4),
        "top_gain_days": top_gain,
        "top_loss_days": top_loss,
        "top5_gain_days_share": _r(top5_share, 4),
        "best_5d_run": best_run,
        "worst_5d_run": worst_run,
        "note": "盈利集中度: top5_gain_days_share 越接近 1, 盈利越集中在少数几天",
    }


# ------------------------------------------------------------------
# 2. 融合因子滚动 IC
# ------------------------------------------------------------------
def daily_ic_from_samples(samples: list) -> list:
    """由 score_series_hist samples 计算逐日 RankIC (fwd1/fwd5)."""
    out = []
    for s in samples:
        sc = s.get("scores") or {}
        if not sc:
            continue
        d = {"date": s.get("date"), "pool_n": s.get("n_pool", 0),
             "scored_n": len(sc)}
        for h in ("fwd1", "fwd5"):
            rets = s.get(h) or {}
            sym = [k for k in sc if k in rets]
            if len(sym) < 30:
                d[h + "_ic"] = None
            else:
                d[h + "_ic"] = _spearman([sc[k] for k in sym],
                                         [rets[k] for k in sym], )
        out.append(d)
    return out


def _mean_ic(x: list) -> float | None:
    a = [v for v in x if v is not None and np.isfinite(v)]
    return float(np.mean(a)) if a else None


def rolling_ic_stats(ic_series: list, window: int = 20, min_pts: int = 5) -> dict:
    """逐日 IC 序列 -> 整窗/三段/最新滚动 20 日诊断.

    Args:
        ic_series: [{date, fwd5_ic, fwd1_ic}...] (按日期升序)
    """
    dates = [d["date"] for d in ic_series]
    x5 = np.array([(d.get("fwd5_ic") if d.get("fwd5_ic") is not None else np.nan)
                   for d in ic_series], dtype=float)
    ok = np.isfinite(x5)
    vals = x5[ok]
    dts = [dates[i] for i in range(len(dates)) if ok[i]]

    def _stats(arr):
        if len(arr) < min_pts:
            return None
        a = np.asarray(arr, dtype=float)
        m = float(a.mean())
        s = float(a.std(ddof=1))
        return {"mean": _r(m, 5), "median": _r(float(np.median(a)), 5),
                "std": _r(s, 5),
                "icir": _r((m / s * np.sqrt(252.0)) if s > 1e-12 else None, 4),
                "neg_share": _r(float((a < 0).mean()), 4),
                "n": int(len(a))}

    overall = _stats(vals)
    thirds = {}
    if len(vals) >= 3 * min_pts:
        n = len(vals)
        labs = ["early", "middle", "late"]
        for i, lab in enumerate(labs):
            lo, hi = n * i // 3, n * (i + 1) // 3
            thirds[lab] = _stats(vals[lo:hi]) if hi - lo >= min_pts else None

    # 最新 window 滚动
    recent = _stats(vals[-window:]) if len(vals) >= min_pts else None
    last = None
    if len(dts):
        last = {"date": dts[-1], "fwd5_ic": _r(float(vals[-1]), 5),
                "window20_mean": recent["mean"] if recent else None}

    # 失效诊断: 三段均值下行 或 最新20日均值为负 或 负占比>60%
    diag = []
    if thirds.get("early") and thirds.get("late"):
        e_mean, l_mean = thirds["early"]["mean"], thirds["late"]["mean"]
        if l_mean is not None and e_mean is not None and l_mean < e_mean - 0.01:
            diag.append(f"IC 三段下行: 前期 {e_mean:.4f} -> 后期 {l_mean:.4f}")
    if recent and recent["mean"] is not None and recent["mean"] < 0:
        diag.append(f"最新 {window} 日均 IC 为负 ({recent['mean']:.4f})")
    if recent and recent["neg_share"] is not None and recent["neg_share"] > 0.6:
        diag.append(f"最新 {window} 日负 IC 占比 {recent['neg_share']:.0%} > 60%")

    return {
        "window": window,
        "n_days": int(len(vals)),
        "overall": overall,
        "thirds": thirds,
        "recent": recent,
        "last": last,
        "failure_signals": diag,
        "series": [{"date": d, "fwd5_ic": _r(v, 5) if np.isfinite(v) else None}
                   for d, v in zip(dts, vals)],
    }


# ------------------------------------------------------------------
# 3. DRL 行为审计
# ------------------------------------------------------------------
def audit_exposure(rows: list) -> dict:
    """由逐日回执 (day/equity/cash/positions) 计算仓位行为.

    Returns: dict {ok, n_days, avg_exposure, avg_n_positions,
                   exposure_min/max, turnover_approx_5d(缺成交明细时估算), note}
    """
    recs = []
    for r in rows:
        eq = r.get("equity")
        cash = r.get("cash")
        if eq is None or cash is None or not np.isfinite(float(eq)) or float(eq) <= 0:
            continue
        mv = float(eq) - float(cash)
        pos = r.get("positions") or {}
        recs.append({"day": r.get("day"), "exposure": float(np.clip(mv / float(eq), 0, 1)),
                     "n_pos": len(pos)})
    if len(recs) < 2:
        return {"ok": False, "error": "回执不足"}

    ex = np.array([x["exposure"] for x in recs])
    np_ = np.array([x["n_pos"] for x in recs])
    # 换手率近似: 5 日滚动 |Δ仓位|/(2) 平均, 说明仓位的变动脉络 (非真实成交换手)
    d_ex = np.abs(np.diff(ex))
    turnover_approx = float(np.mean(d_ex) * 0.5) if len(d_ex) else None
    holding_days = (1.0 / turnover_approx) if (turnover_approx and turnover_approx > 1e-6) else None

    return {
        "ok": True,
        "n_days": int(len(recs)),
        "avg_exposure": _r(float(ex.mean()), 4),
        "exposure_min": _r(float(ex.min()), 4),
        "exposure_max": _r(float(ex.max()), 4),
        "avg_n_positions": _r(float(np_.mean()), 2),
        "turnover_approx_daily": _r(turnover_approx, 5),
        "holding_days_approx": _r(holding_days, 1),
        "note": "exposure=(equity-cash)/equity; turnover 为仓位变动代理, 真实换手需逐笔成交",
    }


def audit_vnpy_summary(summ: dict) -> dict:
    """vnpy 121 日主链路摘要 -> 活动度代理."""
    s = summ.get("stats") or {}

    def f(k):
        try:
            return float(s[k])
        except Exception:
            return None

    weights = summ.get("weights") or []
    n_sym = len(weights)
    wsum = float(sum(weights)) if weights else 0.0
    total_days = None
    try:
        total_days = int(float(s["total_days"]))
    except Exception:
        pass
    n_trades = None
    try:
        n_trades = int(float(s["total_trade_count"]))
    except Exception:
        pass
    turnover = f("total_turnover")
    capital = f("capital") or 100000.0
    rounds = (turnover / capital) if (turnover and capital > 0) else None
    holding = None
    if total_days and rounds and rounds > 1e-9:
        holding = (total_days * (wsum / n_sym) / rounds) if (wsum and n_sym) else total_days / rounds
    return {
        "ok": True,
        "n_symbols": n_sym,
        "weights": [_r(w, 4) for w in weights][:8],
        "weight_sum": _r(wsum, 4),
        "total_days": total_days,
        "total_trades": n_trades,
        "avg_days_between_trades": _r((total_days / n_trades) if (total_days and n_trades) else None, 1),
        "turnover_rounds_total": _r(rounds, 4),
        "turnover_per_day_pct": _r((rounds / total_days * 100) if (rounds and total_days) else None, 4),
        "avg_holding_days_approx": _r(holding, 1),
        "note": "等权持仓近似; 平均持仓周期为换手总量反推, 需逐笔确认",
    }


# ------------------------------------------------------------------
# 数据装载
# ------------------------------------------------------------------
def load_vnpy_summary() -> dict | None:
    dirs = sorted(glob.glob(os.path.join(DATA_DIR, "vnpy_backtest", "*")), reverse=True)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        sp = os.path.join(d, "summary.json")
        if os.path.exists(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("ok") and data.get("stats"):
                    return data
            except Exception:
                continue
    return None


def load_paper_rows():
    try:
        from performance_report import load_daily_equities
        return load_daily_equities()
    except Exception:
        return []


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def compute_fusion_ic(end: str = "2026-09-04", days: int = 121, refresh: bool = False) -> dict:
    """121 日融合因子逐日 IC (缓存到 data/factor_mine/fusion_ic_121d.json)."""
    if os.path.exists(FUSION_IC_CACHE) and not refresh:
        with open(FUSION_IC_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("as_of_end", "")[:10] == end[:10] and cached.get("n_days", 0) >= days - 3:
            return cached
    import factor_fusion as ff
    t0 = time.time()
    raw = ff.score_series_hist(end=end, days=days)
    if "error" in raw or not raw.get("samples"):
        return {"ok": False, "error": raw.get("error", "无样本")}
    ic = daily_ic_from_samples(raw["samples"])
    result = {
        "ok": True,
        "as_of_start": raw["as_of_start"], "as_of_end": raw["as_of_end"],
        "n_days": raw["n_days"], "n_with_ic": len(ic),
        "elapsed_s": round(time.time() - t0, 1),
        "factor": FUSION_FACTOR_DESC,
        "daily_ic": ic,
    }
    os.makedirs(os.path.dirname(FUSION_IC_CACHE), exist_ok=True)
    with open(FUSION_IC_CACHE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return result


def run_all(refresh_ic: bool = False) -> dict:
    res = {"factor": FUSION_FACTOR_DESC}

    # A. 融合 IC 121 日
    ic_data = compute_fusion_ic(refresh=refresh_ic)
    res["fusion_ic_121d"] = ic_data
    if ic_data.get("ok"):
        stats = rolling_ic_stats(ic_data.get("daily_ic", []), window=20)
        res["fusion_ic_rolling"] = stats

    # B. 时段分解 (可用净值曲线)
    segs = {}
    paper = load_paper_rows()
    if paper:
        segs["paper_receipts"] = segment_report(
            [{"day": r["day"], "equity": r["equity"]} for r in paper])
    import glob as _g
    for tag, rel in [("backtest_latest", "backtest_latest.json"),
                     ("cost_after_fee1", os.path.join("backtest", "cost_after_fee1", "result.json"))]:
        p = os.path.join(DATA_DIR, rel)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("curve"):
            segs[tag] = segment_report(d["curve"])
    res["segment_attribution"] = segs

    # C. DRL 行为审计
    beh = {}
    summ = load_vnpy_summary()
    if summ:
        beh["vnpy_121d"] = audit_vnpy_summary(summ)
    beh["paper_receipts"] = audit_exposure(paper)
    res["behavior_audit"] = beh
    return res


def _fmt_ic(stats: dict) -> str:
    if not stats:
        return "无 IC 数据"
    o = stats.get("overall") or {}
    t = stats.get("thirds") or {}
    r = stats.get("recent") or {}
    lines = [
        f"整窗 fwd5: mean={o.get('mean')} icir={o.get('icir')} 负IC占比={o.get('neg_share')} (n={o.get('n')})",
    ]
    for lab in ("early", "middle", "late"):
        s = t.get(lab)
        lines.append(f"  {lab}: mean={s.get('mean') if s else '—'} icir={s.get('icir') if s else '—'}")
    lines.append(f"最新20日: mean={r.get('mean')} neg_share={r.get('neg_share')}")
    if stats.get("failure_signals"):
        lines.append("失效信号: " + " | ".join(stats["failure_signals"]))
    else:
        lines.append("失效信号: 未检出 (IC 未持续走低/转负)")
    return "\n".join(lines)


def _fmt_seg(name: str, s: dict) -> str:
    if not s or not s.get("ok"):
        return f"[{name}] {s.get('error') if s else '无数据'}"
    return (f"[{name}] {s['n_days']}日 总收益 {s['total_return_pct']}% "
            f"盈利日占比 {s['pos_day_share']:.0%} Top5盈利日占比 {s.get('top5_gain_days_share')}\n"
            f"  最佳3日 {s['top_gain_days']} 最差3日 {s['top_loss_days']}")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="归因分析: 时段分解 / 融合IC / 仓位审计")
    ap.add_argument("--refresh-ic", action="store_true", help="强制重算融合IC缓存")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    res = run_all(refresh_ic=args.refresh_ic)

    print("=== 1) 融合因子滚动 IC (121日, 20日窗) ===")
    print(_fmt_ic(res.get("fusion_ic_rolling") or {}))
    print("\n=== 2) 收益时段分解 ===")
    for name, s in (res.get("segment_attribution") or {}).items():
        print(_fmt_seg(name, s))
    print("\n=== 3) DRL 行为审计 ===")
    for name, b in (res.get("behavior_audit") or {}).items():
        if not b or not b.get("ok"):
            print(f"  [{name}] {b.get('error') if b else '无数据'}")
            continue
        ex = " / ".join(f"{k}={v}" for k, v in b.items() if k not in ("ok", "note"))
        print(f"  [{name}] {ex}")
        if b.get("note"):
            print(f"    注: {b['note']}")

    if args.save:
        os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        print(f"\n已保存: {OUT_JSON}")


if __name__ == "__main__":
    main()
