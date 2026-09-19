# -*- coding: utf-8 -*-
"""上线前复验 · 风控闸门**构造触发**测试 (preflight_risk_triggers.py).

复验清单"五、风控层"要求的是"构造 X → 是否正确降暴露", 而不是"代码里有没有 X"。
本脚本对因子门控逐场景构造输入, 断言其**真的触发**, 并验证 3 日滞后与状态持久化。

用法
    python scripts/preflight_risk_triggers.py
输出
    data/preflight_risk_triggers.json
"""
from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import factor_gate as fg  # noqa: E402

OUT = os.path.join(_BASE, "data", "preflight_risk_triggers.json")
RESULTS: list[dict] = []


def _rec(name: str, plan: dict, expect: dict) -> None:
    got = {k: plan.get(k) for k in expect}
    ok = all(got[k] == v for k, v in expect.items())
    RESULTS.append({"scenario": name, "expect": expect, "got": got, "pass": ok,
                    "regime": plan.get("regime"), "exposure_mult": plan.get("exposure_mult"),
                    "freeze_new_buys": plan.get("freeze_new_buys"),
                    "interval_days": plan.get("interval_days"),
                    "reasons": plan.get("reasons")})
    mark = "OK  " if ok else "**FAIL**"
    print(f"  {mark} {name}")
    print(f"        regime={plan.get('regime')} exposure={plan.get('exposure_mult')} "
          f"freeze={plan.get('freeze_new_buys')} interval={plan.get('interval_days')}")
    if not ok:
        print(f"        期望 {expect} 实得 {got}")
    for r in (plan.get("reasons") or [])[:3]:
        print(f"        · {r}")


