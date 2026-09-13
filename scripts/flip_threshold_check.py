# -*- coding: utf-8 -*-
"""item 5 验证: flipped 阈值收紧在**真实 IC 曲线**上的影响 (11 个决策日).

对比"旧判据(|short|>=0.01)"与"收紧后(|short|>=max(0.02, 0.25*|long|))"的隔离结论,
看收紧到底改变了什么、改变了多少。
用法: python scripts/flip_threshold_check.py   (需带 h5i_db 的解释器, 见 README)
"""
from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")


def main() -> None:
    from factor_gate import _Cfg, check_factor_ic_drift, load_factor_ic_from_curves

    class _Old(_Cfg):
        """旧判据: 翻转为 |short| >= 0.01, 无比例要求。"""
        flip_min_abs = 0.01
        flip_min_ratio = 0.0
        weaken_ratio = 0.7
        weaken_min_abs = 0.02

    days = [x["day"] for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"item 5 验证: {len(days)} 个决策日的真实 IC 曲线\n")
    print(f"{'决策日':<12}{'因子':<9}{'long':>9}{'short':>9}{'阈值新':>9}"
          f"{'旧判定':>12}{'新判定':>12}{'结论':>8}")
    print("-" * 92)

    changed = 0
    total = 0
    for day in days:
        fics = load_factor_ic_from_curves(as_of=day)
        if not fics:
            print(f"{day:<12} 无 IC 曲线, 跳过")
            continue
        ro = check_factor_ic_drift(fics, cfg=_Old())
        rn = check_factor_ic_drift(fics, cfg=_Cfg())
        names = sorted(set(ro["drift_detail"]) | set(rn["drift_detail"]))
        for nm in names:
            do, dn = ro["drift_detail"].get(nm), rn["drift_detail"].get(nm)
            if not do or do.get("mode") != "reversal":
                continue                      # 只看反转义因子(flipped 判据适用对象)
            verdict_old = ("翻转" if do.get("flipped") else
                           ("收敛" if do.get("weakened") else "保持"))
            verdict_new = ("翻转" if dn.get("flipped") else
                           ("收敛" if dn.get("weakened") else "保持"))
            iso_old = nm in ro["unstable_factors"]
            iso_new = nm in rn["unstable_factors"]
            mark = "" if (verdict_old, iso_old) == (verdict_new, iso_new) else "**变了**"
            total += 1
            if mark:
                changed += 1
            print(f"{day:<12}{nm:<9}{do['long_mean']:>9.4f}{do['short_mean']:>9.4f}"
                  f"{dn.get('flip_threshold', float('nan')):>9.4f}"
                  f"{verdict_old:>12}{verdict_new:>12}{mark:>8}")

    print("-" * 92)
    print(f"反转义因子判定共 {total} 条, 其中结论发生变化 {changed} 条")
    print("\n注: '翻转'=判为方向反转(信号变反向); '收敛'=强度塌陷; 两者都会隔离该因子权重。")


if __name__ == "__main__":
    main()
