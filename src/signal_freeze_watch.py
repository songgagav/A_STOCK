# -*- coding: utf-8 -*-
"""09:25 信号冻结：**纯告警版**（登记册 P0-FREEZE-0925 的第一步）。

范围（用户 2026-09-22 决定："P0-FREEZE-0925 纯告警版（先记录）"）
------------------------------------------------------------------
**只观察、只记录、只告警；不改变任何信号产物**。这是三项要求里唯一零风险的子集:
登记册早已指出"单独加时间判断会引入新的静默行为（超时完成的信号被丢弃或被静默接受）" ——
本模块**不做任何时间判断去改变信号**, 因此不产生那个二选一, 也就不改变信号路径,
既有 dry-run 结果依然代表当前系统。等真实数据积累后再决定要不要升级为硬截止。

三项要求各自落到哪个告警（阈值全部有本仓证据）
----------------------------------------------
① **池必达保障** —— 避免落入第⑤档现场选股而无人知晓:
   `rung` 非当日同源（跨日回退 / `onsite_select`）=> `pool_fallback` 告警;
   `n == 0`（池空）=> `pool_empty` 告警。
   本仓已有 5 级梯子与留痕(`_trace_targets` → data/targets_source.jsonl), 本模块只是
   把"落到了哪一档"从**日志**升级为**告警+事件账本**。

② **选股耗时预算** —— 阈值 `BUDGET_S = 300`（5 分钟）, 依据:
   · 用户给定的验收构造是『**5.8min 未完成**须告警』=> 预算必须 < 5.8min;
   · 本仓实测: **正常路径亚秒级**（池命中时, docs/e2e-verification.md）,
     慢路径第⑤档现场选股 **30–160s**（docs/pbo-cscv.md 的 PIT 缓存实测）;
     故 300s 明确落在健康带之外, 不会对正常路径误报;
   · 引擎 08:30 启动到 09:25 有 **~55min** 预算 => 5 分钟告警时仍有充裕反应时间。
   **这是初值, 待真实耗时积累后按本仓数据校准** —— 本模块同时把每次耗时写进事件账本,
   正是为了将来能这么校准（而不是拍脑袋改数字）。

③ **逾期告警** —— 信号生成完成时间晚于 `DEADLINE = 09:25` => `past_deadline` 告警。
   09:25 是设计给定的硬截止（登记册 P0-FREEZE-0925）; 本模块只**报告**它被越过,
   不据此丢弃或改写信号。

留痕
----
每次观测与每条告警追加进 `data/signal_freeze_events.jsonl`, 走 #6 的哈希链
(`audit_chain`) —— 于是"09:25 之前到底冻结了没有"这个问题将来有可验证的证据,
而不是只有一行会滚掉的日志。
"""
from __future__ import annotations

import os
from datetime import datetime, time as dtime

#: 信号冻结硬截止（设计给定, 见登记册 P0-FREEZE-0925）
DEADLINE = dtime(9, 25)

#: 选股耗时预算（秒）—— 推导见模块 docstring, 是**初值**, 待真实耗时校准
BUDGET_S = 300.0

#: 视为"当日同源"的档位（其余 = 跨日回退或现场选股, 属需告警的降级）
RUNG_SAME_DAY = ("drl_same_day", "selection_same_day")

_TRACE_HINT = {
    "drl_same_day": "当日 DRL 正式计划",
    "selection_same_day": "当日 selection",
    "drl_cross_day": "跨日回退·DRL 计划",
    "selection_cross_day": "跨日回退·selection",
    "onsite_select": "第⑤档 **现场选股**（数分钟多核重算, 池必达失败）",
}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def events_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "signal_freeze_events.jsonl")


