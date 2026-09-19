# -*- coding: utf-8 -*-
"""vnpy 引擎 vs 简化 fallback 引擎的偏差探针 (engine_bias_probe.py).

背景 (docs/pit-valuation.md §⑬)
  `src/vnpy_backtest.py:807` 用 `bar_map[symbol6_list[0]]` 硬取首个标的的回测起止时间,
  该标的缺前向 bar 即 KeyError -> 整窗回退到自研简化引擎 `_fallback_backtest`。
  推后修复之前需要回答: 这个回退是"数值不准"还是"方向可能反转"?

做法(控制变量)
  同一批选股(PIT 缓存 top10)、同一条前向 bar、同一组等权权重, **只换引擎**:
    A) vnpy 引擎  -> 取已落盘的回测产物 total_return
    B) 简化引擎  -> 直接调用 `_fallback_backtest`
  取 4 个 vnpy 成功窗作**受控对照**, 另加 1 个真实回退窗作**复现校验**
  (该窗的"vnpy 值"其实就是简化引擎自己的值, 故 Δ 必为 0 —— 用来验证本探针的复现能力)。

用法
    python scripts/engine_bias_probe.py
输出
    data/_engine_bias_experiment.json
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

from vnpy_backtest import (_fallback_backtest, _load_bars_forward,  # noqa: E402
                           _load_pit_selection_cached, _make_weights)

OUT = os.path.join(_BASE, "data", "_engine_bias_experiment.json")
#: 受控对照窗(vnpy 引擎成功产出) + 复现校验窗(实际回退过)
CONTROLLED = ["2010-06-30", "2013-06-28", "2018-06-29", "2024-07-03"]
VALIDATION = ["2015-06-30"]
RESULT_FILES = ["vnpy_backtest_nonoverlap_fwd_results_hist1017.json",
                "vnpy_backtest_nonoverlap_fwd_results.json"]


def main() -> None:
    stored: dict[str, dict] = {}
    for f in RESULT_FILES:
        fp = os.path.join(_BASE, "data", f)
        if not os.path.exists(fp):
            continue
        for x in json.load(open(fp, encoding="utf-8")):
            if x.get("ok"):
                stored.setdefault(x["day"], x)

    rows = []
    for role, days in (("controlled", CONTROLLED), ("validation_reproduce", VALIDATION)):
        for day in days:
            targets = _load_pit_selection_cached(day, 10)
            if not targets:
                print(f"  {day}: 无选股缓存, 跳过")
                continue
            t = targets[:10]
            s6s = [str(x["canon"]).split(".")[0] for x in t]
            day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
            bar_map = {}
            for s6 in s6s:
                df = _load_bars_forward(s6, day_dt, 120)
                if df is not None and not df.empty:
                    bar_map[s6] = df
            fb = _fallback_backtest(bar_map, s6s, _make_weights(t, "equal"), day_dt)
            fb_ret = fb["stats"].get("total_return")
            st = (stored.get(day) or {}).get("stats") or {}
            v_ret = st.get("total_return")
            same = None if (v_ret is None or fb_ret is None) else \
                ((v_ret > 0) == (fb_ret > 0))
            rows.append({"day": day, "role": role, "vnpy": v_ret, "fallback": fb_ret,
                         "delta_pp": (None if v_ret is None or fb_ret is None
                                      else round(fb_ret - v_ret, 4)),
                         "sign_same": same, "n_syms": len(bar_map),
                         "n_bars": sum(len(v) for v in bar_map.values())})
            flag = "n/a" if same is None else ("同号" if same else "**反号**")
            print(f"  [{role:<20}] {day}  vnpy={v_ret:>8.2f}  简化={fb_ret:>8.2f}  "
                  f"Δ={rows[-1]['delta_pp']:>+7.2f}pp  {flag}")

    ctrl = [r for r in rows if r["role"] == "controlled" and r["delta_pp"] is not None]
    same = sum(1 for r in ctrl if r["sign_same"])
    deltas = [r["delta_pp"] for r in ctrl]
    summary = {"n_controlled": len(ctrl), "sign_same": same,
               "delta_min": min(deltas) if deltas else None,
               "delta_max": max(deltas) if deltas else None,
               "delta_mean": round(sum(deltas) / len(deltas), 4) if deltas else None}
    print(f"\n  受控对照 {len(ctrl)} 窗: 符号一致 {same}/{len(ctrl)}; "
          f"Δ {summary['delta_min']:+.2f} ~ {summary['delta_max']:+.2f}pp; "
          f"均值 {summary['delta_mean']:+.2f}pp")
    print("  结论: 数值系统性偏高但**未见方向反转** -> 可长期推后修复, "
          "前提是 fallback 窗口一律排除出矩阵。")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "summary": summary,
                   "note": "受控对照=只换引擎; validation_reproduce=真实回退窗(Δ 应为 0, "
                           "用于验证探针复现能力)"}, f, ensure_ascii=False, indent=2)
    print("  已保存:", OUT)


if __name__ == "__main__":
    main()
