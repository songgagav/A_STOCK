# -*- coding: utf-8 -*-
"""`tests/doc_section.py` 的守卫 —— 工具本身也必须被验证, 否则它只是"看起来在帮忙"。

## 为什么工具要单独测 (本仓纪律: 守卫的负样本必须能触发守卫)

`doc_section` 是用来**根治**一类守卫失效的(锚点被引用劫持 / 魔数窗口)。
若它自己写错, 那么所有改用它的小节守卫会**一起静默失效** ——
比不改用更糟, 因为"看起来已经根治了"。故这里必须有:
1. 正例(能取到正确区间);
2. **反例**(必须能抓到"引用劫持"这一真实形态);
3. 边界情形(未找到时**抛错而不是返回 -1**)。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doc_section import (  # noqa: E402
    find_unique,
    headings_in,
    line_of,
    section_bounds,
)

#: 一份**刻意**含"引用劫持"的最小文档:
#: 第 3 行在**别的小节里**引用了第 8 行那句关键话。
DOC = """# 标题

## 甲节

> 顺带提一下 留痕字段宁可留空不猜 这条原则。

正文甲。

## 乙节

### 留痕字段: 要么如实, 要么留空

关键句在这里: 留痕字段宁可留空不猜, 因为它是事后追溯的唯一依据。

正文乙。

## 丙节

