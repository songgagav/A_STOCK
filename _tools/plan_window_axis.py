"""一次性诊断: 判定 `data/drl/<D>/target_plan.json` 的**消费日轴**.

## 为什么要问这个问题

`_plan_is_formal` 的窗口是 `[前一交易日 16:00, 消费日 00:00)` —— 窗口的**下界**
取决于"消费日"是谁。而"目录日 D"与"消费日"可能是**同一天**, 也可能
**差一个交易日**。这两种读法会给出**完全相反的结论**:

* 读法 A: 目录日 D 就是消费日 D
  ⇒ 窗口 = `[prev_trade(D) 16:00, D 00:00)`
  ⇒ 但生产 run_daily 在 **19:10** 才跑 ⇒ 写出的 `generated_at` 必然 **>= D 19:10**
  ⇒ **永远晚于窗口上界 D 00:00** ⇒ 第 1 档永远 0%(系统性缺陷)。

* 读法 B: 目录日 D 的计划供**下一个交易日** ND 消费
  ⇒ 窗口 = `[D 16:00, ND 00:00)`
  ⇒ 19:10 落在这个窗口内 ⇒ **通过** ⇒ 第 1 档本应命中。

两种读法的预测**互相排斥**, 所以真实文件能直接判定, 不需要读代码猜。

## 方法

对每一个真实存在的 `target_plan.json`, 分别按两种读法算窗口并判过/不过,
然后看哪种读法能**自洽**:
  · 若生产交易日(19:10 生成)的文件在读法 A 下**全灭**、在读法 B 下**全过**,
    则读法 B 成立、读法 A 证伪。

## 输出

逐文件一行 + 两种读法的汇总计数。**只读**, 不写任何状态。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from collections import Counter

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import trading_calendar as tc  # noqa: E402


def prev_trade_day(day: datetime.date, limit: int = 30) -> datetime.date | None:
    d = day - datetime.timedelta(days=1)
    for _ in range(limit):
        if tc.is_trading_day(d):
            return d
        d -= datetime.timedelta(days=1)
    return None


def next_trade_day(day: datetime.date, limit: int = 30) -> datetime.date | None:
    d = day + datetime.timedelta(days=1)
    for _ in range(limit):
        if tc.is_trading_day(d):
            return d
        d += datetime.timedelta(days=1)
    return None


def main() -> int:
    root = os.path.join(_BASE, "data", "drl")
    if not os.path.isdir(root):
        print("ERROR: 找不到", root)
        return 2

    rows = []
    for dd in sorted(x for x in os.listdir(root)
                     if x.isdigit() and len(x) == 8):
        fp = os.path.join(root, dd, "target_plan.json")
        if not os.path.exists(fp):
            continue
        with open(fp, encoding="utf-8") as fh:
            plan = json.load(fh)
        gen = plan.get("generated_at") or ""
        if not gen:
            continue
        try:
            gadt = datetime.datetime.strptime(str(gen), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        dirday = datetime.date(int(dd[:4]), int(dd[4:6]), int(dd[6:8]))

        # 读法 A: 消费日 == 目录日 D
        pd_ = prev_trade_day(dirday)
        a_lo = datetime.datetime.combine(pd_, datetime.time(16, 0))
        a_hi = datetime.datetime.combine(dirday, datetime.time(0, 0))
        ok_a = a_lo <= gadt < a_hi

        # 读法 B: 消费日 == 目录日的下一交易日 ND
        nd = next_trade_day(dirday)
        b_lo = datetime.datetime.combine(dirday, datetime.time(16, 0))
        b_hi = datetime.datetime.combine(nd, datetime.time(0, 0))
        ok_b = b_lo <= gadt < b_hi

        rows.append({
            "dir": dd,
            "wd": dirday.strftime("%a"),
            "is_trade": tc.is_trading_day(dirday),
            "gen": str(gen),
            "hhmm": str(gen)[11:16],
            "A": ok_a,
            "B": ok_b,
        })

    print("=" * 84)
    print("data/drl/<D>/target_plan.json  —— 消费日轴判定")
    print("=" * 84)
    print("%-9s %-4s %-6s %-21s %-6s %-7s %-7s" % (
        "dir", "wd", "trade", "generated_at", "hhmm", "asA", "asB"))
    print("-" * 84)
    for r in rows:
        mark = "" if r["is_trade"] else "  <- 非交易日目录"
        print("%-9s %-4s %-6s %-21s %-6s %-7s %-7s%s" % (
            r["dir"], r["wd"], "Y" if r["is_trade"] else "n",
            r["gen"], r["hhmm"],
            "PASS" if r["A"] else "fail",
            "PASS" if r["B"] else "fail", mark))

    print()
    print("-" * 84)
    print("汇总(全部 %d 份):" % len(rows))
    print("  读法 A(消费日 == 目录日)      :", dict(Counter(
        "PASS" if r["A"] else "fail" for r in rows)))
    print("  读法 B(消费日 == 下一交易日)  :", dict(Counter(
        "PASS" if r["B"] else "fail" for r in rows)))

    tr = [r for r in rows if r["is_trade"]]
    print()
    print("只看**交易日目录**(生产 run_daily 19:10 产出的那批, n=%d):" % len(tr))
    print("  读法 A:", dict(Counter("PASS" if r["A"] else "fail" for r in tr)))
    print("  读法 B:", dict(Counter("PASS" if r["B"] else "fail" for r in tr)))
    hh = sorted(r["hhmm"] for r in tr)
    if hh:
        print("  generated_at 时刻范围: %s ~ %s" % (hh[0], hh[-1]))
    ntr = [r for r in rows if not r["is_trade"]]
    if ntr:
        hh2 = sorted(r["hhmm"] for r in ntr)
        print("  非交易日目录(n=%d) 时刻范围: %s ~ %s"
              % (len(ntr), hh2[0], hh2[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
