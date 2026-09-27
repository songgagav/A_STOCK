"""一次性诊断: `data/daily/<date>/` 到底"该有哪些"、"缺了哪些"、缺的那几天是不是交易日.

## 为什么要这样算

先前把"09-25/09-26/09-27 没有 `data/daily/`"当成了异常。**这是误判**:
按 `trading_calendar` 实测, 2026-09-25 是周五、09-26 周六、09-27 周日, 而
**09-25 被日历判为"非交易日"** —— 非交易日不跑收盘选股, 因此**本来就不该有**
`data/daily/<该日>/`。

正确的问法不是"有没有", 而是:

    对每一个**交易日**, 有没有 `data/daily/<该日>/selection.json`?

缺口要分两类, 处置完全不同:
  · **非交易日无目录** = 正确行为(不是缺陷);
  · **交易日无目录**     = 真缺口, 需要归因(守护停了? 数据门禁 HALT? 管道失败?)。

## 输出

1. 区间内逐日的"是否交易日 / 有无目录 / 有无 selection.json / 有无 target_plan"表;
2. 汇总: 交易日总数、交易日缺目录数、非交易日有目录数(后者才是异常)。

**只读**。
"""

from __future__ import annotations

import datetime
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import trading_calendar as tc  # noqa: E402

START = datetime.date(2026, 8, 25)
END = datetime.date(2026, 9, 28)


def main() -> int:
    daily_root = os.path.join(_BASE, "data", "daily")
    drl_root = os.path.join(_BASE, "data", "drl")

    print("=" * 88)
    print("data/daily/ 与 data/drl/ 覆盖情况  %s .. %s" % (START, END))
    print("=" * 88)
    print("%-11s %-4s %-8s %-8s %-10s %-10s %s" % (
        "date", "wd", "trade?", "daily/", "selection", "target_plan", "note"))
    print("-" * 88)

    n_trade = 0
    n_trade_missing = 0
    n_trade_missing_sel = 0
    n_nontrade_with_dir = 0
    missing_trade = []
    nontrade_with = []

    day = START
    while day <= END:
        ds = day.strftime("%Y%m%d")
        ddir = os.path.join(daily_root, ds)
        has_dir = os.path.isdir(ddir)
        has_sel = os.path.exists(os.path.join(ddir, "selection.json"))
        has_plan = os.path.exists(
            os.path.join(drl_root, ds, "target_plan.json"))
        is_td = bool(tc.is_trading_day(day))

        note = ""
        if is_td:
            n_trade += 1
            if not has_sel:
                n_trade_missing += 1
                if not has_dir:
                    n_trade_missing_sel += 1
                note = "**交易日缺 selection**"
                missing_trade.append(ds)
        else:
            if has_dir:
                n_nontrade_with_dir += 1
                note = "非交易日却有目录"
                nontrade_with.append(ds)
            else:
                note = "非交易日(无目录=正常)"

        print("%-11s %-4s %-8s %-8s %-10s %-10s %s" % (
            day.isoformat(), day.strftime("%a"),
            "Y" if is_td else "n",
            "Y" if has_dir else "-",
            "Y" if has_sel else "-",
            "Y" if has_plan else "-",
            note))
        day += datetime.timedelta(days=1)

    print()
    print("=" * 88)
    print("汇总")
    print("=" * 88)
    print("  区间内交易日数                     : %d" % n_trade)
    print("  其中**缺** selection.json 的交易日 : %d" % n_trade_missing)
    print("    · 其中连目录都没有的             : %d" % n_trade_missing_sel)
    print("  非交易日却产出了目录(**异常**)     : %d" % n_nontrade_with_dir)
    if missing_trade:
        print()
        print("  缺 selection 的交易日明细: %s" % ", ".join(missing_trade))
    if nontrade_with:
        print("  非交易日却有目录: %s" % ", ".join(nontrade_with))
    print()
    print("  注: 09-25(Fri)/09-26(Sat)/09-27(Sun) 按日历均**非交易日**,")
    print("      故「没有 data/daily/」是**正确行为**, 不是缺陷 —— 需从缺口清单剔除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
