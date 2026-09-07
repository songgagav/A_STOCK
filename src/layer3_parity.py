# -*- coding: utf-8 -*-
"""第三层: 回测-模拟一致性验证 (排除过拟合)

方法:
  模拟盘运行日 = 最近 5 个交易日 2026-08-31 ~ 2026-09-04 (daily_summary 齐全).
  回测用与实盘同款 load_targets(D) 池 (C<D 盘前视角), 跑 6 个交易日
  (08-28 作为建仓预热日, 使 08-31 起回测侧已持有与模拟盘同源池的持仓,
  消除"首日空仓买在收盘"的人为偏差), 仅对比尾部 5 天 08-31~09-04.

指标(与用户口径一致):
  - 日收益偏差 = |回测日收益 - 模拟日收益| <= 0.5pp
  - 最大回撤偏差 <= 2pp
  - 年化Sharpe差 <= 0.3
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import BacktestRunner

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
BLT = os.path.join(DATA_DIR, "backtest_latest.json")
BT_TAG = "parity_layer3"
# 建仓预热日 + 模拟盘5个运行日
PRE_DAYS = ["2026-08-28"]
PAPER_DAYS = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def load_paper_equity():
    """读 daily_summary steps.paper.equity -> {day: equity}"""
    out = {}
    for day in PRE_DAYS + PAPER_DAYS:
        p = os.path.join(DAILY_DIR, day.replace("-", ""), "daily_summary.json")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        eq = ((d.get("steps") or {}).get("paper") or {}).get("equity")
        if eq is None:
            eq = (d.get("summary") or {}).get("equity")
        if eq is not None:
            out[day] = float(eq)
    return out


def daily_rets(eq_map, days):
    """按给定有序日期链求日收益%"""
    rets = {}
    prev = None
    for d in days:
        if d in eq_map and prev is not None and eq_map.get(prev):
            rets[d] = (eq_map[d] / eq_map[prev] - 1) * 100.0
        prev = d if d in eq_map else prev
    return rets


def max_dd_from_rets(rets):
    """从按日升序收益序列计算最大回撤(pp, 负值表示回撤)"""
    cum, peak, mdd = 1.0, 1.0, 0.0
    for d in rets:
        cum *= (1 + rets[d] / 100.0)
        peak = max(peak, cum)
        mdd = min(mdd, cum / peak - 1)
    return mdd * 100.0


def sharpe_annual(rets):
    """日收益序列 -> 年化夏普 (mean/std*sqrt252), 与 performance_report 同口径"""
    import math
    vals = list(rets.values())
    n = len(vals)
    if n < 2:
        return None
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    std = math.sqrt(var)
    if std <= 1e-12:
        return None
    return mean / std * math.sqrt(252)


def cum_ret(rets):
    c = 1.0
    for d in rets:
        c *= (1 + rets[d] / 100.0)
    return (c - 1) * 100.0


def main():
    # 1) 模拟盘权益链
    paper_eq = load_paper_equity()
    chain = PRE_DAYS + PAPER_DAYS
    paper_eq = {k: v for k, v in paper_eq.items() if k in chain}
    missing = [d for d in chain if d not in paper_eq]
    if missing:
        print("模拟盘缺失日:", missing)
    print("模拟盘权益:", {d: round(paper_eq[d], 2) for d in paper_eq})

    # 2) 回测 (备份/恢复 backtest_latest.json)
    snap = None
    if os.path.exists(BLT):
        with open(BLT, encoding="utf-8") as f:
            snap = f.read()
    r = BacktestRunner(days=6, start=date(2026, 8, 1)).run(tag=BT_TAG)
    if snap is not None:
        with open(BLT, "w", encoding="utf-8") as f:
            f.write(snap)
    bt_eq = {c["day"]: float(c["equity"]) for c in r["curve"]}
    bt_eq = {k: v for k, v in bt_eq.items() if k in chain}
    print("回测权益:", {d: round(bt_eq[d], 2) for d in bt_eq})

    # 3) 对齐: 尾部5个运行日
    aligned = [d for d in PAPER_DAYS if d in paper_eq and d in bt_eq]
    paper_rets = daily_rets(paper_eq, PRE_DAYS + aligned)
    bt_rets = daily_rets(bt_eq, PRE_DAYS + aligned)
    print("模拟盘日收益%:", {d: round(v, 4) for d, v in paper_rets.items()})
    print("回测日收益%: ", {d: round(v, 4) for d, v in bt_rets.items()})

    dev = {d: round(abs(bt_rets.get(d, 0) - paper_rets.get(d, 0)), 4)
           for d in aligned}
    max_dev_day = max(dev, key=dev.get)
    paper_mdd = max_dd_from_rets({d: paper_rets[d] for d in aligned})
    bt_mdd = max_dd_from_rets({d: bt_rets[d] for d in aligned})
    p_sharpe = sharpe_annual({d: paper_rets[d] for d in aligned})
    b_sharpe = sharpe_annual({d: bt_rets[d] for d in aligned})

    report = {
        "window": {"start": aligned[0], "end": aligned[-1], "n_days": len(aligned),
                   "method": "回测含1个建仓预热日(2026-08-28), 仅对比尾部5个模拟盘运行日; "
                             "回测池=实盘同款 load_targets(D) C<D 盘前视角"},
        "paper": {"days": aligned, "equity": {d: paper_eq[d] for d in aligned},
                  "daily_return_pct": paper_rets,
                  "cum_return_pct": round(cum_ret({d: paper_rets[d] for d in aligned}), 4),
                  "max_drawdown_pct": round(paper_mdd, 4),
                  "sharpe_annual": round(p_sharpe, 4) if p_sharpe is not None else None},
        "backtest": {"days": aligned, "equity": {d: bt_eq[d] for d in aligned},
                     "daily_return_pct": bt_rets,
                     "cum_return_pct": round(cum_ret({d: bt_rets[d] for d in aligned}), 4),
                     "max_drawdown_pct": round(bt_mdd, 4),
                     "sharpe_annual": round(b_sharpe, 4) if b_sharpe is not None else None},
        "deviations": {
            "daily_return_pp": dev,
            "max_abs_daily_dev_pp": max(dev.values()),
            "max_dev_day": max_dev_day,
            "pass_daily_le_0_5pp": max(dev.values()) <= 0.5,
            "dd_diff_pp": round(abs(bt_mdd - paper_mdd), 4),
            "pass_dd_le_2pp": abs(bt_mdd - paper_mdd) <= 2.0,
            "sharpe_diff": round(abs((b_sharpe or 0) - (p_sharpe or 0)), 4),
            "pass_sharpe_le_0_3": (b_sharpe is not None and p_sharpe is not None
                                   and abs(b_sharpe - p_sharpe) <= 0.3),
        },
    }
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "factor_validation_layer3.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n=== LAYER3_RESULT ===")
    print(json.dumps(report["deviations"], ensure_ascii=False, indent=2))
    print(json.dumps(report["paper"], ensure_ascii=False, indent=2))
    print(json.dumps(report["backtest"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
