# -*- coding: utf-8 -*-
"""降级驱动量的**采集** (P2-DEGRADE-INPUTS): 拒单率 / 滑点 / 参与率。

为什么只采集不判定
------------------
`health_state` 的驱动量此前是延迟/数据滞后/账户静态估值/L3/数据停流。清单第 4 项
要求"延迟/拒单率/滑点驱动自动降级", 缺后两项。它们进状态机都需要一个**阈值**
("拒单率多高算降级"), 而本仓纪律是「阈值须基于下游表现」—— 阈值不能拍。

故本模块只做三件事:
  1. 从**已有账本**把量算出来(`data/order_audit.jsonl` 的拒单、成交的滑点);
  2. 给出**分布**与分位数, 而不是一个判定;
  3. 如实报告"数据够不够标定"(样本天数), 不够就说不够。

数据来源(全部已有, 不新增采集点)
--------------------------------
· 拒单率 —— `pretrade_compliance` 往 `data/order_audit.jsonl` 写的每条裁决
  (`action` ∈ execute/reject/pending_approval/...)。按日聚合即得拒单率。
· 滑点   —— 虚拟盘成交价里含常量成本 7bps(见 `micro_cost`)。逐笔的
  `impact_bps` / `exec_risk_bps` **只有当撮合层拿到 ADV/波动率时才产生**,
  而生产路径目前不传(常量分支), 故当前"实测滑点"等于常量率, 分布是退化的
  —— 这件事必须如实说出来, 而不是把常量当成"实测结果"。
· 参与率 —— 单笔名义额 / ADV。ADV 走 `market_stats`(一次批量取)。

**当前状态(2026-09-22 实测)**: `data/order_audit.jsonl` 不存在、
`data/state.json.trades_history` 为空 —— 虚拟盘尚无成交, 故三个量都无样本。
`report()` 会返回 `n_days=0` 并说明原因, 而不是给一张空表让人误读成"无异常"。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

#: 标定所需的最少**有成交的天数**。依据: 分位数要稳, 至少需要能覆盖一周以上
#: 不同市况; 5 天以下连"分布"都谈不上, 只够看个例。**这是占位初值**,
#: 与其它阈值一样待真实分布累积后按下游表现重标。
MIN_DAYS_FOR_CALIBRATION = 10


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def audit_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "order_audit.jsonl")


def _read_jsonl(fp: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(fp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return out
    return out


def _day_of(rec: dict) -> str:
    ts = str(rec.get("ts") or rec.get("at") or "")
    return ts[:10] if len(ts) >= 10 else "unknown"


# --------------------------------------------------------------------------
# 1) 拒单率
# --------------------------------------------------------------------------
#: 视为"被拦下"的动作。`execute` 是放行, 其余都是没成交。
BLOCKING_ACTIONS = ("reject", "pending_approval")


def rejection_stats(path: str | None = None) -> dict:
    """按日统计订单裁决 -> 拒单率(含高危转人工)。

    返回 {'n_total','n_blocked','rate','by_day':[...], 'n_days','by_action',
          'top_reasons','source','note'}
    `rate` = (reject + pending_approval) / 全部裁决。**pending_approval 也算被拦**
    —— 它确实没有成交, 只是原因不同; 两者分开列出以便区分"规则拒单"与"转人工"。
    """
    recs = _read_jsonl(audit_path(path))
    by_day: dict = {}
    by_action: dict = {}
    reasons: dict = {}
    for r in recs:
        a = str(r.get("action") or "unknown")
        by_action[a] = by_action.get(a, 0) + 1
        d = _day_of(r)
        bucket = by_day.setdefault(d, {"day": d, "total": 0, "blocked": 0,
                                       "reject": 0, "pending": 0})
        bucket["total"] += 1
        if a in BLOCKING_ACTIONS:
            bucket["blocked"] += 1
            bucket["reject" if a == "reject" else "pending"] += 1
        for why in (r.get("reasons") or []):
            key = str(why)[:60]
            reasons[key] = reasons.get(key, 0) + 1
    rows = []
    for d in sorted(by_day):
        b = by_day[d]
        b["rate"] = round(b["blocked"] / b["total"], 6) if b["total"] else None
        rows.append(b)
    n_total = len(recs)
    n_blocked = sum(1 for r in recs if str(r.get("action")) in BLOCKING_ACTIONS)
    out = {
        "n_total": n_total, "n_blocked": n_blocked,
        "rate": round(n_blocked / n_total, 6) if n_total else None,
        "by_day": rows, "n_days": len([d for d in by_day if d != "unknown"]),
        "by_action": by_action,
        "top_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:10],
        "source": audit_path(path),
        "note": "", "calibration_ready": False,
    }
    if n_total == 0:
        out["note"] = ("无订单审计记录(data/order_audit.jsonl 不存在或为空) "
                       "=> 虚拟盘尚无下单流水, 拒单率无样本")
    elif out["n_days"] < MIN_DAYS_FOR_CALIBRATION:
        out["note"] = (f"仅 {out['n_days']} 天样本 < {MIN_DAYS_FOR_CALIBRATION} 天 "
                       f"=> 可看个例, 不足以标定阈值")
    else:
        out["calibration_ready"] = True
        out["note"] = "样本天数已达标定下限, 可按分位数设定阈值"
    return out


# --------------------------------------------------------------------------
# 2) 滑点分布
# --------------------------------------------------------------------------
def slippage_stats(trades: list | None = None, *, path: str | None = None) -> dict:
    """按日/按分位统计实测滑点。

    口径: 只统计**真实产生了滑点分解**的成交(`impact_bps` 或 `exec_risk_bps`
    存在者)。当前生产撮合走常量分支(不传 ADV/波动率), 故这些字段**不产生** ——
    此时如实返回退化样本与原因, **不把常量 7bps 当成实测分布**。
    """
    rows = list(trades or [])
    source = "caller"
    if not rows:
        source = "data/state.json.trades_history"
        try:
            fp = path or os.path.join(_repo_root(), "data", "state.json")
            with open(fp, encoding="utf-8-sig") as f:
                st = json.load(f)
            for day, items in (st.get("trades_history") or {}).items():
                for t in items or []:
                    rows.append({**t, "day": day})
        except Exception:  # noqa: BLE001
            rows = []
    vals, by_day = [], {}
    n_trades = len(rows)
    for t in rows:
        try:
            imp = t.get("impact_bps")
            erk = t.get("exec_risk_bps")
        except Exception:  # noqa: BLE001
            continue
        if imp is None and erk is None:
            continue
        v = float(imp or 0.0) + float(erk or 0.0)
        vals.append(v)
        d = str(t.get("day") or "unknown")
        by_day.setdefault(d, []).append(v)
    out = {"n_trades": n_trades, "n_with_slippage": len(vals), "source": source,
           "mean_bps": None, "p50_bps": None, "p95_bps": None, "max_bps": None,
           "by_day": [], "n_days": 0, "calibration_ready": False,
           "constant_rate_bps": None, "note": ""}
    try:
        import micro_cost
        out["constant_rate_bps"] = round(micro_cost.constant_rate() * 1e4, 4)
    except Exception:  # noqa: BLE001
        pass
    if not vals:
        out["note"] = (
            ("虚拟盘尚无成交" if n_trades == 0 else
             f"{n_trades} 笔成交均未产生滑点分解") +
            " => 实测滑点分布无样本。原因: 生产撮合走**常量成本分支**"
            "(PaperBook 未收到 avg_daily_volume/volatility), 逐笔 impact_bps 不产生。"
            "**不得把常量 7bps 当作实测分布引用**。")
        return out
    import statistics
    vals_sorted = sorted(vals)
    def _q(p):
        i = min(int(len(vals_sorted) * p), len(vals_sorted) - 1)
        return round(vals_sorted[i], 4)
    out.update({
        "mean_bps": round(statistics.fmean(vals), 4),
        "p50_bps": _q(0.50), "p95_bps": _q(0.95), "max_bps": round(max(vals), 4),
        "by_day": [{"day": d, "n": len(v), "mean_bps": round(statistics.fmean(v), 4)}
                   for d, v in sorted(by_day.items())],
        "n_days": len([d for d in by_day if d != "unknown"]),
    })
    out["calibration_ready"] = out["n_days"] >= MIN_DAYS_FOR_CALIBRATION
    out["note"] = (f"{len(vals)} 笔含滑点分解; "
                   + ("样本天数已达标定下限" if out["calibration_ready"]
                      else f"仅 {out['n_days']} 天 < {MIN_DAYS_FOR_CALIBRATION} 天"))
    return out


# --------------------------------------------------------------------------
# 3) 参与率分布
# --------------------------------------------------------------------------
def participation_stats(trades: list | None = None, *, adv: dict | None = None) -> dict:
    """单笔名义额 / ADV 的分布。拆单判定的唯一输入。"""
    rows = list(trades or [])
    if not rows:
        try:
            fp = os.path.join(_repo_root(), "data", "state.json")
            with open(fp, encoding="utf-8-sig") as f:
                st = json.load(f)
            for day, items in (st.get("trades_history") or {}).items():
                for t in items or []:
                    rows.append({**t, "day": day})
        except Exception:  # noqa: BLE001
            rows = []
    out = {"n_trades": len(rows), "n_with_adv": 0, "participation_p50": None,
           "participation_p95": None, "max_notional": None,
           "note": "", "cap_reference": None}
    try:
        from config import PAPER
        out["cap_reference"] = PAPER.get("participation_cap")
    except Exception:  # noqa: BLE001
        pass
    if not rows or not adv:
        out["note"] = ("无成交或无 ADV => 参与率分布无样本"
                       if not rows else "有成交但未提供 ADV")
        return out
    ps, notionals = [], []
    for t in rows:
        canon = str(t.get("canon") or t.get("symbol") or "")
        a = (adv or {}).get(canon)
        try:
            n = abs(float(t.get("price", 0)) * float(t.get("qty", 0)))
        except Exception:  # noqa: BLE001
            continue
        if not a or n <= 0:
            continue
        ps.append(n / float(a))
        notionals.append(n)
    if not ps:
        out["note"] = "无可配对的 (成交, ADV) 样本"
        return out
    ps.sort()
    out.update({
        "n_with_adv": len(ps),
        "participation_p50": round(ps[len(ps) // 2], 8),
        "participation_p95": round(ps[min(int(len(ps) * 0.95), len(ps) - 1)], 8),
        "max_notional": round(max(notionals), 2),
        "note": (f"{len(ps)} 笔可配对" +
                 (f"; 上限 {out['cap_reference']}" if out["cap_reference"] else "")),
    })
    return out


# --------------------------------------------------------------------------
# 4) 汇总
# --------------------------------------------------------------------------
def report(*, audit: str | None = None, compute_adv: bool = False,
           sample_canons=None) -> dict:
    """一次算齐三个量, 供日更/面板消费。**只采集, 不判定**。

    compute_adv=True 时会去取真实 ADV(需要 h5i; 仅当有成交件时才值得跑)。
    """
    rej = rejection_stats(audit)
    slp = slippage_stats()
    adv_map = None
    if compute_adv and sample_canons:
        try:
            import market_stats
            adv_map = {c: v["adv"] for c, v in
                       (market_stats.stats_for(sample_canons) or {}).items()}
        except Exception:  # noqa: BLE001
            adv_map = None
    par = participation_stats(adv=adv_map)
    ready = bool(rej.get("calibration_ready") and slp.get("calibration_ready"))
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rejection": rej,
        "slippage": slp,
        "participation": par,
        "min_days_for_calibration": MIN_DAYS_FOR_CALIBRATION,
        "calibration_ready": ready,
        "verdict": ("可标定" if ready else
                    "样本不足 => **本轮只采集不判定**(阈值须基于下游表现)"),
        "wired_into_state_machine": False,
        "why_not_wired": (
            "拒单率与滑点的降级阈值尚未标定; 按本仓 method-1(阈值须基于下游表现), "
            "先累积分布再进 health_state.assemble。当前**不参与任何拦截**。"),
    }


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="降级驱动量采集: 拒单率 / 滑点 / 参与率")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--audit", default=None, help="订单审计文件路径")
    args = ap.parse_args(argv)
    r = report(audit=args.audit)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print("=== 拒单率 ===")
    j = r["rejection"]
    print(f"  裁决总数={j['n_total']} 被拦={j['n_blocked']} 拒单率={j['rate']} "
          f"天数={j['n_days']}")
    print(f"  按动作: {j['by_action']}")
    print(f"  说明: {j['note']}")
    print("=== 滑点 ===")
    s = r["slippage"]
    print(f"  成交={s['n_trades']} 含滑点分解={s['n_with_slippage']} "
          f"常量口径={s['constant_rate_bps']}bps")
    print(f"  均值={s['mean_bps']} p50={s['p50_bps']} p95={s['p95_bps']}")
    print(f"  说明: {s['note']}")
    print("=== 参与率 ===")
    p = r["participation"]
    print(f"  成交={p['n_trades']} 可配对={p['n_with_adv']} "
          f"p50={p['participation_p50']} p95={p['participation_p95']}")
    print(f"  说明: {p['note']}")
    print(f"\n结论: {r['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
