# -*- coding: utf-8 -*-
"""上线前复验 · PaperBook 执行链**等效验证** (preflight_paperbook.py).

复验清单"四、执行层"在本环境无法验证真实券商行为(TRADE_BROKER=paper, easytrader 未接入),
故改为在 PaperBook 层做**语义等价**测试, 并明确标注哪些结论**不能**外推到真实通道:
  · 资金不足   -> 是否拒单(不产生持仓/不扣错钱)
  · T+1 锁定   -> 当日买入能否当日卖出
  · 幂等重放   -> 同一目标池重复执行是否产生重复委托
  · 内部对账   -> cash + market_value == equity, 手续费与成交记录自洽
  · 涨跌停推算 -> 各板块涨跌幅与四舍五入到分是否正确

**不可外推**: 真实通道的成交/部分成交/拒单/超时/断线重连/券商回报对账。
用法
    python scripts/preflight_paperbook.py
输出
    data/preflight_paperbook.json
"""
from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

OUT = os.path.join(_BASE, "data", "preflight_paperbook.json")
R: dict = {}
PRICES: dict[str, float] = {}


def _mk(cash: float = 100000.0):
    from paper_book import PaperBook
    pb = PaperBook(init_capital=cash)
    pb.cash = cash
    pb.market_price = lambda canon: PRICES.get(canon, 0.0)   # 注入价格, 不触网
    return pb


