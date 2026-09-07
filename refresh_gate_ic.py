# -*- coding: utf-8 -*-
"""盘前 IC 缓存刷新任务.

收盘后运行, 完成三件事 (供次日盘中的 IC 门控与归因使用):
  1. 重放最近 days(=121) 个交易日融合因子逐日 IC, 写入
     data/factor_mine/fusion_ic_121d.json (滚动窗口以当日为终点);
  2. 重新生成门控快照 data/factor_gate_state.json (次日引擎读取的当天状态);
  3. 打印归因 121 日窗口与最新 20 日摘要 (人工/Agent 复核因子是否转弱).

用法: python refresh_gate_ic.py [--days 121] [--end 2026-09-04] [--no-ic]
依赖: factor_fusion(score_series_hist, 只读) + attribution_analysis 缓存读取.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)
os.chdir(_BASE)

DATA_DIR = os.path.join(_BASE, "data")
IC_CACHE = os.path.join(DATA_DIR, "factor_mine", "fusion_ic_121d.json")
GATE_STATE = os.path.join(DATA_DIR, "factor_gate_state.json")
_LIVE_FROM = os.environ.get("GATE_LIVE_FROM", "")


def refresh(days: int = 121, end: str | None = None,
            skip_ic: bool = False, live_from: str | None = None) -> dict:
    global _LIVE_FROM
    if live_from:
        _LIVE_FROM = str(live_from).replace("-", "")
    from attribution_analysis import compute_fusion_ic
    from attribution_analysis import rolling_ic_stats
    import factor_gate

    out = {"days": days, "ok": True}
    if not skip_ic:
        ic = compute_fusion_ic(end=end or "2026-09-04", days=days, refresh=True)
        if not ic.get("ok"):
            return {"ok": False, "error": ic.get("error")}
        out["ic"] = {
            "as_of_start": ic["as_of_start"], "as_of_end": ic["as_of_end"],
            "n_days": ic["n_days"], "n_with_ic": ic["n_with_ic"],
            "elapsed_s": ic.get("elapsed_s"),
            "cache": IC_CACHE,
        }
        stats = rolling_ic_stats(ic.get("daily_ic", []), window=20)
        out["ic_summary"] = {
            "overall": stats.get("overall"),
            "thirds": stats.get("thirds"),
            "recent20": stats.get("recent"),
            "failure_signals": stats.get("failure_signals"),
        }

    plan = factor_gate.build_plan_from_cache(daily_loss=None, base_interval=3)
    out["gate_plan"] = plan
    p = factor_gate.save_gate_state(plan, path=GATE_STATE)
    out["gate_state_path"] = p
    out["ledger"] = record_gate_day(plan, live_from=_LIVE_FROM)
    out["acceptance"] = acceptance_report()
    return out


# ------------------------------------------------------------------
# 路径A: 带门控净值账本 (paper_equity_after_gate 曲线)
# ------------------------------------------------------------------
PAPER_AFTER_GATE = os.path.join(DATA_DIR, "paper_after_gate.json")


def record_gate_day(plan: dict | None = None,
                    live_from: str | None = None,
                    force: bool = False) -> dict:
    """盘后记录: 取最近一份实盘纸面收盘净值 + 当日门控状态, 追加到账本.

    只记录 live_from (引擎门控真正生效日) 之后的交易日, 避免把门控上线前的
    净值误标成"带门控". live_from 缺省读环境变量 GATE_LIVE_FROM;
    force=True 绕过 (测试/人工核对用). 幂等: 已记录过该交易日则跳过.
    """
    from performance_report import load_daily_equities
    rows = load_daily_equities()
    if not rows:
        return {"ok": False, "error": "无实盘纸面净值"}
    live_from = live_from or os.environ.get("GATE_LIVE_FROM", "")
    live_from_n = str(live_from).replace("-", "")
    last = rows[-1]
    day = str(last["day"]).replace("-", "")
    if not force and live_from_n and day < live_from_n:
        return {"ok": False, "skipped": True,
                "reason": f"最近净值日 {day} < 门控生效日 {live_from_n}, 不记录"}
    if not force and not live_from_n:
        return {"ok": False, "skipped": True,
                "reason": "未设置 GATE_LIVE_FROM(门控生效日), 账本不记录"}
    equity = float(last["equity"])

    ledger = {"rows": [], "start": None, "end": None}
    if os.path.exists(PAPER_AFTER_GATE):
        try:
            with open(PAPER_AFTER_GATE, encoding="utf-8") as f:
                ledger = json.load(f)
        except Exception:
            ledger = {"rows": [], "start": None, "end": None}
    if any(str(r.get("date", "")).replace("-", "") == day for r in ledger.get("rows", [])):
        return {"ok": True, "skipped": True, "date": day, "path": PAPER_AFTER_GATE}

    plan = plan or {}
    regime = plan.get("regime", "unknown")
    active = regime not in ("unavailable", "disabled", "unknown")
    ledger.setdefault("rows", []).append({
        "date": str(last["day"]),
        "equity": round(equity, 2),
        "regime": regime,
        "exposure_mult": plan.get("exposure_mult", 1.0),
        "freeze_new_buys": bool(plan.get("freeze_new_buys")),
        "interval_days": plan.get("interval_days", 3),
        "gate_active": bool(active),
    })
    dates = [str(r["date"]) for r in ledger["rows"]]
    ledger["start"] = min(dates)
    ledger["end"] = max(dates)
    with open(PAPER_AFTER_GATE, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1)
    tag = "GATE_ACTIVE" if active else "GATE_INACTIVE"
    print(f"[{tag}] 账本追加 {last['day']} equity={equity:.2f} "
          f"regime={regime} exposure={plan.get('exposure_mult')}")
    return {"ok": True, "date": str(last["day"]), "n_rows": len(ledger["rows"]),
            "path": PAPER_AFTER_GATE}


# ------------------------------------------------------------------
# 预期改善验收表 (对照 2026-09-07 定义的目标)
# ------------------------------------------------------------------
ACCEPTANCE_TARGETS = [
    # key, 名称, 对照源, 基线, 目标, 方向(1=需>=目标, -1=需<=目标)
    # 基线 = 2026-09-07 vnpy 引擎权威重跑(120 交易日)实测值
    ("sharpe", "夏普比率", "vnpy 主链路(120日引擎级)", 1.1273, 0.60, 1),
    ("cagr", "年化复合增长", "vnpy 主链路(120日引擎级)", 0.2042, 0.08, 1),
    ("recovery", "恢复因子", "vnpy 主链路(120日引擎级)", 1.2871, 0.80, 1),
    ("pos_day_share", "盈利日占比", "vnpy 主链路(120日引擎级)", 0.5462, 0.75, 1),
    ("top5_gain_days_share", "Top5盈利日占比", "vnpy 主链路(120日引擎级)", 0.2287, 0.80, -1),
]


def _latest_vnpy() -> tuple[dict, list] | None:
    """最新 vnpy summary + 结算曲线(非空 balance)."""
    import glob
    dirs = sorted(glob.glob(os.path.join(DATA_DIR, "vnpy_backtest", "*")), reverse=True)
    for d in dirs:
        sp = os.path.join(d, "summary.json")
        cp = os.path.join(d, "curve.json")
        if not (os.path.exists(sp) and os.path.exists(cp)):
            continue
        try:
            with open(sp, encoding="utf-8") as f:
                summ = json.load(f)
            with open(cp, encoding="utf-8") as f:
                pts = json.load(f)
            curve = [{"day": p.get("date"), "equity": p.get("balance")}
                     for p in pts if p.get("balance") is not None]
            if summ.get("stats") and len(curve) >= 2:
                return summ, curve
        except Exception:
            continue
    return None


def acceptance_report() -> dict:
    """当前数值 vs 预期改善目标 (基线 = vnpy 引擎权威重跑, 2026-09-07)."""
    from attribution_analysis import segment_report
    from strategy_validation import metrics_from_vnpy_summary

    hit = _latest_vnpy()
    v_metrics, seg = {}, None
    if hit:
        summ, curve = hit
        v_metrics = metrics_from_vnpy_summary(summ)
        seg = segment_report(curve)
    src_map = {
        "sharpe": v_metrics.get("sharpe"),
        "cagr": v_metrics.get("cagr"),
        "recovery": v_metrics.get("recovery"),
        "pos_day_share": (seg or {}).get("pos_day_share"),
        "top5_gain_days_share": (seg or {}).get("top5_gain_days_share"),
    }
    items = []
    for key, label, src, base, target, direc in ACCEPTANCE_TARGETS:
        cur = src_map.get(key)
        cur = None if cur is None else float(cur)
        passed = False
        if cur is not None:
            passed = (cur >= target) if direc == 1 else (cur <= target)
        items.append({
            "metric": key, "label": label, "对照源": src,
            "baseline": base, "target": target,
            "current": round(cur, 4) if cur is not None else None,
            "passed": passed,
            "note": "" if passed else "门控上线后需 ≥N 个交易日样本重新量测",
        })
    return {
        "items": items,
        "verdict": "PASSED" if all(it["passed"] for it in items) else "NOT_PASSED",
        "note": "全部指标统一取 vnpy 主链路引擎级净值(120 交易日); "
                "基线已更新为 2026-09-07 权威重跑值; 门控上线后仍以带门控样本对比",
    }


def _print_acceptance(acc: dict) -> None:
    print("\n=== 预期改善验收表 (门控上线前基线对照) ===")
    for it in (acc or {}).get("items", []):
        mark = "✅" if it["passed"] else "❌"
        cur = it["current"]
        cur_s = "—" if cur is None else (f"{cur * 100:.2f}%" if it["metric"] in ("cagr", "pos_day_share", "top5_gain_days_share") else f"{cur:.4f}")
        tgt = it["target"]
        tgt_s = (f"{tgt * 100:.0f}%" if it["metric"] in ("cagr", "pos_day_share", "top5_gain_days_share") else f"{tgt:.2f}")
        print(f"  {mark} {it['label']}({it['对照源']}): 当前 {cur_s} | 目标 {('≤' if it['metric'] == 'top5_gain_days_share' else '≥')} {tgt_s}")
    print(f"  总评: {acc.get('verdict')} — {acc.get('note')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="盘前 IC 缓存刷新")
    ap.add_argument("--days", type=int, default=121)
    ap.add_argument("--end", default=None, help="窗口终点 YYYY-MM-DD (默认最新交易日)")
    ap.add_argument("--no-ic", action="store_true", help="跳过 IC 重放, 仅重算门控快照")
    ap.add_argument("--live-from", default=None,
                    help="门控真实生效日 YYYY-MM-DD (账本从该日之后开始记录)")
    args = ap.parse_args()

    res = refresh(days=args.days, end=args.end, skip_ic=args.no_ic,
                  live_from=args.live_from)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    _print_acceptance(res.get("acceptance"))
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
