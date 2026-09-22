# -*- coding: utf-8 -*-
"""B 归因(第 2 批): 门控 RISK 状态占比.

做法: 对每个前向窗口调用 backtest_with_gate 的连续回放, 读 per_day 的 regime 分布。
注意: 门控判定依赖融合 IC 缓存(data/factor_mine/fusion_ic_121d.json, 仅近 121 日),
历史窗口的 regime 会退化为 'unknown' —— 这本身就是要报告的事实。

用法: python scripts/gate_regime_share.py
输出: data/gate_regime_share.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")
OUT = os.path.join(_BASE, "data", "gate_regime_share.json")
PY = sys.executable


def main() -> None:
    fw = [x for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    print(f"门控 regime 分布: {len(fw)} 个前向窗口\n")
    print(f"{'决策日':<12}{'天数':>6}{'normal':>8}{'caution':>9}{'risk':>7}"
          f"{'unknown':>9}{'risk占比%':>10}  结论")
    rows = []
    for x in fw:
        day = x["day"]
        end = x["stats"].get("end_date")
        tmp = os.path.join(tempfile.gettempdir(), f"gate_{day.replace('-', '')}.json")
        t0 = time.time()
        cp = subprocess.run([PY, os.path.join(_BASE, "src", "backtest_with_gate.py"),
                             "--start", day, "--end", end, "--output", tmp],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=900)
        if cp.returncode != 0 or not os.path.exists(tmp):
            # `or ""`: 解码失败时 stderr 会是 None, 直接切片会把真实失败报成 TypeError
            print(f"{day:<12} 失败: {(cp.stderr or '')[-160:]}")
            continue
        d = json.load(open(tmp, encoding="utf-8"))
        per = d.get("per_day") or []
        cnt: dict[str, int] = {}
        for p in per:
            cnt[p.get("regime") or "unknown"] = cnt.get(p.get("regime") or "unknown", 0) + 1
        n = max(len(per), 1)
        risk_share = 100.0 * cnt.get("risk", 0) / n
        real = n - cnt.get("unknown", 0)
        verdict = ("regime 全为 unknown -> 历史窗口门控不可算" if real == 0
                   else ("RISK 占比 > 30% -> 因子频繁失效" if risk_share > 30
                         else "RISK 占比正常"))
        rows.append({"day": day, "end": end, "n_days": n, "counts": cnt,
                     "risk_share": risk_share, "unknown_share": 100.0 * cnt.get("unknown", 0) / n})
        print(f"{day:<12}{n:>6}{cnt.get('normal', 0):>8}{cnt.get('caution', 0):>9}"
              f"{cnt.get('risk', 0):>7}{cnt.get('unknown', 0):>9}{risk_share:>10.1f}  "
              f"{verdict}  ({time.time() - t0:.0f}s)", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("\n已保存:", OUT)
    usable = [r for r in rows if r["unknown_share"] < 50]
    if usable:
        s = [r["risk_share"] for r in usable]
        print(f"\n可用于判定的窗口 {len(usable)} 个: RISK 占比均值 {sum(s)/len(s):.1f}%")
    else:
        print("\n[结论] 全部窗口 regime 均为 unknown -> 门控 RISK 占比在历史上不可算;")
        print("       根因: fusion_ic_121d.json 仅覆盖近 121 日, 门控无法回溯判定。")


if __name__ == "__main__":
    main()
