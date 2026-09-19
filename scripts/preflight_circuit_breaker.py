# -*- coding: utf-8 -*-
"""上线前复验 · 熔断 / 仓位约束的**构造触发** (preflight_circuit_breaker.py).

补齐复验清单"五、风控层"中因子门控之外的项:
  · 组合回撤 -8%  -> 熔断并限制仓位
  · 尾部风险 CVaR -> 触发
  · 波动率        -> 触发
  · 仓位约束      -> 单票权重被截断到上限

用法
    python scripts/preflight_circuit_breaker.py
输出
    data/preflight_circuit_breaker.json
"""
from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

OUT = os.path.join(_BASE, "data", "preflight_circuit_breaker.json")
R: list[dict] = []


def main() -> None:
    from risk_first import CircuitBreaker
    import config as C

    paper = getattr(C, "PAPER", {}) or {}
    cb = CircuitBreaker()
    print(f"阈值: drawdown={cb.drawdown_threshold} vol={cb.vol_threshold} "
          f"cvar={cb.cvar_threshold} accel={cb.drawdown_acceleration}")
    print(f"config.PAPER 相关: RF_CB_DRAWDOWN={paper.get('circuit_breaker_drawdown')} "
          f"RF_CB_VOL={paper.get('circuit_breaker_vol')} "
          f"RF_CB_CVAR={paper.get('circuit_breaker_cvar')}")

    print(f"\n{'场景':<34}{'level':>6}{'仓位上限':>10}  triggered")
    print("-" * 78)

    def run(name: str, **kw):
        lvl = cb.evaluate(**kw)
        try:
            lim = cb.get_position_limit()
        except Exception as e:  # noqa: BLE001
            lim = f"ERR:{type(e).__name__}"
        R.append({"scenario": name, "inputs": kw, "level": lvl,
                  "position_limit": lim, "triggered": list(cb.triggered)})
        print(f"{name:<34}{lvl:>6}{str(lim):>10}  {cb.triggered}")
        return lvl, lim

    # 基线
    run("正常 (回撤 0)", portfolio_drawdown=0.0)
    # 回撤梯度
    run("回撤 -5% (阈值内)", portfolio_drawdown=0.05)
    lv8, lim8 = run("回撤 -8% (达阈值)", portfolio_drawdown=0.08)
    run("回撤 -10%", portfolio_drawdown=0.10)
    lv12, lim12 = run("回撤 -12% (1.5x -> 清仓)", portfolio_drawdown=0.12)
    run("回撤 -20%", portfolio_drawdown=0.20)
    # 尾部风险
    run("CVaR95 = 6% (>阈值 5%)", portfolio_drawdown=0.0, cvar_95=0.06)
    run("CVaR95 = 4% (阈值内)", portfolio_drawdown=0.0, cvar_95=0.04)
    # 波动率
    run("年化波动 40% (>阈值 35%)", portfolio_drawdown=0.0, annualized_vol=0.40)
    run("年化波动 30% (阈值内)", portfolio_drawdown=0.0, annualized_vol=0.30)
    # 回撤加速
    run("近5日回撤恶化 +4%", portfolio_drawdown=0.0, drawdown_5d_change=0.04)
    # 组合
    run("回撤 8% + CVaR 6% + 波动 40%",
        portfolio_drawdown=0.08, cvar_95=0.06, annualized_vol=0.40)

    print("\n=== 仓位约束 (target_weighting 集中度上限) ===")
    from target_weighting import allocate_target_weights
    items = [{"canon": f"{600000+i}.SH", "fml": v}
             for i, v in enumerate([100.0, 90.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])]
    w = allocate_target_weights(items, mode="fml")
    ws = [float(x.get("target_weight") or 0.0) for x in items]
    eq = 1.0 / len(items)
    print(f"  输入: 极端不均 fml(100/90/1...)  等权={eq:.4f}")
    print(f"  输出权重: {[round(v, 4) for v in ws]}")
    print(f"  最大权重={max(ws):.4f}  权重和={sum(ws):.4f}  "
          f"相对等权倍数={max(ws) / eq:.2f}x")
    R.append({"scenario": "仓位约束(极端不均 fml)", "n": len(items),
              "max_weight": round(max(ws), 6), "sum_weight": round(sum(ws), 6),
              "equal": round(eq, 6), "max_mult": round(max(ws) / eq, 4),
              "weights": [round(v, 6) for v in ws]})
    if max(ws) > 0.5:
        print("  **WARN** 单票权重 >50%, 集中度上限可能未生效")
    if abs(sum(ws) - 1.0) > 1e-3:
        print(f"  **WARN** 权重和偏离 1: {sum(ws):.6f}")

    print("\n=== 判定 ===")
    ok_dd = lv8 >= 2
    ok_clear = lv12 == 3
    ok_cvar = R[6]["level"] >= 1
    ok_vol = R[8]["level"] >= 1
    print(f"  回撤 -8% 触发熔断(level>=2): {'OK' if ok_dd else '**FAIL**'} (level={lv8}, "
          f"仓位上限 {lim8})")
    print(f"  回撤 -12% 升级清仓(level=3): {'OK' if ok_clear else '**FAIL**'} (level={lv12}, "
          f"仓位上限 {lim12})")
    print(f"  CVaR 6% 触发: {'OK' if ok_cvar else '**FAIL**'} "
          f"(level={R[6]['level']}, {R[6]['triggered']})")
    print(f"  波动 40% 触发: {'OK' if ok_vol else '**FAIL**'} "
          f"(level={R[8]['level']}, {R[8]['triggered']})")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"thresholds": {"drawdown": cb.drawdown_threshold,
                                  "vol": cb.vol_threshold,
                                  "cvar": cb.cvar_threshold,
                                  "accel": cb.drawdown_acceleration},
                   "pass": {"drawdown_8": ok_dd, "clear_12": ok_clear,
                            "cvar": ok_cvar, "vol": ok_vol},
                   "results": R}, f, ensure_ascii=False, indent=2, default=str)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()
