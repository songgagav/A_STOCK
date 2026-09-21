# -*- coding: utf-8 -*-
"""Regime 成本场景 (路线图 #9/⑨): 同一段回测在 normal / stress 下的成本与延迟投影.

问题
----
`vnpy_backtest.run_vnpy_backtest()` 的成本口径是**单场景写死**的: 佣金/印花/过户
取 `config.PAPER`, 滑点由 Almgren-Chriss 现算, 一次性折成 `long_rate` / `short_rate`
两个数。因此"这套参数下的回测结论"只在**一个**市场环境假设下成立 —— 一旦真实成交
比假设差(流动性收缩/波动放大/成交延迟), 回测给出的净值曲线没有任何下界信息。

本模块只做一件事
----------------
把"基准费率"投影到若干**具名场景**上, 得到该场景的费率。它**不自己算基准**
(基准由调用方从 config.PAPER + 动态滑点给出), 也**不改任何既有路径** ——
纯函数, 无 I/O, 无全局状态。

场景为何是这些数(不另造证据)
----------------------------
· `stress` 的滑点倍率/手续费倍率取用户 2026-09 指定的"标准压力测试项"
  (滑点×4、手续费×2、延迟+1 bar)。**它们是压力假设, 不是从本仓数据推出来的**
  —— 记在此处以免后人误当实测值引用。
· 有本仓依据的是**量级校验**: A 股主板涨跌停 ±10%, 而本仓止损阈值
  `PAPER.stop_loss=3%`; 万5 基准滑点的 4 倍 = 万20 = 0.20%, 仍比一个跌停板
  小 50 倍 —— 即"×4 滑点"不会把模型送到物理不可能的区域, 属可承受的悲观假设。
· `latency_bars` 的语义是**信号延迟 N 根 bar**: 决策在 T 日收盘产生, 但成交推到
  T+N 日。本仓既有回测已是"D 日决策、D 日收盘撮合", 故 stress 下延迟 +1 bar
  等于"T+1 才成交", 与 A 股 T+1 交收约束同向, 不是凭空加严。

用法
----
    from regime_costs import resolve_scenario, project_rates
    sc = resolve_scenario("stress")               # 或 os.environ["REGIME_SCENARIO"]
    rates = project_rates(buy_rate, sell_rate, sc)
    # rates["long_rate"] / rates["short_rate"] -> 喂给 vnpy lab.add_contract_setting
"""
from __future__ import annotations

import os

#: 场景名 -> 投影规则。`slippage_mult` 只作用于**滑点分量**, `fee_mult` 只作用于
#: **税费分量**; 两者分开, 因为手续费率是费率表约定的(可查证), 而滑点是市场状态
#: 的函数(不可查证) —— 混在一起会让"到底是哪一项变贵了"无法归因。
SCENARIOS: dict[str, dict] = {
    "normal": {
        "slippage_mult": 1.0,
        "fee_mult": 1.0,
        "latency_bars": 0,
        "label": "常态",
        "note": "与既有单场景回测口径逐位一致(倍率全 1、无延迟)",
    },
    "stress": {
        "slippage_mult": 4.0,
        "fee_mult": 2.0,
        "latency_bars": 1,
        "label": "压力",
        "note": ("流动性收缩+波动放大(滑点×4) + 费率上调/最低佣金占比抬升(费×2) "
                 "+ 成交延迟 1 根 bar。压力假设, 非实测值"),
    },
}

#: 默认场景。**默认必须是 normal** —— 否则压力值会静默混进既有产物, 使历史
#: 回测结果与今天的不可比(本仓对"静默改变产物语义"零容忍)。
DEFAULT_SCENARIO = "normal"

#: 环境变量名(供 harness / 运维切换, 不必改代码)
ENV_VAR = "REGIME_SCENARIO"


def scenario_names() -> list[str]:
    """可用场景名(稳定顺序, 供 CLI/报告枚举)。"""
    return sorted(SCENARIOS.keys())


def resolve_scenario(name: str | None = None) -> dict:
    """取场景定义。name 为 None 时按 `ENV_VAR` -> `DEFAULT_SCENARIO` 解析。

    未知场景名**抛 KeyError**(不静默回退): 拼错场景名却拿到 normal 结果,
    是本类工具最危险的失效模式 —— 看起来"压力测试通过了", 其实根本没跑压力。
    """
    if name is None:
        name = os.environ.get(ENV_VAR) or DEFAULT_SCENARIO
    key = str(name).strip().lower()
    if key not in SCENARIOS:
        raise KeyError(f"未知 regime 场景: {name!r}; 可用: {scenario_names()}")
    sc = dict(SCENARIOS[key])
    sc["name"] = key
    return sc