def main() -> None:
    from paper_book import _limit_prices
    ok = lambda b: "OK  " if b else "**FAIL**"
    out: list[dict] = []

    print("=== 1. 涨跌停推算 (各板块 + 四舍五入到分) ===")
    cases = [("600519.SH", 100.0, 110.0, 90.0), ("000001.SZ", 10.0, 11.0, 9.0),
             ("300750.SZ", 100.0, 120.0, 80.0), ("688111.SH", 50.0, 60.0, 40.0),
             ("830799.BJ", 10.0, 13.0, 7.0), ("600000.SH", 10.05, 11.06, 9.05)]
    for canon, lc, eu, ed in cases:
        up, dn = _limit_prices(canon, lc)
        good = abs(up - eu) < 1e-9 and abs(dn - ed) < 1e-9
        print(f"  {ok(good)} {canon} 昨收{lc} -> 涨停{up} 跌停{dn} (期望 {eu}/{ed})")
        out.append({"case": canon, "last_close": lc, "up": up, "down": dn,
                    "expect": [eu, ed], "pass": good})
    R["limit_prices"] = out
    print(f"  另: 无昨收 -> {_limit_prices('600519.SH', 0)} (None,None=不限制)")

    print("\n=== 2. 资金不足 -> 应拒单 ===")
    PRICES["600519.SH"] = 100.0
    pb = _mk(10000.0)
    c0 = pb.cash
    r = pb.buy("600519.SH", 1000, 100.0)      # 需 ~10 万, 现金仅 1 万
    held = pb.positions.get("600519.SH", {}).get("qty", 0)
    good = held == 0
    print(f"  {ok(good)} 买入 1000 股@100 (需10万) 现金1万 -> 返回={type(r).__name__} "
          f"持仓={held} 现金 {c0:.2f}->{pb.cash:.2f}")
    R["insufficient_funds"] = {"pass": good, "held": held, "cash_before": c0,
                               "cash_after": pb.cash}

    print("\n=== 3. T+1 锁定 -> 当日买入不得当日卖出 ===")
    pb = _mk(100000.0)
    b = pb.buy("600519.SH", 100, 100.0)
    qty_before = pb.positions.get("600519.SH", {}).get("qty", 0)
    locked = pb.positions.get("600519.SH", {}).get("locked_qty", None)
    s = pb.sell("600519.SH", 100, 100.0)
    qty_after = pb.positions.get("600519.SH", {}).get("qty", 0)
    good = qty_after == qty_before and qty_before > 0
    print(f"  {ok(good)} 买入100股 -> 持仓={qty_before} locked_qty={locked}; "
          f"当日卖出 -> 持仓={qty_after} (卖返回={type(s).__name__})")
    R["t_plus_1"] = {"pass": good, "qty_after_buy": qty_before, "locked_qty": locked,
                     "qty_after_sell": qty_after}

    print("\n=== 4. 幂等重放 -> 同一目标池执行两次, 第二次应 0 成交 ===")
    PRICES.update({"600519.SH": 100.0, "000001.SZ": 10.0, "600036.SH": 30.0})
    pb = _mk(100000.0)
    target = [{"canon": "600519.SH"}, {"canon": "000001.SZ"}, {"canon": "600036.SH"}]
    latest = dict(PRICES)
    try:
        pb.rebalance(target, latest)
        n1 = len(pb.trades_today) if hasattr(pb, "trades_today") else None
        eq1, cash1 = pb.cash + pb.market_value(), pb.cash
        t1 = len(getattr(pb, "trades", []) or [])
        pb.rebalance(target, latest)
        t2 = len(getattr(pb, "trades", []) or [])
        eq2, cash2 = pb.cash + pb.market_value(), pb.cash
        good = abs(cash1 - cash2) < 1e-6 and abs(eq1 - eq2) < 1e-6
        print(f"  {ok(good)} 第一次后 成交累计={t1} 现金={cash1:.2f} 权益={eq1:.2f}")
        print(f"        第二次后 成交累计={t2} 现金={cash2:.2f} 权益={eq2:.2f} "
              f"(新增成交 {t2 - t1})")
        R["idempotent_replay"] = {"pass": good, "trades_1": t1, "trades_2": t2,
                                  "cash_1": cash1, "cash_2": cash2,
                                  "equity_1": eq1, "equity_2": eq2}
    except Exception as e:  # noqa: BLE001
        print(f"  **ERR** rebalance 调用失败: {type(e).__name__}: {str(e)[:120]}")
        R["idempotent_replay"] = {"pass": False, "error": f"{type(e).__name__}: {e}"}

    print("\n=== 5. 内部对账: cash + market_value == equity; 手续费自洽 ===")
    pb = _mk(100000.0)
    pb.buy("600519.SH", 100, 100.0)
    pb.buy("000001.SZ", 500, 10.0)
    sn = pb.snapshot()
    eq_calc = pb.cash + pb.market_value()
    eq_snap = float(sn.get("equity") or 0.0)
    d1 = abs(eq_calc - eq_snap)
    fees = float(sn.get("fees_paid") or 0.0)
    bf = float(sn.get("buy_fees") or 0.0)
    sf = float(sn.get("sell_fees") or 0.0)
    d2 = abs(fees - (bf + sf))
    good = d1 < 0.01 and d2 < 0.01
    print(f"  {ok(good)} 自算权益={eq_calc:.2f} snapshot权益={eq_snap:.2f} 差={d1:.4f}")
    print(f"        fees_paid={fees:.4f} vs buy+sell={bf + sf:.4f} 差={d2:.4f}")
    R["internal_recon"] = {"pass": good, "equity_calc": eq_calc, "equity_snap": eq_snap,
                           "fees_paid": fees, "buy_fees": bf, "sell_fees": sf}

    print("\n=== 6. 快照/恢复往返 (对应'断线重连'的状态一致性) ===")
    snap = pb.snapshot()
    pb2 = _mk(100000.0)
    pb2.restore(json.loads(json.dumps(snap)))
    s2 = pb2.snapshot()
    same_pos = set(pb.positions) == set(pb2.positions)
    same_cash = abs(pb.cash - pb2.cash) < 1e-6
    good = same_pos and same_cash
    print(f"  {ok(good)} 恢复后 持仓键一致={same_pos} 现金一致={same_cash} "
          f"({pb.cash:.2f} vs {pb2.cash:.2f})")
    R["snapshot_restore"] = {"pass": good, "same_positions": same_pos,
                             "same_cash": same_cash}

    npass = sum(1 for v in R.values() if isinstance(v, dict) and v.get("pass"))
    ntot = sum(1 for v in R.values() if isinstance(v, dict) and "pass" in v)
    print(f"\n=== 汇总: {npass}/{ntot} 通过 ===")
    R["_summary"] = {"pass": npass, "total": ntot,
                     "not_extrapolable": "真实通道的成交/部分成交/拒单/超时/断线重连/券商回报对账"}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False, indent=2, default=str)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()
