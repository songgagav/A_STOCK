# -*- coding: utf-8 -*-
"""一次性: 把本轮两项并入登记册。

  A. `DECISION-FUSION-TRIM-Q-CALIBRATION` 补上**生产实际持有期**实测 + h7 异常档说明
     (该条已存在, 这里**追加**而非新建 —— 同一决策不建两个条目);
  B. 新增 `FINDING-BACKTEST-VS-PRODUCTION-HOLDING-GAP`:
     回测隐含持有期(4.1 天) vs 生产实际(中位 1 天) 的口径缺口。
"""
from __future__ import annotations

import json
import os

FP = os.path.join("ops", "acceptance_status.json")
d = json.load(open(FP, encoding="utf-8"))
items = d["items"]
by_id = {it["id"]: it for it in items}
NL = "\n"

APPEND = {
    "DECISION-FUSION-TRIM-Q-CALIBRATION": NL.join([
        "",
        "---",
        "",
        "## 2026-09-27 追加: **生产实际持有期**实测 => 结论向「不上线」进一步收敛",
        "",
        "从**生产台账**测量(非回测): 合并 `data/state.json` +",
        "`data/_backup_before_dryrun/state.json` + 13 个 `data/daily/<day>/paper_book.json`,"
        " 得 12 个交易日 / 122 笔成交(买 65 / 卖 57), FIFO 配平 51 笔卖出。",
        "",
        "**持有期(日历日) n=51**: min=1  p25=1  **中位=1**  p75=3  p90=6  max=7  均值=2.25",
        "",
        "```text",
        "   1 日 :  26  ##########################",
        "   2 日 :   8  ########",
        "   3 日 :  10  ##########",
        "   6 日 :   6  ######",
        "   7 日 :   1  #",
        "```",
        "",
        "=> **生产实际持有期落在 1~7 天档**。而 `_tools/trim_by_horizon.py` 实测该档",
        "   h7 的最优是 **q=0.00(不截尾, +1.67%)**",
        "   => **本决策(维持 `FUSION_TRIM_Q=0`)由生产数据支持, 不再只是「暂不上线」**。",
        "",
        "**同时暴露一个口径缺口**(已另立条): 回测隐含持有期约 **4.1 天**,",
        "而生产实际中位 **1 天** —— 两者差 4 倍。原因是 `portfolio_backtest.py`",
        "原先**没有实现 `min_hold_days`**(本轮已补), 且撮合时点与实盘不同。",
        "",
        "## h7 = **异常档**(已标注, 不影响主结论)",
        "",
        "各 horizon 的最优 q: `[h1=0.15, h3=0.03, h5=0.03, **h7=0.00**, h10=0.03,",
        "h20=0.05, h60=0.05, h120=0.05]`",
        "",
        "**h7 的 q=0.00 与相邻档(h5=0.03, h10=0.03)方向相反**, 且 h7 的不截尾均值",
        "(+1.67%)还**高于** h5/h10 的任一档 => 更像**噪声/窗口特异**, 不是稳定结构。",
        "判据: h1 的「最优 q=0.15」与 h7 的「最优 q=0.00」分别是两个极端,",
        "而**短期限各档的绝对差异都小于 1.3pp** —— 在该量级上排序不稳。",
        "",
        "**处理: 标注为异常档, 不据此改判据**。主结论(20~120 天档 q=0.05 幅度最大、",
        "短期限档几乎无收益)不受影响。",
        "",
        "**但这条也强化了决策**: 生产持有期中位 1 天 => 落在**截尾价值尚未显现**的区间,",
        "故「不上线」是对的; 若将来把持有期拉长到 20 天以上(如 `min_hold=20`),",
        "**那时才应重新评估截尾**。",
    ]),
}

