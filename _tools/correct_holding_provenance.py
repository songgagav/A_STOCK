# -*- coding: utf-8 -*-
"""一次性: 更正「生产实际持有期中位 1 天」—— 该结论的样本被<u>预检回放</u>污染。

## 错在哪(过程留痕)

`_tools/holding_period.py` 合并了三个来源:
  · `data/state.json`                     (明确的生产盘)
  · `data/_backup_before_dryrun/state.json`(**预检隔离产物**)
  · `data/daily/<day>/paper_book.json`     (每日快照, 但实测 0 笔新增)

而 `data/preflight_dryrun.json` 写着:
    tmp_data_dir = C:\\...\\Temp\\_interp_parity_B_blufu271
    prod_state_untouched = True

⇒ 那个备份是**解释器一致性预检在临时目录里跑出来的**, 不是生产实盘增量。
实测构成: **bak 卖出 54 笔 / live 卖出 3 笔 ⇒ bak 占 95%**
⇒ **"中位持有期 1 天" 主要来自预检回放, 不能作为生产实盘的证据。**

## 处置

1. 把 `FINDING-BACKTEST-VS-PRODUCTION-HOLDING-GAP` 的该半条**降级为待重测**;
2. 在 `docs/hist-window-protocol.md` §3b 给「中位 1 天」**加限定**;
3. 登记一条新发现: **`state.json` 台账只保留最近几天 ⇒ 生产持有期无法从现状测量**;
4. 记下**代码可证**的那一半(它不依赖台账):
   · `min_hold_days` 只管「离开目标池」一条路径(L951-981, 且在 `if gate_open:` 内);
   · 止损(L983-1036)与风控(L1038-1049)在它**之外**, 注释明写「止损/风控除外」;
   · `config.PAPER`: `rebalance_interval_days=3`, `stop_loss=0.03`, `trailing_stop=True`
     ⇒ **调仓门每 3 自然日才开一次**, 而 `min_hold=2` < 3
     ⇒ **min_hold 在实践中几乎不可能拦住任何"离开目标池"的卖出**
     (窗口一开, 最早买入的仓位也已持满 >=3 天)。
"""
from __future__ import annotations

import json
import os

FP = os.path.join("ops", "acceptance_status.json")
d = json.load(open(FP, encoding="utf-8"))
items = d["items"]
by_id = {it["id"]: it for it in items}
NL = "\n"

# ---------------------------------------------------------------- 1) 降级 + 更正
it = by_id.get("FINDING-BACKTEST-VS-PRODUCTION-HOLDING-GAP")
if it is None:
    print("WARN 未找到 FINDING-BACKTEST-VS-PRODUCTION-HOLDING-GAP")
else:
    if "2026-09-27 更正" not in (it.get("detail") or ""):
        it["detail"] = (it.get("detail") or "") + NL.join([
            "",
            "---",
            "",
            "## ⚠️ 2026-09-27 更正: 「生产实际中位 1 天」**证据被污染**, 降级为待重测",
            "",
            "**错在哪**: 我在 `_tools/holding_period.py` 里把",
            "`data/_backup_before_dryrun/state.json` 当成了生产台账一起合并。",
            "而 `data/preflight_dryrun.json` 写着:",
            "",
            "```text",
            "tmp_data_dir         = C:\\Users\\...\\Temp\\_interp_parity_B_blufu271",
            "prod_state_untouched = True",
            "md5 before/after 一致 = True",
            "```",
            "",
            "⇒ 那个目录是**解释器一致性预检在隔离临时目录里跑出来的产物**, **不是生产实盘增量**。",
            "",
            "**样本构成(实测)**:",
            "",
            "| 来源 | 卖出笔数 | 占比 |",
            "|---|---|---|",
            "| `_backup_before_dryrun`(预检回放) | **54** | **95%** |",
            "| `data/state.json`(生产盘) | **3** | 5% |",
            "",
            "⇒ **「中位 1 天」主要来自预检回放, 不能作为生产实盘的证据。**",
            "",
            "**更正后的表述**:",
            "",
            "| 半条结论 | 状态 |",
            "|---|---|",
            "| 「回测无 `min_hold` ⇒ 隐含持有期约 4.1/4.9 天」 | ✅ **仍成立**(代码可证) |",
            "| 「生产实际中位 1 天」 | ❌ **降级为待重测** |",
            "",
            "**为什么这条值得完整留痕**: 它与本仓反复出现的形态同族 ——",
            "**把「看起来像生产数据的文件」当成生产数据**。判据应是",
            "「**这个文件是谁写的、写到哪里、有没有隔离标记**」, 而不是",
            "「它在 `data/` 下、且名字像台账」。",
        ])
        print("appended correction to:", it["id"])

