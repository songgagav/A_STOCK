# -*- coding: utf-8 -*-
"""纪律文件自身的守卫 (2026-09-22)。

## 为什么"纪律文件"也需要守卫

一份纪律如果只是**写着**而没有任何东西在检查它, 它就会腐烂成一段漂亮文字 ——
这与本仓反复出现的「看起来做了 vs 实际生效」是同一族。

故这里锁两件事:
  1. **纪律文件存在且被引用** —— 否则下一个人找不到它;
  2. **DISC-1 的每条执行机制都真的存在** —— 文件里声明"三层强制",
     那三层就必须能在代码里**点到名**(否则文件在撒谎)。

第 2 条是关键: 它把"文档说有三层"变成"测试证明有三层"。
"""
from __future__ import annotations

import inspect
import os
import re
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

_DOC = os.path.join(_REPO, "docs", "disciplines.md")


class TestDisciplineDocExists:
    def test_doc_present(self):
        assert os.path.isfile(_DOC), f"纪律文件不见了: {_DOC}"

    def test_declares_both_method1_and_disc1(self):
        src = open(_DOC, encoding="utf-8").read()
        assert "METHOD-1" in src, "纪律文件必须与 METHOD-1 并列(用户指定)"
        assert "DISC-1" in src, "缺少 DISC-1(价格衍生指标取自源)"

    def test_points_to_method1_original_not_a_copy(self):
        """**不得复制 METHOD-1 正文** —— 两处副本必然漂移。

        正确做法是引用原处(`docs/drl-learning-verification.md` §📌)。
        """
        src = open(_DOC, encoding="utf-8").read()
        assert "drl-learning-verification.md" in src, \
            "纪律文件应**引用** METHOD-1 原处, 而不是另抄一份"
        assert "不复制其正文" in src or "避免两处漂移" in src, \
            "应写明为什么不复制(否则后人会「顺手补全」)"