NEW = [
    {
        "id": "FINDING-BACKTEST-VS-PRODUCTION-HOLDING-GAP",
        "level": "P1",
        "title": ("回测隐含持有期(约 4.1 天) vs 生产实际(中位 **1 天**)差 4 倍 "
                  "—— 回测不代表实盘节奏"),
        "status": "open",
        "detail": NL.join([
            "## 实测(2026-09-27)",
            "",
            "| 口径 | 持有期 | 依据 |",
            "|---|---|---|",
            "| **生产实际**(虚拟盘台账) | **中位 1 天**(p25=1, p75=3, p90=6, max=7) | 12 交易日 / 51 笔 FIFO 配平 |",
            "| 回测(无 min_hold, 改造前) | 约 **4.1 天** | 10 天窗口换手 484.10% = 2.42 往返 |",
            "| 回测(min_hold=2, 生产配置) | 约 **4.9 天** | 换手 407.87% |",
            "| 回测(min_hold=20) | 约 **20.4 天** | 换手 98.24% |",
            "",
            "=> **回测隐含 4.1 天, 生产实际 1 天, 差约 4 倍**。",
            "",
            "## 两个已知成因(其一本轮已修)",
            "",
            "1. **`portfolio_backtest.py` 原先没实现 `min_hold_days`** —— 它做的是",
            "   **每日全量再平衡**, 而生产 `realtime_engine`/`backtest_engine` 都按",
            "   `PAPER['min_hold_days']=2` 拦住「持仓不足 2 天」的卖出。**本轮已补**",
            "   (与 `backtest_engine.py:433-446` 逐行对齐), 故换手从 484% 降到 408%;",
            "2. **撮合时点不同**: 回测按**收盘价**撮合, 实盘按盘中(实测成交时间多在",
            "   `09:30:06~09:30:21`, 即开盘瞬间)。这会让「实际持有一天」在两种口径下",
            "   含义不同 —— 回测的「1 天」是隔夜, 实盘的「1 天」更接近当日开盘到次日开盘。",
            "",
            "## 为什么重要",
            "",
            "1. **持有期是标定截尾比例的自变量**(见 `DECISION-FUSION-TRIM-Q-CALIBRATION`):",
            "   用回测的 4.1 天去标定, 与实际 1 天落在**不同档**;",
            "2. 本仓既有「回测池与虚拟盘池分叉」的教训 —— 这是**同类问题的第二个实例**",
            "   (名字相同、跑的不是同一件事)。",
            "",
            "## 台账覆盖度(结论的边界)",
            "",
            "可用台账只覆盖 12 个交易日(2026-08-27..09-24), 且 **15 笔卖出无法配平**",
            "(建仓早于台账起点) => 中位数基于 51 笔, 样本不大。",
            "但「中位 1 天」这个量级与「p25=1」一致, 不像抽样噪声。",
        ]),
        "evidence": NL.join([
            "`_tools/holding_period.py`(可复现): 合并 `data/state.json` +",
            "`data/_backup_before_dryrun/state.json` + 13 个 `data/daily/*/paper_book.json`;",
            "FIFO 配平 51 笔; 分布 1日×26 / 2日×8 / 3日×10 / 6日×6 / 7日×1;",
            "回测换手数据来自 `_tools/cost_bridge.py` 的 min_hold 扫描",
            "(484.10% / 407.87% / 98.24%);",
            "`portfolio_backtest.py` 的 min_hold 缺口已在本轮修复并有 3 条新守卫。",
        ]),
        "next": NL.join([
            "**不要用回测隐含持有期去标定任何与持有期有关的参数**(截尾比例、min_hold、",
            "换手阈值)。标定前先用 `_tools/holding_period.py` 量一次生产实际值。",
            "**待补**: ① 让台账可长期累积(当前只有 12 天, 且 15 笔配不平);",
            "② 查撮合时点差异(回测收盘价 vs 实盘开盘附近)对收益口径的影响 ——",
            "这可能是「回测 -5.90% vs 实盘 +?」差异的另一来源;",
            "③ 若将来改 `min_hold` 拉长持有期, 必须**重新量一次**实际持有期再标定。",
        ]),
    },
]

applied = []
for eid, extra in APPEND.items():
    it = by_id.get(eid)
    if it is None:
        print("WARN 未找到待追加条目:", eid)
        continue
    if "2026-09-27 追加" in (it.get("detail") or ""):
        print("skip (已追加):", eid)
        continue
    it["detail"] = (it.get("detail") or "") + extra
    applied.append(eid)

added = 0
have = set(by_id)
for e in NEW:
    if e["id"] in have:
        print("skip (已存在):", e["id"])
        continue
    items.append(e)
    added += 1

d["generated_at"] = "2026-09-27"
with open(FP, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print("appended to:", applied)
print("added:", added, "| total items:", len(items))