def project_rates(buy_rate: float, sell_rate: float, scenario: dict | str | None,
                  *, slippage_share: float | None = None) -> dict:
    """把基准买卖费率投影到场景。

    Parameters
    ----------
    buy_rate, sell_rate : float
        基准费率(买/卖各一个), 与 `vnpy_backtest` 传给 `add_contract_setting`
        的 `long_rate` / `short_rate` 同口径。
    scenario : dict | str | None
        场景定义(或场景名); None 走 `resolve_scenario()`。
    slippage_share : float | None
        基准费率中**属于滑点**的比例(0..1)。给定后 `fee_mult` 只作用于
        `(1-slippage_share)` 的税费部分, 使归因精确。**不给则退化**为
        "整块费率都按 slippage_mult 放大, 再叠加 fee_mult 的税费增量" 的
        保守近似 —— 宁可高估成本, 不可低估(压力场景的用途就是找下界)。

    返回 {'buy_rate','sell_rate','long_rate','short_rate','slippage_mult',
          'fee_mult','latency_bars','scenario','scenario_label'}
    """
    sc = resolve_scenario(scenario) if not isinstance(scenario, dict) else dict(scenario)
    smul = float(sc.get("slippage_mult", 1.0))
    fmul = float(sc.get("fee_mult", 1.0))

    def _proj(rate: float) -> float:
        r = float(rate)
        if slippage_share is None:
            # 保守近似: 滑点分量放大 + 税费分量的增量(差值法, 避免二次放大滑点)
            return r * smul + r * (fmul - 1.0)
        s = min(max(float(slippage_share), 0.0), 1.0)
        return r * (s * smul + (1.0 - s) * fmul)

    b, sl = _proj(buy_rate), _proj(sell_rate)
    return {
        "buy_rate": b,
        "sell_rate": sl,
        "long_rate": b,       # vnpy 口径别名(买入)
        "short_rate": sl,     # vnpy 口径别名(卖出)
        "slippage_mult": smul,
        "fee_mult": fmul,
        "latency_bars": int(sc.get("latency_bars", 0) or 0),
        "scenario": sc.get("name", "custom"),
        "scenario_label": sc.get("label", ""),
    }


def cost_delta_bps(baseline_rate: float, scenario_rate: float) -> float:
    """场景相对基准的单边费率增量(bps)。用于报告"这个场景贵了多少"。"""
    return round((float(scenario_rate) - float(baseline_rate)) * 10000.0, 4)


def describe(scenario: dict | str | None = None) -> str:
    """一行人类可读描述(日志/报告用)。"""
    sc = resolve_scenario(scenario) if not isinstance(scenario, dict) else dict(scenario)
    return (f"{sc.get('name')}({sc.get('label','')}): "
            f"滑点×{sc.get('slippage_mult')} 费×{sc.get('fee_mult')} "
            f"延迟{sc.get('latency_bars')}bar")


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Regime 成本场景: 枚举/投影自检")
    ap.add_argument("--list", action="store_true", help="列出全部场景")
    ap.add_argument("--scenario", default=None, help="按名投影(默认读环境变量)")
    ap.add_argument("--buy", type=float, default=None, help="基准买入费率")
    ap.add_argument("--sell", type=float, default=None, help="基准卖出费率")
    args = ap.parse_args(argv)

    if args.list or args.buy is None:
        for n in scenario_names():
            sc = SCENARIOS[n]
            print(f"  {describe(n):58s} {sc.get('note','')}")
        return 0

    from config import PAPER
    comm = PAPER.get("commission", 0.00025)
    stamp = PAPER.get("stamp_tax", 0.0005)
    transfer = PAPER.get("transfer_fee", 0.00001)
    slip = PAPER.get("slippage", 0.0005)
    impact = PAPER.get("impact_cost", 0.0002)
    buy = args.buy if args.buy is not None else comm + transfer + slip + impact
    sell = args.sell if args.sell is not None else comm + stamp + transfer + slip + impact
    for n in scenario_names():
        r = project_rates(buy, sell, n)
        print(f"  {describe(n):58s} 买 {r['buy_rate']*100:.4f}% "
              f"(+{cost_delta_bps(buy, r['buy_rate']):.2f}bps) "
              f"卖 {r['sell_rate']*100:.4f}% (+{cost_delta_bps(sell, r['sell_rate']):.2f}bps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