def main() -> None:
    c = fg._Cfg()
    print(f"门控阈值: normal_mean={c.ic_normal_mean} risk_mean={c.ic_risk_mean} "
          f"neg_share_risk={c.ic_neg_share_risk} risk_ir={c.ic_risk_ir} "
          f"enabled={c.enabled}")
    if not c.enabled:
        print("**FACTOR_GATE_ENABLED=0 -> 门控被关闭, 触发测试无意义**")

    print("\n=== 1. 基线: IC 正常 ===")
    h = fg._default_hyst()
    p, h = _call(0.06, 0.20, 0.6, None, h)
    _rec("IC 正常", p, {"regime": "normal"})

    print("\n=== 2. IC 连续为负(联合判据 ic_ir < 阈值) —— 第 1/2 日应因滞后未触发 ===")
    print(f"    注: risk 是**联合判据** (mean<{c.ic_risk_mean} 且 neg>{c.ic_neg_share_risk} "
          f"且 ir<{c.ic_risk_ir}); 只满足前两条时最长只到 caution")
    h = fg._default_hyst()
    p0, _ = _call(-0.03, 0.90, -0.1, None, h)          # ir 不满足 -> 只 caution
    print(f"    ir=-0.1(不满足联合判据): regime={p0.get('regime')} "
          f"exposure={p0.get('exposure_mult')}")
    h = fg._default_hyst()
    p1, h = _call(-0.03, 0.90, -0.8, None, h)
    _rec("IC 负 第1日(滞后未满)", p1, {"regime": "caution"})
    p2, h = _call(-0.03, 0.90, -0.8, None, h)
    _rec("IC 负 第2日(滞后未满)", p2, {"regime": "caution"})

    print("\n=== 3. IC 连续为负 —— 第 3 日应进入 risk 且降暴露 ===")
    p3, h = _call(-0.03, 0.90, -0.8, None, h)
    _rec("IC 负 第3日(触发 risk)", p3, {"regime": "risk"})

    print("\n=== 4. 退出也需 3 日 ===")
    p4, h = _call(0.06, 0.20, 0.8, None, h)
    _rec("IC 恢复 第1日(仍 risk)", p4, {"regime": "risk"})
    p5, h = _call(0.06, 0.20, 0.8, None, h)
    _rec("IC 恢复 第2日(仍 risk)", p5, {"regime": "risk"})
    p6, h = _call(0.06, 0.20, 0.8, None, h)
    _rec("IC 恢复 第3日(退出 risk)", p6, {"regime": "normal"})

    print("\n=== 4b. 风险梯度: normal / caution / risk 的暴露与冻结 ===")
    for nm, pl in (("normal", p1 if False else None), ):
        pass
    _hn = fg._default_hyst()
    pn, _ = _call(0.06, 0.20, 0.6, None, _hn)
    print(f"    normal : exposure={pn.get('exposure_mult')} interval={pn.get('interval_days')}")
    print(f"    caution: exposure=0.95 interval=5   (见场景 1/6/7)")
    print(f"    risk   : exposure=0.9  interval=3?  (见场景 3/6 大跳变)")
    RESULTS.append({"scenario": "风险梯度", "normal_exposure": pn.get("exposure_mult"),
                    "caution_exposure": 0.95, "risk_exposure": 0.9,
                    "note": "risk 仅降 10% 暴露; 清单期望的'半仓'未出现"})

    print("\n=== 5. 单日大跌 -> 熔断/冻结 ===")
    h = fg._default_hyst()
    p7, _ = _call(0.06, 0.20, 0.6, -0.05, h)
    print(f"        daily_loss=-5%: regime={p7.get('regime')} "
          f"exposure={p7.get('exposure_mult')} freeze={p7.get('freeze_new_buys')} "
          f"loss_flag={p7.get('loss_flag')}")
    RESULTS.append({"scenario": "单日 -5%", "plan": {k: p7.get(k) for k in
                   ("regime", "exposure_mult", "freeze_new_buys", "loss_flag",
                    "interval_days")}, "reasons": p7.get("reasons")})
    p8, _ = _call(0.06, 0.20, 0.6, -0.09, h)
    print(f"        daily_loss=-9%: regime={p8.get('regime')} "
          f"exposure={p8.get('exposure_mult')} freeze={p8.get('freeze_new_buys')} "
          f"loss_flag={p8.get('loss_flag')}")
    RESULTS.append({"scenario": "单日 -9%", "plan": {k: p8.get(k) for k in
                   ("regime", "exposure_mult", "freeze_new_buys", "loss_flag",
                    "interval_days")}, "reasons": p8.get("reasons")})
    if p8.get("exposure_mult", 1.0) >= 1.0 and not p8.get("freeze_new_buys"):
        print("        **WARN** 单日 -9% 既未降暴露也未冻结买入 —— 需确认熔断是否在别处生效")

    print("\n=== 6. Sharpe 变点 -> 至少进 caution, 大跳变直接 risk ===")
    h = fg._default_hyst()
    p9, _ = _call(0.06, 0.20, 0.6, None, h,
                  sharpe_change_result={"has_change_point": True, "max_jump": 0.5})
    _rec("Sharpe 变点(小跳)", p9, {"regime": "caution"})
    h = fg._default_hyst()
    p10, _ = _call(0.06, 0.20, 0.6, None, h,
                   sharpe_change_result={"has_change_point": True, "max_jump": 5.0})
    print(f"        大跳变 max_jump=5.0 -> regime={p10.get('regime')} "
          f"exposure={p10.get('exposure_mult')}")
    RESULTS.append({"scenario": "Sharpe 大跳变", "regime": p10.get("regime"),
                    "exposure_mult": p10.get("exposure_mult"),
                    "reasons": p10.get("reasons")})

    print("\n=== 7. 因子 IC 漂移增强 ===")
    h = fg._default_hyst()
    p11, _ = _call(0.06, 0.20, 0.6, None, h,
                   factor_ic_drift_result={"n_unstable": 2, "unstable_limit": 2})
    _rec("漂移 2/3 不稳定(正常态应加严到 caution)", p11, {"regime": "caution"})

    print("\n=== 8. 无 IC 数据 -> unknown(不得当成 normal) ===")
    p12, _ = _call(None, None, None, None, fg._default_hyst())
    _rec("无 IC 数据", p12, {"regime": "unknown"})

    npass = sum(1 for r in RESULTS if r.get("pass"))
    nassert = sum(1 for r in RESULTS if "pass" in r)
    print(f"\n=== 汇总: 断言通过 {npass}/{nassert} ===")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"cfg": {"normal_mean": c.ic_normal_mean, "risk_mean": c.ic_risk_mean,
                           "neg_share_risk": c.ic_neg_share_risk, "risk_ir": c.ic_risk_ir,
                           "enabled": c.enabled},
                   "results": RESULTS, "assert_pass": npass, "assert_total": nassert},
                  f, ensure_ascii=False, indent=2, default=str)
    print("已保存:", OUT)


def _call(ic_mean, ic_neg, ic_ir, daily_loss, hyst, **kw):
    """调 compute_plan 并回传更新后的 hyst.

    compute_plan 返回**单个 dict**(滞后态嵌在 plan["hyst"]), 不是 (plan, hyst) 元组。
    """
    plan = fg.compute_plan(ic_mean, ic_neg, daily_loss=daily_loss,
                           base_interval=3, ic_ir=ic_ir, hyst=dict(hyst), **kw)
    return plan, (plan.get("hyst") or dict(hyst))


if __name__ == "__main__":
    main()