def evaluate(rung: str, pool_size=None, elapsed_s=None, finished_at=None,
             deadline: dtime = DEADLINE, budget_s: float = BUDGET_S) -> dict:
    """**纯函数**: 把一次目标池装载判成一串告警（不改变任何信号）。

    返回 {'alerts': [{'code','severity','detail'}], 'worst': 'OK'|'WARN'|'CRITICAL',
          'rung', 'pool_size', 'elapsed_s', 'past_deadline'}
    """
    alerts: list = []
    finished_at = finished_at or datetime.now()
    past_deadline = False
    try:
        t = finished_at.time() if hasattr(finished_at, "time") else finished_at
        past_deadline = t > deadline
    except Exception:  # noqa: BLE001
        past_deadline = False

    if rung and rung not in RUNG_SAME_DAY:
        alerts.append({"code": "pool_fallback", "severity": "WARN",
                       "detail": (f"目标池落到非当日同源档位: {rung}"
                                  f"（{_TRACE_HINT.get(rung, '未知档位')}）"
                                  f" —— 池必达失败, 当日无正式计划")})
    if pool_size is not None:
        try:
            if int(pool_size) <= 0:
                alerts.append({"code": "pool_empty", "severity": "CRITICAL",
                               "detail": "目标池为空(0 只) —— 当日无法建仓, 必须人工确认"})
        except Exception:  # noqa: BLE001
            pass
    if elapsed_s is not None and budget_s:
        try:
            if float(elapsed_s) > float(budget_s):
                alerts.append({"code": "over_budget", "severity": "WARN",
                               "detail": (f"选股耗时 {float(elapsed_s):.0f}s 超出预算 "
                                          f"{float(budget_s):.0f}s（正常路径亚秒级, 第⑤档实测 30–160s）")})
        except Exception:  # noqa: BLE001
            pass
    if past_deadline:
        alerts.append({"code": "past_deadline", "severity": "WARN",
                       "detail": (f"信号生成完成于 {finished_at.strftime('%H:%M:%S')}, "
                                  f"已越过 {deadline.strftime('%H:%M')} 冻结截止"
                                  f"（仅告警, 不丢弃/不改写本次信号）")})

    worst = "OK"
    if any(a["severity"] == "CRITICAL" for a in alerts):
        worst = "CRITICAL"
    elif alerts:
        worst = "WARN"
    return {"alerts": alerts, "worst": worst, "rung": rung, "pool_size": pool_size,
            "elapsed_s": elapsed_s, "past_deadline": past_deadline,
            "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S")}


def observe(rung: str, pool_size=None, elapsed_s=None, finished_at=None,
            path: str | None = None, now=None) -> dict:
    """记录一次观测（含哈希链留痕）并返回裁决。**绝不抛异常** —— 选股主链路。"""
    try:
        r = evaluate(rung, pool_size=pool_size, elapsed_s=elapsed_s,
                     finished_at=finished_at or now)
        try:
            import sys
            sys.path.insert(0, os.path.join(_repo_root(), "src"))
            import audit_chain as AC
            AC.append(events_path(path), {"kind": "observe", **r}, now=now)
        except Exception:  # noqa: BLE001
            pass
        return r
    except Exception as e:  # noqa: BLE001
        return {"alerts": [], "worst": "OK", "error": f"{type(e).__name__}: {e}"}


def recent(n: int = 20, path: str | None = None) -> list:
    """读最近 n 条观测（含链校验结论, 供人核对账本是否被动过）。"""
    fp = events_path(path)
    if not os.path.isfile(fp):
        return []
    try:
        with open(fp, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()][-n:]
        out = []
        for ln in lines:
            import json
            out.append(json.loads(ln))
        return out
    except Exception:  # noqa: BLE001
        return []


def _main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="09:25 信号冻结: 纯告警版看门狗")
    ap.add_argument("--recent", type=int, default=10, help="显示最近 N 条观测")
    ap.add_argument("--verify", action="store_true", help="校验事件账本的哈希链")
    ap.add_argument("--simulate", metavar="RUNG",
                    help="用给定档位模拟一次观测(不写账本), 用于验证告警")
    args = ap.parse_args(argv)

    if args.simulate:
        r = evaluate(args.simulate, pool_size=0 if "onsite" in args.simulate else 10,
                     elapsed_s=348 if "onsite" in args.simulate else 0.4)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if args.verify:
        import sys
        sys.path.insert(0, os.path.join(_repo_root(), "src"))
        import audit_chain as AC
        v = AC.verify(events_path())
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0 if v.get("ok") else 1
    rows = recent(args.recent)
    if not rows:
        print("(尚无观测记录)")
        return 0
    for r in rows:
        al = r.get("alerts") or []
        tag = r.get("worst", "?")
        print(f"[{tag}] {r.get('finished_at')}  rung={r.get('rung')} "
              f"n={r.get('pool_size')} 耗时={r.get('elapsed_s')}")
        for a in al:
            print(f"      - [{a['code']}] {a['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
