# -*- coding: utf-8 -*-
"""第二层: 交易成本验证 (A/B 控制实验)

同一历史窗口 (最近 10 个交易日, 与 performance_report 一致) 下:
  旧规则(before): 早期摩擦参数 (换手25% / 无最小持仓 / 小单500 / 无调仓间隔 /
                  集中度触发1.35 / 不拦非多信号)
  新规则(after) : 当前降摩擦+成本门槛全量 (换手20% / 持仓>=2天 / 小单1000+1.5% /
                  调仓间隔3天 / 触发1.55 / 拦非多信号 / 止损-3%全持仓)
手续费开关: fee=1 真实费率(佣金/印花/过户) ; fee=0 仅关闭手续费(保留滑点/冲击,
          保证买入决策与成交路径一致 -> 期末差近似手续费侵蚀总额).
指标:
  日均交易次数下降 / 单笔毛利/单笔手续费 / 手续费占毛利润比
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PAPER
from backtest_engine import BacktestRunner

DAYS = 10

BEFORE = dict(max_turnover_pct=0.25, min_reorder_notional=500.0,
              min_reorder_weight_pct=0.0, min_hold_days=0,
              rebalance_interval_days=0, concentration_trigger_mult=1.35,
              min_signal_buy=False)
AFTER = dict(max_turnover_pct=0.20, min_reorder_notional=1000.0,
             min_reorder_weight_pct=1.5, min_hold_days=2,
             rebalance_interval_days=3, concentration_trigger_mult=1.55,
             min_signal_buy=True)
FEE_ON = dict(commission=0.00025, stamp_tax=0.0005, transfer_fee=0.00001)
FEE_OFF = dict(commission=0.0, stamp_tax=0.0, transfer_fee=0.0)


def run(stage: str, fee: int):
    PAPER.update(BEFORE if stage == "before" else AFTER)
    PAPER.update(FEE_ON if fee else FEE_OFF)
    r = BacktestRunner(days=DAYS).run(tag=f"cost_{stage}_fee{fee}")
    return r


if __name__ == "__main__":
    res = {}
    for st in ("before", "after"):
        for fee in (1, 0):
            key = f"{st}_fee{fee}"
            print(f"\n########## RUN {key} ##########", flush=True)
            r = run(st, fee)
            res[key] = {"final_equity": r["final_equity"],
                        "total_trades": r["total_trades"],
                        "trade_days": r["trade_days"]}
    print("\n=== COST_AB_RESULT ===")
    print(res)