# ---------------------------------------------------------------- 2) 新发现: 台账覆盖
NEW = [
    {
        "id": "FINDING-STATE-LEDGER-TOO-SHORT-FOR-HOLDING-MEASUREMENT",
        "level": "P1",
        "title": ("`data/state.json` 台账只保留最近 3 天(3 笔卖出) ⇒ "
                  "**生产实际持有期当前无法测量**"),
        "status": "open",
        "detail": NL.join([
            "## 实测(2026-09-27)",
            "",
            "| 来源 | 覆盖 | 笔数 | 卖出 |",
            "|---|---|---|---|",
            "| `data/state.json`(生产盘) | **3 天**(09-22..09-24) | 10 | **3** |",
            "| `data/daily/<day>/paper_book.json`(13 个快照) | 各 0 天 | 0 新增 | 0 |",
            "| `data/_backup_before_dryrun/state.json` | 9 天(08-27..09-09) | 112 | 54 |",
            "",
            "**关键**: 第三行是**预检隔离产物**(`preflight_dryrun.json` 记",
            "`tmp_data_dir=...\\Temp\\_interp_parity_B_*` + `prod_state_untouched=True`),",
            "**不能当生产台账**。",
            "",
            "⇒ 排除它之后, 生产盘只有 **3 笔卖出** ⇒ **无法做持有期分布, 也无法做卖出条件归因**。",
            "",
            "## 这意味着什么",
            "",
            "1. 「生产实际持有期落在标的报告的哪一档」**目前无法回答** ——",
            "   而它是 `FUSION_TRIM_Q` 标定的**自变量**;",
            "2. 前几轮据此作出的『1~7 天档 ⇒ 不上线截尾』**其生产数据那一侧不成立**;",
            "   (不过该决策**另有支持**: `_tools/trim_by_horizon.py` 的 h7 最优 q=0.00",
            "    是**回测口径**的实测, 与台账无关。)**决策不变, 但理由要改**。",
            "",
            "## 代码可证的部分(不依赖台账)",
            "",
            "`config.PAPER`: `rebalance_interval_days=3`, `stop_loss=0.03`, `trailing_stop=True`;",
            "`realtime_engine.py`:",
            "",
            "| 序 | 卖出路径 | 行 | 受 `min_hold` 约束 | 受调仓门约束 |",
            "|---|---|---|---|---|",
            "| 1 | 离开目标池 | 951-981 | **是** | **是**(整块在 `if gate_open:`) |",
            "| 2 | 单票止损/移动止损 | 983-1036 | **否** | **否** |",
            "| 3 | 组合级风控 | 1038-1049 | **否** | **否** |",
            "",
            "⇒ **`min_hold=2` 在实践中几乎拦不住任何东西**: 调仓门每 **3** 自然日才开一次,",
            "窗口一开, 最早买入的仓位也已持满 **>=3 天** > `min_hold=2`。",
            "⇒ **真正的持有期下限由止损线(-3%, 移动止损回吐 6%)决定**, 不是 `min_hold`。",
        ]),
        "evidence": NL.join([
            "`_tools/provenance_check.py`(可复现): 报两个来源规模、天数重叠、live 卖出明细;",
            "`data/preflight_dryrun.json` 的 `tmp_data_dir` / `prod_state_untouched` / md5 一致;",
            "`src/realtime_engine.py` L922-933(调仓门) / L951-981 / L983-1036 / L1038-1049;",
            "`src/config.py` L256 `stop_loss`、L265 `trailing_stop`、L272 `trailing_giveback`、",
            "L304 `rebalance_interval_days=3`。",
        ]),
        "next": NL.join([
            "① **不要**再用 `_backup_before_dryrun/` 当生产台账(它是预检隔离产物);",
            "② 若要测生产持有期, 需让 `state.json` 台账**累积更长时间**(或从别处导出更长流水) ——",
            "   当前仅 3 天;",
            "③ **决策不变但理由要改**: `FUSION_TRIM_Q=0` 的依据回到**回测口径**",
            "   (`trim_by_horizon.py` 的 h7 最优 q=0.00), 而不是「生产持有期 1 天」;",
            "④ 若要让 `min_hold` 真正生效, 需要它 **> `rebalance_interval_days`(=3)**, ",
            "   或把止损也纳入 min_hold 约束(设计取舍, 需用户决策)。",
        ]),
    },
]

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

print("added:", added, "| total items:", len(items))