class TestDisc1MechanismsActuallyExist:
    """DISC-1 声明"三层强制", 三层必须都能在代码里点到名。"""

    def test_layer1_request_enforces_required_fields(self):
        import baostock_adapter as BA
        assert BA.REQUIRED_FIELDS, "缺 REQUIRED_FIELDS"
        for f in BA.REQUIRED_FIELDS:
            assert f in BA.FIELDS.split(","), f"{f} 不在 FIELDS 里"
        # 缺字段必须抛 ValueError —— 静态确认判据在源码里
        src = inspect.getsource(BA.make_baostock_fetcher)
        assert "ValueError" in src and "REQUIRED_FIELDS" in src, \
            "请求层没有「缺字段即报错」的判据"

    def test_layer2_mapping_covers_every_source(self):
        import bars_ingest as BI
        # 每个已登记源都必须把"涨跌幅"映射到 change_pct
        for name, spec in BI.SOURCE_SPECS.items():
            vals = set(spec["field_map"].values())
            assert "change_pct" in vals, \
                f"源 {name} 未把涨跌幅映射到 change_pct ⇒ 下游会自算(除权日错)"

    def test_layer2_normalize_forbids_price_derivation(self):
        import bars_ingest as BI
        src = inspect.getsource(BI.normalize)
        for bad in ("pct_change(", "shift(1)", "pct_chg ="):
            assert bad not in src, f"normalize 出现价格推导 {bad!r}"

    def test_layer3_cross_validate_compares_change_pct(self):
        import cross_validate as CV
        src = inspect.getsource(CV.cross_validate)
        assert "change_pct" in src, "校验层没有比对 change_pct"

    def test_doc_checklist_matches_the_sources_it_names(self):
        """文件里点名的模块/常量必须真实存在 —— 防止文档指向已改名/删除的东西。"""
        src = open(_DOC, encoding="utf-8").read()
        import baostock_adapter as BA
        import cross_validate as CV
        for token, obj in (("REQUIRED_FIELDS", BA.REQUIRED_FIELDS),
                           ("EXPECTED_VOLUME_RATIO", CV.EXPECTED_VOLUME_RATIO)):
            assert token in src, f"纪律文件应点名 {token}"
            assert obj, f"{token} 为空"
        # 点名的测试函数必须真实存在
        import test_baostock_adapter as T  # noqa: E402
        for fn in ("test_selfcomputed_value_would_be_caught",
                   "test_normalize_never_derives_change_pct",
                   "test_all_sources_that_have_the_field_declare_it"):
            assert fn in src, f"纪律文件应点名守卫 {fn}"
            assert hasattr(T.TestFactorComputationDiscipline, fn), \
                f"纪律文件点名了 {fn}, 但它不存在 —— 文档在撒谎"

    def test_disc2_disc3_disc4_are_formalized_with_mechanisms(self):
        """DISC-2/3/4 已**正式固化**, 各自必须带"执行机制"。

        它们原本是**标明为「候选」**的空位。用户要求正式固化 ——
        固化即意味着: 不再只是想法, 而要能指出**在哪儿被执行**。
        """
        src = open(_DOC, encoding="utf-8").read()
        for n in ("DISC-2", "DISC-3", "DISC-4"):
            assert n in src, f"缺 {n}"
            idx = src.find(f"## {n}")
            assert idx >= 0, f"{n} 没有正式小节标题(可能仍留在候选区)"
            body = src[idx:idx + 2500]
            assert "执行机制" in body, f"{n} 缺「执行机制」—— 固化必须能指到执行处"
            assert "候选" not in src[:idx].split("## ")[-1][:40], \
                f"{n} 仍被标为候选, 但用户已要求正式固化"

    def test_no_leftover_candidate_section(self):
        """候选区若已清空, 不应留下"待登记的纪律"这个空标题。"""
        src = open(_DOC, encoding="utf-8").read()
        if "待登记的纪律" in src:
            tail = src[src.find("待登记的纪律"):]
            assert "候选" in tail, \
                "留着「待登记的纪律」标题但里面没有候选 —— 空标题会误导"

    def test_disc4_points_at_the_implementation(self):
        """DISC-4 必须点名它的实现, 而不只是描述原则。"""
        src = open(_DOC, encoding="utf-8").read()
        assert "_tools/safe_write.py" in src, "DISC-4 未点名生成端实现"
        assert "write_python" in src, "DISC-4 未点名辅助函数"

    def test_disc4_has_a_fixer_not_only_a_validator(self):
        """DISC-4 必须**同时有修复端** —— 这是本会话追加的教训。

        校验端只能说"错了"; 而报错行常不是肇事行, 于是仍是低效的手工循环。
        修复端把它变成一条命令。
        """
        src = open(_DOC, encoding="utf-8").read()
        assert "_tools/fix_cjk_quotes.py" in src, "DISC-4 未点名修复端"
        assert "fix_cjk_quotes" in src
        p = os.path.join(_REPO, "_tools", "fix_cjk_quotes.py")
        assert os.path.isfile(p), f"缺修复端实现: {p}"

    def test_fixer_fixes_real_cases_and_leaves_correct_code_alone(self):
        """修复端**实测**: 能修真实历史样本, 且**不碰**本就正确的代码。"""
        import importlib.util
        p = os.path.join(_REPO, "_tools", "fix_cjk_quotes.py")
        spec = importlib.util.spec_from_file_location("fxq", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 真实历史样本(本会话第 1 次与第 8 次踩到的形态)
        for bad, want in (
            ('assert x, "未声明"不告警""', "未声明「不告警」"),
            ('x = "与"某个 bug 怎么修的"不同"', "与「某个 bug 怎么修的」不同"),
        ):
            new, n, ok = mod.fix_text(bad)
            assert ok, f"修复后仍未通过: {new!r}"
            assert n == 1, f"应恰好修 1 处, 实为 {n}"
            assert want in new, new
            # 修完后, 每个字符串字面量**内部**都不该再有半角引号。
            # 注意判据要精确: "紧贴中文"太宽 —— 合法的**界定符**同样紧贴中文
            # (如 `"未声明…"`), 故必须按字面量拆分后再看内部(首版在此假红)。
            import ast as _ast
            for node in _ast.walk(_ast.parse(new)):
                if isinstance(node, _ast.Constant) and isinstance(node.value, str):
                    inner_q = '"' in node.value
                    assert not inner_q, \
                        f"字面量内部仍有半角引号: {node.value!r} (源码 {new!r})"
        # **不得碰**本就正确的代码
        good = 'x = "中文「这样」就对了"\n'
        new, n, ok = mod.fix_text(good)
        assert ok and n == 0 and new == good, "修复端改动了本就正确的代码"

    def test_disc4_implementation_exists_and_refuses_bad_syntax(self):
        """实现必须真实存在, 且**语法不过时拒绝落盘**。"""
        import importlib.util
        import tempfile
        p = os.path.join(_REPO, "_tools", "safe_write.py")
        assert os.path.isfile(p), f"缺实现: {p}"
        spec = importlib.util.spec_from_file_location("safe_write", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 坏语法必须抛, 且**不落盘**
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "bad.py")
            bad = 'x = "中文"里嵌引号" 后面还有"\n'
            with pytest.raises(mod.PythonSyntaxInvalid):
                mod.write_python(target, bad)
            assert not os.path.exists(target), \
                "语法不过却落盘了 —— 会留下坏文件给别人导入"
            # 好语法正常落盘
            good = 'x = "中文「这样」就对了"\n'
            mod.write_python(target, good)
            assert os.path.isfile(target)

    def test_safe_write_error_message_points_at_the_real_cause(self):
        """报错必须提示"报错行常不是肇事行" —— 否则人会一直改错行。"""
        import importlib.util
        p = os.path.join(_REPO, "_tools", "safe_write.py")
        spec = importlib.util.spec_from_file_location("safe_write2", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with pytest.raises(mod.PythonSyntaxInvalid) as ei:
            mod.validate_python('a = 1\nb = "中文"嵌引号"\n')
        msg = str(ei.value)
        assert "不是肇事行" in msg, "报错未指向真因"
        assert "「」" in msg, "报错未给出修法"


class TestDisc2FormIndex:
    """DISC-2 的「失效形态索引」必须与详细章节**对得上** (2026-09-23, 用户建议)。

    用户建议把 5 种形态列成一张带**排查优先级**的表。索引的价值全在"能扫一眼决定
    先查哪个"; 一旦它与下面的详细章节脱节(改了形态却没改索引, 或反之),
    它就从"索引"退化成"另一段会腐烂的文字" —— 而**索引腐烂比没有索引更糟**,
    因为它会让人以为已经覆盖了, 实际指向的是过期的形态清单。
    """

    _FORMS = ("①", "②", "③", "④", "⑤", "⑥")

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_index_section_exists_and_precedes_details(self):
        src = self._src()
        i_idx = src.find("### 失效形态索引")
        assert i_idx > 0, "缺「失效形态索引」小节"
        i_detail = src.find("### 详细判据")
        assert i_detail > 0, "缺详细章节(标题应以「### 详细判据」开头)"
        assert i_idx < i_detail, (
            "索引必须排在详细章节**之前** —— 它的用途就是"
            "让人先决定查哪个, 再往下读细节")

    def test_disc2_top_has_a_navigation_pointer_to_the_index(self):
        """DISC-2 章首必须有指向索引的**排查入口**, 且索引**紧邻其后**。

        为什么这条单独锁: DISC-2 章首原本紧跟一张**很长的实例表**(六条真实事故)。
        带症状来排查的人会从章首往下读, 于是**先读完整张历史档案**才可能碰到索引 ——
        那索引等于没写。

        [2026-09-23 两处自查]
        ① 第一版用 `src.find("失效形态索引")` 取索引位置, 但**章首的入口本身就含
           这五个字** ⇒ 取到指针自己 ⇒ 切片为空。教训: 断"某段在另一段之前"时,
           边界关键词**不能是该引用自身的可见文字**, 要用小节标题。
        ② 第二版只断言"入口在索引之前" —— 入口在章首、索引在章中段, 那条**恒真**,
           真正的风险(中间夹着实例表)**抓不到**。故现在改成断言**紧邻**:
           入口与索引之间**不得**出现其它小节标题。
        """
        src = self._src()
        i_disc2 = src.find("## DISC-2:")
        assert i_disc2 > 0
        i_idx_head = src.find("### 失效形态索引", i_disc2)
        assert i_idx_head > i_disc2, "找不到索引小节标题"
        head = src[i_disc2:i_idx_head]
        assert "排查入口" in head, (
            "DISC-2 章首没有指向索引的排查入口 —— "
            "读者会先逐条读六条事故实例, 才可能碰到索引")
        assert "失效形态索引" in head, "入口应点名索引小节"
        assert "优先级" in head and "检查方法" in head, (
            "入口应说明索引里有什么(优先级/检查方法), 否则没人会跳过去")
        # **核心**: 入口与索引之间不得夹任何其它小节 —— 否则"入口"要跨过它才到得了索引
        for sub in ("### 事实依据", "### 关于", "### 四种"):
            assert sub not in head, (
                f"入口与索引之间夹着 `{sub}` —— 入口必须紧邻索引; "
                "读者会先读完那一节才看到索引, 入口形同虚设")

    def test_index_covers_exactly_the_six_forms(self):
        """索引表必须**恰好**列出 6 种形态, 不多不少。

        多列 = 索引里有详细章节没写的形态(读者找不到细节);
        少列 = 新形态只写进正文却没进索引(读者扫不到)。
        两个方向都要拦。

        [2026-09-25] 用户要求补第 ⑥ 种「降级过程无告警」, 故由五种扩到六种。
        **这条断言必须跟着改** —— 若只改文档不改它, 用例会以"找不到 ⑥"失败
        (这是好事: 说明守卫真的在盯索引与正文的一致性)。
        """
        src = self._src()
        i = src.find("### 失效形态索引")
        j = src.find("### 详细判据", i)
        index_block = src[i:j]
        for f in self._FORMS:
            assert f in index_block, f"索引里缺形态 {f}"
        # 详细章节那边也必须六种都在: 前四种在对照表里以 `| **① ` 起行,
        # ⑤⑥ 各有独立的 `### ⑤ ...` / `### ⑥ ...` 小节标题。
        detail = src[j:]
        for f in self._FORMS:
            assert f"**{f}" in detail or f"### {f}" in detail, \
                f"详细章节里找不到形态 {f}"

    def test_index_marks_three_forms_as_high_priority(self):
        """④/⑤/⑥ 必须被标为**高**优先级, 且理由写出来(用户指定)。

        为什么单锁这几个字符: 用户的原话是「⑤ 和 ④ 应该优先检查 —— 它们最难发现,
        且会把人引向错误方向」, 后又在 2026-09-25 要求补 ⑥。
        若将来有人"顺手"把优先级都抹平, 这张表就只剩装饰作用 ——
        而"没有优先级的索引"与"没有索引"在排查时的效果一样。
        """
        src = self._src()
        i = src.find("### 失效形态索引")
        j = src.find("### 详细判据", i)
        block = src[i:j]
        for f in ("④", "⑤", "⑥"):
            row = [ln for ln in block.splitlines() if ln.strip().startswith(f"| **{f}")]
            assert row, f"索引表里找不到形态 {f} 的行"
            assert "高" in row[0], f"形态 {f} 未被标为高优先级: {row[0][:90]}"
        # 理由必须写明(否则后人不知道为什么高)
        assert "最难" in block or "反方向" in block, \
            "未写出 ④/⑤ 为何优先(它们最难发现 / 会把人引向错误方向)"
        # 低优先级的那个也要有标注, 保证三档都有
        assert "🟢" in block and "🟡" in block and "🔴" in block, \
            "优先级应有三档标记"

    def test_index_names_a_concrete_check_per_form(self):
        """每行必须给出**可执行的检查方法**, 而不是"注意一下"这类空话。

        [2026-09-23 自查] 本用例第一版只断言"整行里含 检查/搜索/问 之一"——
        反证实测它**抓不住**把检查方法改成"注意一下"的变异: 因为那一行的
        **优先级列里也有"检查"二字**, 顺带满足了断言。
        **教训**: 断言要落在**该落的那一列**上, 不是"这一行里有没有这几个字"。
        故这里按 `|` 切列, 只取第 3 列(检查方法)来判。
        """
        src = self._src()
        i = src.find("失效形态索引")
        j = src.find("### 四种", i)
        block = src[i:j]
        for f in self._FORMS:
            rows = [ln for ln in block.splitlines() if ln.strip().startswith(f"| **{f}")]
            assert rows, f"缺形态 {f} 的行"
            cells = [c.strip() for c in rows[0].strip().strip("|").split("|")]
            assert len(cells) >= 3, f"形态 {f} 的行列数不对: {rows[0][:110]}"
            method = cells[2]                    # 第 3 列 = 检查方法
            assert method, f"形态 {f} 的检查方法为空"
            assert any(k in method for k in ("检查", "搜索", "问")), (
                f"形态 {f} 的**检查方法列**不含可执行动作, 像一句空话: {method!r}")
            # 空话黑名单: 这些话看着像建议, 实际无法执行
            for vague in ("注意一下", "关注", "留意", "小心"):
                assert vague not in method, (
                    f"形态 {f} 的检查方法流于空话({vague!r}): {method!r}")
