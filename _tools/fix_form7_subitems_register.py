"""一次性: 登记 2026-09-26 第三批用户清单项(登记册为权威记录)。

用户清单:
  □ DISC-2 补充子条目「结论相同、来由不同处, 必须连判据一起记」(与 5 同族, 更一般化)
  □ DISC-2 7 补充可操作判据「这个判据, 能区分对错吗?」("叙述区段数 <= 4" 是反例)
  □ docs/disciplines.md 环境节补充「凡要交给别的程序读的文件, 写完都验一次首字节」
  □ 确认 4b 的「占幅断言」已加入守卫

顺带登记实现过程中挖出的真问题:
  · 锚点句被引用劫持 ⇒ 两条 DISC-1 守卫以误导性理由失败(守卫自指涉第 5 次)
"""
from __future__ import annotations

import json
import os

FP = os.path.join("ops", "acceptance_status.json")
d = json.load(open(FP, encoding="utf-8"))
items = d["items"]
have = {it["id"] for it in items}

NEW = [
    {
        "id": "ROADMAP-DISC2-SAME-CONCLUSION-MUST-RECORD-CRITERIA",
        "level": "P1",
        "title": "DISC-2 7 补子条目「结论相同、来由不同处, 必须连判据一起记」: 5 的更一般化",
        "status": "fixed",
        "detail": (
            "用户 2026-09-26 要求补, 并指出它与 5 **同族, 但更一般化**。\n\n"
            "## 判据\n\n"
            "> **若同一个结论(状态/动作/返回值)有两个或以上不同来由,**\n"
            "> **记录时必须把<u>判据</u>一起记下来, 不能只记结论。**\n\n"
            "**为什么**: 只记结论 ⇒ 事后**无法重建**当时的局面;\n"
            "而「两个来由」往往对应**相反的运维动作** —— 记不清就会照错的那个去处理。\n\n"
            "## 实例(2026-09-28 回填实验的准备工作)\n\n"
            "`backfill_trigger` 的 `no_gap`(不触发)**有两个来由**, 打印出来完全一样:\n\n"
            "| A_engine_gap | B_h5i_gap | action | 真实含义 |\n|---|---|---|---|\n"
            "| false | 任意 | no_gap | **厂商已恢复** ⇒ 没有「厂商缺席的日子」要补 |\n"
            "| true | false | no_gap | **厂商未恢复, 但 h5i 已补齐** ⇒ 重复回填无意义 |\n"
            "| true | true | **trigger** | 真正要回填的缺口 |\n\n"
            "前两行含义相反(一个「厂商好了什么都不用做」, 一个「厂商还坏着只是存储不缺」)\n"
            "而输出相同。**若记录里只有 action, 一周后分不清是哪种局面** ——\n"
            "而 09-28 那场自然实验要回答的正是这个问题。\n\n"
            "## 与 5 的关系\n\n"
            "| | 5 | 本条 |\n|---|---|---|\n"
            "| 管的是 | **排查方向**(错误信息把人引向错误方向) | **记录完整性**(结论记了, 判据丢了) |\n"
            "| 典型症状 | 越认真读越可能走错方向 | 记录看起来齐全, 但**事后重建不了** |\n"
            "| 对策粒度 | 「保留**每层**的 error 字段」 | 「凡**结论相同、来由不同**处, 连判据一起记」 |\n\n"
            "⇒ **5 是本条在错误传递场景下的特例**: 它要求的「保留每层 error」,\n"
            "本质就是「不要只留最终那个 ok」。本条把它推广到**所有多来由的结论**。\n\n"
            "## 处置\n\n"
            "已在 `docs/stockdb-source-status.md` 6.13 把 **criteria 两值列为检查单强制第 0 项**,\n"
            "6.14 的观测记录按此补记(见 ROADMAP-BACKFILL-AUTO-TRIGGER 批次)。"
        ),
        "evidence": (
            "docs/disciplines.md 在 7 小节内新增"
            "`#### 7 的第三个判据: 结论相同、来由不同处, 必须连判据一起记`; "
            "tests/test_discipline_doc.py 新增 "
            "`TestSameConclusionDifferentReasonsMustRecordCriteria`(子条目在 7 内 / "
            "三行真实判据表 / 与 5 的更一般化关系 / 5 为特例); "
            "另加 `TestDocAnchorPhrasesAreUnique` 锁住锚点句唯一性; "
            "实测 92 passed(test_discipline_doc.py)"
        ),
        "next": (
            "维持。**通用判据**: 记一条结论时问\n"
            "「这个结论还有别的来由吗? 若只看我记下的东西, 别人能区分是哪一个吗?」\n"
            "答不出 ⇒ 把判据一起记。适用于: 动作名 / 状态码 / 返回值 / 结论字段。"
        ),
    },
    {
        "id": "ROADMAP-DISC2-FORM7-DISCRIMINATING-JUDGEMENT-RULE",
        "level": "P1",
        "title": "DISC-2 7 补可操作判据「这个判据, 能区分对错吗?」: 以「叙述区段数 <= 4」为反例",
        "status": "fixed",
        "detail": (
            "用户 2026-09-26 要求补。7 原本只有一条判据(管**证据**), 本条管**判据本身**。\n\n"
            "## 判据\n\n"
            "> **「这条判据, 在「做对了」与「做错了」两种情况下, 结果会不一样吗?」**\n"
            "> · 会不一样 ⇒ 有判别力, 留着;\n"
            "> · **两种情况下结果相同 ⇒ 它是错的判据** —— 不是「不够严」,\n"
            ">   而是**根本不测那件事**。\n\n"
            "## 反例(我自己刚写的, 当场删掉)\n\n"
            "```python\n"
            "assert len(runs) <= 4, \"叙述区被切成了 N 段, 疑似识别过宽\"   # 错的判据\n"
            "```\n\n"
            "**错在哪**: 本文档的引用块(`>`)本来就散落在全文各处, 正常状态下实测 **34 段**\n"
            "—— 即「做对了」也会失败; 而「做错了」(识别过头成一大段)反而**段数变少**,\n"
            "**会通过**。⇒ 它与被判定的那件事**方向相反**, 是个纯噪声判据(既误报又漏报)。\n\n"
            "**替换为**: `sum(flags) < len(lines) * 0.5`(**叙述区占幅 < 半篇**)。\n"
            "理由: 真实 bug 是「识别过头 ⇒ 叙述区被算到几千行」, 占幅会立刻超一半;\n"
            "而正常状态下引用块虽多, 总占比很小 ⇒ 两种情况结果**确实不同**。\n\n"
            "| 判据 | 做对了 | 做错了(识别过头) | 能区分? |\n|---|---|---|---|\n"
            "| 段数 <= 4 | 34 段 ⇒ **失败** | 1 大段 ⇒ **通过** | **不能, 且方向相反** |\n"
            "| 占幅 < 半篇 | 占比小 ⇒ 通过 | 占几千行 ⇒ **失败** | 能 |\n\n"
            "## 与既有两问句的分工\n\n"
            "| 问句 | 管的是 | 失效表现 |\n|---|---|---|\n"
            "| 「反过来才是真的, 证据会不一样吗?」 | **证据的效力** | 拿无判别力的证据当确认(7 本体) |\n"
            "| 「这个判据能区分对错吗?」 | **判据的效力** | 写了一条「两种情况同结果」的断言 |\n\n"
            "两者同源(都在问「能不能分开两种情况」), 但**一个是读证据, 一个是写判据**。\n"
            "后者更常见于**新写的守卫**: 写的时候满脑子「要检查 X」, 于是随手挑了一个\n"
            "**看起来与 X 相关**的量(段数), 而没问它是否**真的随 X 变化**。\n\n"
            "与「负样本必须能触发守卫」是同一件事的两个说法:\n"
            "那条要求你**真的喂**一个负样本; 这条要求你**在写的时候就问**。"
        ),
        "evidence": (
            "docs/disciplines.md 在 7 小节内新增"
            "`#### 7 的第二个判据: 「这个判据, 能区分对错吗?」`, 含反例代码 / "
            "对照表 / 与另两问句的分工表; "
            "对应实现: tests/test_discipline_doc.py 的 "
            "`test_the_narrative_regions_are_detected` 中**删掉了** `len(runs) <= 4` "
            "并改为占幅断言(注释里写明为何删); "
            "新增 `TestJudgementRuleMustDiscriminate`(判据存在 / 反例点名 34 段 / "
            "方向相反 / 给替换判据与对照表 / 与负样本规则挂钩)"
        ),
        "next": (
            "维持。**自查动作**: 写完一条守卫, 先把「做错」的情形在脑子里跑一遍,\n"
            "问「这种情况下, 我这条断言会红吗?」答案是「不会红」或「不确定」⇒\n"
            "这条守卫是**装饰**, 必须换成一个**已知会在那种情形下变化**的量。"
        ),
    },
    {
        "id": "ROADMAP-FIRST-BYTE-VERIFICATION-RULE",
        "level": "P1",
        "title": "环境节固化「凡要交给别的程序读的文件, 写完都验一次首字节」: 本仓已实测两次",
        "status": "fixed",
        "detail": (
            "用户 2026-09-26 要求在 `docs/disciplines.md` 的**环境节**补充这条。\n\n"
            "## 判据\n\n"
            "> 凡是**写给别人读**的文件(开关 / 配置 / 提交信息 / JSON 清单 /\n"
            "> 给子进程的输入), 写完之后**先验首字节, 再交付**: `bytes` / `NULs` / `BOM` 三项。\n\n"
            "**为什么**: 「我写对了内容」与「**文件字节正确**」是**两件事** ——\n"
            "而第二件才是别的程序实际读的东西。\n\n"
            "## 本仓实测两次, 根因同一个(PowerShell 编码默认值 != 无 BOM UTF-8)\n\n"
            "| # | 日期 | 操作 | 实际写出 | 后果 | 发现方式 |\n|---|---|---|---|---|---|\n"
            "| 1 | 2026-09-25 | `Out-File -Encoding UTF8` 写 backfill_switch.json | **带 BOM** | `utf-8` 读抛异常被 except 吞掉 ⇒ **开关被静默读成「未配置」** | 用 `utf-8-sig` 读才复现 |\n"
            "| 2 | 2026-09-26 | `Set-Content -Encoding utf8` 写 git 提交信息 | **含 NUL** | git 拒绝 ⇒ **提交根本没产生**; 而 `git push` 报 `Everything up-to-date`(看起来像已推过) | 字节三连验出 NULs>0 |\n\n"
            "**两次都属于「报告说成功、东西其实没生效」** —— 与 DISC-2 要防的形态同源。\n\n"
            "## 一次性写对 + 读侧防御\n\n"
            "写: `[System.IO.File]::WriteAllText($path, $c, (New-Object System.Text.UTF8Encoding($false)))`\n"
            "验: `bytes` / `NULs` / `BOM` 三项一行;\n"
            "读: 本仓自己写的配置**一律 `encoding=\"utf-8-sig\"`**;\n"
            "**不要把「读失败」吞成默认值**(第 1 次就是被 except 吞掉才变成静默)。"
        ),
        "evidence": (
            "docs/disciplines.md 环境节新增独立小节"
            "`### 「凡要交给别的程序读的文件, 写完都验一次首字节」(2026-09-26 固化)`, "
            "含两次实测对照表 / 一次性写对写法 / 读侧防御 / 推广; "
            "tests/test_discipline_doc.py 新增 `TestFirstBytesMustBeVerified`"
            "(规则在环境节 / 三项验收 / 两次实测都记 / 连带症状 / 写读两侧 / "
            "**反向验证本节不再引用 DISC-1 锚点句**)"
        ),
        "next": (
            "维持。**推广**: 本项目的运维预案本身就在写 `data/` 下的文件, "
            "故「编码/字节」问题的暴露面会随运维成熟而**增加**。\n"
            "**每次新增一个「人写、程序读」的文件, 都要同时写它的字节验收方式。**"
        ),
    },
    {
        "id": "BUG-ANCHOR-PHRASE-HIJACKED-BY-QUOTE",
        "level": "P2",
        "title": "锚点句被「引用」劫持 ⇒ 两条 DISC-1 守卫以误导性理由失败(守卫自指涉第 5 次)",
        "status": "fixed",
        "detail": (
            "## 现象\n\n"
            "新增「验首字节」小节后, **两条 DISC-1 守卫同时失败**:\n"
            "`test_rule_explains_why_guessing_is_worse` 与\n"
            "`test_rule_links_to_the_implementation_and_guards`,\n"
            "失败理由是「未说明留痕是事后追溯的唯一依据」/「应点名实现 `_engine_day`」——\n"
            "**看起来像是 DISC-1 的内容缺了**, 而 DISC-1 一个字都没改。\n\n"
            "## 真因\n\n"
            "两条守卫都用 `src.find(\"宁可 None, 不猜\")` **定位 DISC-1 那一节**,\n"
            "再取其后 2600 字符切片取证。而我在新写的小节里**引用了那句原句**来作类比,\n"
            "且**引用位置更靠前** ⇒ `find` 命中我的引用 ⇒ 切片窗口落在我那段编码说明上\n"
            "⇒ 断言自然找不到 DISC-1 的内容。\n\n"
            "**这是本会话第 5 次「守卫自指涉」**, 也是「识别条件必须覆盖该节所有可能格式」\n"
            "的实例: 用**子串首次出现**定位一节, 会被任何**更靠前的引用**劫持。\n\n"
            "## 处置\n\n"
            "1. 把全文里**所有**对该句的引用改成同义描述, 使其**唯一出现**(实测 count 1);\n"
            "2. 新增 `TestDocAnchorPhrasesAreUnique` 锁住这个不变量 ——\n"
            "   锚点句必须恰好出现一次, 且落在预期小节附近;\n"
            "3. 在「验首字节」小节里**留痕**这次教训(含修法方向: 这类守卫应改为\n"
            "   按**小节标题行首锚定**定位, 而不是按关键句首次出现)。\n\n"
            "**为什么值得单独立条**: 「引用某节的关键句」是写文档时的**自然冲动** ——\n"
            "不立守卫就会再犯; 而它失败时的报错**指向错误的方向**(像是 DISC-1 出了问题),\n"
            "正是 5 号形态。"
        ),
        "evidence": (
            "docs/disciplines.md: 全文对锚点句的引用改为同义描述(实测 `宁可 None, 不猜` "
            "出现次数 1, 位于 DISC-1 留痕小节标题); 新增小节内留痕该教训; "
            "tests/test_discipline_doc.py 新增 `TestDocAnchorPhrasesAreUnique`"
            "(锚点唯一 + 落在预期小节 + 反向样本能造出重复); "
            "并新增 `test_this_doc_does_not_quote_the_anchor_phrase` 反向验证新小节不再引用; "
            "实测修复前 2 failed, 修复后 92 passed"
        ),
        "next": (
            "维持。**修法方向(留给后人)**: 用「某句关键话的首次出现」定位一节**本质上会被引用劫持**;\n"
            "应改为**按小节标题行首锚定**(见 `TestDisc2FormIndex._DETAIL_HEADING` 与\n"
            "`TestSameConclusionDifferentReasonsMustRecordCriteria._form7_bounds` 的写法)。\n"
            "本次先立「锚点唯一」守卫作为过渡 —— 它能挡住再犯, 但不解决根因。"
        ),
    },
]

added = 0
for e in NEW:
    if e["id"] in have:
        print("skip (already present):", e["id"])
        continue
    items.append(e)
    added += 1

d["generated_at"] = "2026-09-26"
with open(FP, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print("added:", added, "| total items:", len(items))