其它。
"""


class TestSectionBounds:
    def test_returns_the_section_not_the_whole_tail(self):
        a, b = section_bounds(DOC, "## 甲节")
        seg = DOC[a:b]
        assert seg.startswith("## 甲节")
        assert "正文甲" in seg
        assert "## 乙节" not in seg, "区间应止于下一个同级标题"
        assert "正文丙" not in seg and "其它" not in seg

    def test_stops_at_same_or_higher_level(self):
        # `## 乙节` 内部有 `### 留痕字段…`, 应被**包含**在乙节里(更深的层级)
        a, b = section_bounds(DOC, "## 乙节")
        seg = DOC[a:b]
        assert "### 留痕字段" in seg, "更深层级的子标题应包含在本节内"
        assert "正文乙" in seg
        assert "## 丙节" not in seg, "同级标题应结束本节"

    def test_subsection_bounds_stop_at_next_sibling(self):
        a, b = section_bounds(DOC, "### 留痕字段")
        seg = DOC[a:b]
        assert seg.startswith("### 留痕字段")
        assert "关键句在这里" in seg
        # [自查] 不能用 `"正文乙" not in seg` —— 该串在**标题之前**也出现过
        # (乙节的引言), 故它会命中那个更早的实例 ⇒ 误报。
        # 判据必须落在**区间之后**的内容上。
        assert "## 丙节" not in seg, "同级/更高级标题都应结束子节"
        assert "其它。" not in seg, "丙节的内容不该进来"

    def test_fenced_code_blocks_are_not_headings(self):
        """**围栏代码块里的 `#` 不是标题** —— 实测踩过, 见 `_iter_heading_lines` 注释。

        若不排除, 代码块里的 shell 注释会被当成 L1 标题 ⇒
        **提前结束所在小节** ⇒ 该节的守卫在错误区间里取证(静默)。
        """
        doc = (
            "## 甲节\n\n"
            "说明文字。\n\n"
            "```powershell\n"
            "# 这是 shell 注释, 不是标题\n"
            "Write-Host 'x'\n"
            "```\n\n"
            "甲节的后半段。\n\n"
            "## 乙节\n"
        )
        a, b = section_bounds(doc, "## 甲节")
        seg = doc[a:b]
        assert "甲节的后半段" in seg, (
            "代码块里的 `#` 被当成了标题 ⇒ 小节被提前截断")
        assert "## 乙节" not in seg
        # 也不得把它列进标题清单
        assert not any("shell 注释" in line for _, _, line in headings_in(doc))

    def test_tilde_fences_also_excluded(self):
        doc = "## 甲节\n\n~~~\n# 注释\n~~~\n\n后半段。\n\n## 乙节\n"
        a, b = section_bounds(doc, "## 甲节")
        assert "后半段" in doc[a:b]

    def test_heading_prefix_form_works(self):
        """支持行首前缀(如 `"### ⑤ "`), 以便用编号定位而不写全标题。"""
        a, b = section_bounds(DOC, "### 留痕字段:")
        assert DOC[a:b].startswith("### 留痕字段:")

    def test_missing_heading_raises_instead_of_returning_minus_one(self):
        """**核心**: 找不到必须**抛错**, 绝不能返回 -1。

        返回 -1 会让 `src[i:-1]` 静默取到文末 —— 即 DISC-2 形态 ④b「边界腐烂」。
        """
        with pytest.raises(AssertionError) as ei:
            section_bounds(DOC, "## 不存在的节")
        assert "找不到行首标题" in str(ei.value)

    def test_does_not_match_a_phrase_inside_a_quote(self):
        """**反例(本工具存在的理由)**: 引用里的同名字样**不得**被当成标题。"""
        with pytest.raises(AssertionError):
            section_bounds(DOC, "### 引用里假装是标题的")
        doc = DOC + "\n> ### 引用里假装是标题的\n> 内容\n"
        with pytest.raises(AssertionError):
            section_bounds(doc, "### 引用里假装是标题的")

    def test_does_not_match_a_heading_like_string_mid_line(self):
        """行内的 `### …`(表格格子/句子中间)也不得被当成标题。"""
        doc = DOC + "\n| 形态 | 检查方法 |\n|---|---|\n| ④b | 用 `### 详细判据` 常量 |\n"
        with pytest.raises(AssertionError):
            section_bounds(doc, "### 详细判据")

    def test_does_not_match_a_longer_heading_by_prefix(self):
        """`## 甲乙节` 不得被 `## 甲节` 命中(避免前缀误配)。"""
        doc = DOC.replace("## 甲节", "## 甲节").replace("## 丙节", "## 丙节长名字")
        a, b = section_bounds(doc, "## 丙节")
        assert DOC[a:b] is not None  # 仅确认不抛
        # 真正的检查: 用 `## 甲节` 时不应命中 `## 甲节延伸`
        doc2 = doc.replace("## 乙节", "## 甲节延伸")
        a2, b2 = section_bounds(doc2, "## 甲节")
        assert "正文甲" in doc2[a2:b2], "起点应落在真正的 `## 甲节` 上"

    def test_start_offset_is_respected(self):
        """可限定从某处往后找 —— 用于"某节之后的那一节"。"""
        i = DOC.find("## 乙节")
        a, b = section_bounds(DOC, "## 丙节", start=i)
        assert DOC[a:b].startswith("## 丙节")

    def test_the_real_hijack_scenario(self):
        """**本工具要根治的真实形态**: 用关键句定位 vs 用标题定位。

        构造: 关键句在"甲节"里被**引用**了一次(更靠前), 真正的定义在"乙节"。
        · 旧写法 `src.find(关键句)` ⇒ 命中甲节的引用(错);
        · 新写法 `section_bounds("## 乙节")` ⇒ 命中真正的定义(对)。
        """
        key = "留痕字段宁可留空不猜"
        old_way = DOC.find(key)
        a, b = section_bounds(DOC, "## 乙节")
        assert old_way < a, "前提: 引用确实出现在更靠前的位置"
        # [自查] 第一版用 `DOC[old_way:old_way+120]` 判"窗口里没有依据句" ——
        # 但样本里那句就在**60 字之后**, 于是把真正的定义也框了进去 ⇒ 误报。
        # 判据改为**落在区间上**: 旧写法的命中点根本不在乙节里。
        assert not (a <= old_way < b), "旧写法应命中甲节的引用(乙节之外)"
        assert "事后追溯的唯一依据" in DOC[a:b], "新写法的区间里有真正的依据句"
        # 甲节(旧写法命中处所属的节)里不该有依据句
        aa, bb = section_bounds(DOC, "## 甲节")
        assert "事后追溯的唯一依据" not in DOC[aa:bb]

    def test_line_of_rejects_out_of_range(self):
        a, b = section_bounds(DOC, "## 甲节")
        assert line_of(DOC, "正文甲", a, b) > 0
        with pytest.raises(AssertionError) as ei:
            line_of(DOC, "其它", a, b)      # 它在丙节里
        assert "找不到" in str(ei.value)


class TestFindUnique:
    def test_accepts_a_unique_needle(self):
        assert find_unique(DOC, "正文乙") == DOC.find("正文乙")

    def test_rejects_a_duplicated_needle(self):
        key = "留痕字段宁可留空不猜"
        assert DOC.count(key) == 2, "样本前提: 该句出现两次"
        with pytest.raises(AssertionError) as ei:
            find_unique(DOC, key)
        assert "出现 2 次" in str(ei.value)

    def test_rejects_a_missing_needle(self):
        with pytest.raises(AssertionError):
            find_unique(DOC, "根本没有这句")


class TestHeadingsIn:
    def test_lists_only_real_headings(self):
        doc = DOC + "\n| x | 用 `### 详细判据` |\n> ### 引用标题\n"
        got = [line for _, _, line in headings_in(doc)]
        assert "# 标题" in got and "## 甲节" in got and "### 留痕字段: 要么如实, 要么留空" in got
        assert not any("引用标题" in g for g in got), "引用行里的标题不算"
        assert not any("详细判据" in g for g in got), "行内 `### ` 不算"

    def test_levels_are_correct(self):
        lv = {line: lvl for _, lvl, line in headings_in(DOC)}
        assert lv["# 标题"] == 1
        assert lv["## 甲节"] == 2
        assert lv["### 留痕字段: 要么如实, 要么留空"] == 3


class TestItIsUsedOnTheRealDoc:
    """工具必须**真的用在真文档上** —— 只在小样本上通过不算。"""

    _DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "disciplines.md")

    def _src(self):
        return open(self._DOC, encoding="utf-8").read()

    def test_disc1_section_is_reachable_by_heading(self):
        src = self._src()
        a, b = section_bounds(src, "## DISC-1:")
        seg = src[a:b]
        assert "宁可 None, 不猜" in seg, "DISC-1 的留痕小节应在 DISC-1 区间内"
        assert "_engine_day" in seg and "_h5i_watermark" in seg
        assert "## DISC-2:" not in seg, "区间应止于 DISC-2"

    def test_the_old_phrase_anchor_is_demonstrably_wrong(self):
        """在**真文档**上证明旧写法会被劫持 —— 这就是本次要根治的证据。

        做法: 在 DISC-1 **之前**插一行"引用该句"的文字(模拟写文档时的自然冲动),
        然后看两种定位法各自指到哪儿。

        [2026-09-26 自查] 本用例第一版写成
        `assert not (a < hijacked.find(key) < b)` —— 插入了文字之后**偏移全变了**,
        旧的 `a` 已经不对, 那个断言恒真。**这正是 ④b 的同族**:
        区间变了却还在用旧区间。故现在改为**重新计算两侧区间**再比较。
        """
        src = self._src()
        key = "宁可 None, 不猜"
        a, b = section_bounds(src, "## DISC-1:")
        # 现状: 唯一且在 DISC-1 内(过渡期守卫在保它)
        assert src.count(key) == 1, "前提: 该句当前唯一"
        assert a < src.find(key) < b

        # 模拟"更靠前的引用"
        hijacked = src[:a] + f"> 引用: {key} —— 见 DISC-1\n" + src[a:]
        # 旧写法: 命中那行引用, 落在 DISC-1 **之外**
        a2, b2 = section_bounds(hijacked, "## DISC-1:")
        old_hit = hijacked.find(key)
        assert not (a2 <= old_hit < b2), (
            "旧写法(关键句首次出现)应被劫持到 DISC-1 之外")
        # 新写法: 区间仍然正确, 且区内仍含该句
        assert key in hijacked[a2:b2], "新写法不受引用影响"
