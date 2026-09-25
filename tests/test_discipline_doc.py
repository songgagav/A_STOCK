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


class TestCrossCuttingPrinciples:
    """跨形态的**共同原则**必须写在章首, 且钉死 (2026-09-25 用户要求)。

    用户的原话: 「建议把『依赖对照而非注意力』写入 DISC-2 章首 ——
    它是所有形态的共同原则, 也是唯一能对抗『注意力耗尽』的方法。」

    **为什么单独守卫它**: 索引表列的是"查哪几种"(战术), 而这条是"凭什么能防住"(战略)。
    它一旦被删或退化, 六种形态就退回成六条"靠更小心"的建议 —— 而**建议不防任何东西**。
    """

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_disc2_opens_with_the_shared_principle(self):
        src = self._src()
        i = src.find("## DISC-2:")
        assert i > 0
        # 原则必须在索引**之前**(章首), 否则读者先看到战术表、看不到依据
        i_principle = src.find("依赖一次对照, 不依赖注意力", i)
        i_index = src.find("### 失效形态索引", i)
        assert i_principle > 0, "DISC-2 章首缺「依赖一次对照, 不依赖注意力」"
        assert i_index > 0
        assert i_principle < i_index, "共同原则必须排在失效形态索引**之前**"

    def test_principle_says_why_attention_fails(self):
        """必须写清**为什么**: 注意力会耗尽且耗尽时不报警 —— 否则它只是句口号。"""
        src = self._src()
        i = src.find("依赖一次对照, 不依赖注意力")
        block = src[i:i + 3600]
        assert "耗尽" in block, "未说明注意力会耗尽"
        assert "不报警" in block or "不报" in block, (
            "未说明注意力耗尽时**不报警** —— 这正是它与本纪律要防的东西同源的原因")
        assert "同一个病" in block, "未点出它与所防之病同源"

    def test_principle_gives_a_contrast_table_for_every_form(self):
        """六种形态**每一种**都要有「靠注意力 ❌ vs 一次对照 ✅」的对照。

        为什么逐种都要求: 只写一句原则、不落到每种形态上, 读者会认同原则、
        然后回到"靠注意"的老做法。**对照表是原则与行动的接口。**
        """
        src = self._src()
        i = src.find("依赖一次对照, 不依赖注意力")
        j = src.find("排查入口(先读这一行)", i)
        assert j > i, "找不到原则块的结束边界"
        block = src[i:j]
        # 注意: 这块整体在 Markdown **引用块**里, 故每行前缀是 `> ` ——
        # 断言里必须带上它(我第一次就漏了, 于是 `| ① ` 匹配不到而失败)。
        for f in ("①", "②", "③", "④", "⑤", "⑥"):
            assert (f"> | {f} ") in block, (
                f"共同原则的对照表里缺形态 {f}(行首应为 `> | {f} `)")
        # ❌/✅ 只出现在**表头**的两个列名里(每行不再重复) —— 故断言"表头有这两列",
        # 而不是"每行都有"。我第一次写成 `count >= 6`, 实测是 1/1, 属想当然。
        header = [ln for ln in block.splitlines() if ln.startswith("> | 形态")]
        assert header, "找不到对照表的表头行"
        assert "❌" in header[0] and "✅" in header[0], (
            f"表头应含有 ❌/✅ 两列: {header[0][:120]}")

    def test_principle_has_an_operational_self_check(self):
        """必须给一条**可执行的自检**, 而不是停在原则上。"""
        src = self._src()
        i = src.find("依赖一次对照, 不依赖注意力")
        block = src[i:i + 3600]
        assert "注意" in block and "小心" in block, "自检应点名那些无用的词"
        assert "改写" in block or "对照动作" in block, "自检应要求把它改写成对照动作"


class TestDisc1LoggingDiscipline:
    """DISC-1 同族纪律: 留痕字段**宁可 None, 不猜** (2026-09-25 用户要求)。"""

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_rule_present_under_disc1(self):
        src = self._src()
        i = src.find("## DISC-1:")
        j = src.find("## DISC-2:")
        assert i > 0 and j > i
        block = src[i:j]
        assert "宁可 None, 不猜" in block, "DISC-1 下缺「留痕字段宁可 None, 不猜」"
        assert "留痕" in block, "应明确它管的是留痕/上报字段"

    def test_rule_explains_why_guessing_is_worse(self):
        """必须说清**为什么猜的日期比 None 危险** —— 否则会被当成"太保守"。"""
        src = self._src()
        i = src.find("宁可 None, 不猜")
        block = src[i:i + 2600]
        assert "事后追溯" in block or "唯一依据" in block, (
            "未说明留痕是事后追溯的唯一依据")
        assert "当成事实" in block, "未说明猜测会被当成事实"
        assert "沿用上次" in block or "last-known" in block, (
            "应显式否掉「沿用上次值」这个常见做法 —— 否则后人会顺手加上")

    def test_rule_links_to_the_implementation_and_guards(self):
        """必须点名实现与守卫 —— 文档说"有守卫"而守卫不存在, 就是文件在撒谎。"""
        src = self._src()
        i = src.find("宁可 None, 不猜")
        block = src[i:i + 2600]
        assert "_engine_day" in block and "_h5i_watermark" in block, (
            "应点名实现(`_engine_day` / `_h5i_watermark`)")
        for fn in ("test_helpers_never_invent_a_date", "test_helpers_tolerate_exceptions"):
            assert fn in block, f"应点名守卫 {fn}"
        # 守卫必须真实存在(文档指向已删除的东西是最常见的腐烂)
        import test_baostock_backfill as T
        cls = T.TestProvenanceCarriesTheSemanticClarification
        for fn in ("test_helpers_never_invent_a_date", "test_helpers_tolerate_exceptions"):
            assert hasattr(cls, fn), f"文档点名了 {fn}, 但它不存在"


class TestBackfillTwoWayVerificationIsDocumented:
    """回填后**必须同时**验证两件事 —— 且两件是相反的期望 (2026-09-25 用户要求)。"""

    _DOC2 = os.path.join(_REPO, "docs", "stockdb-source-status.md")

    def _src(self):
        return open(self._DOC2, encoding="utf-8").read()

    def test_section_present(self):
        src = self._src()
        assert "验证两件事" in src, (
            "stockdb-source-status.md 缺「回填后需同时验证两件事」小节")

    def test_both_expectations_are_stated_and_are_opposite(self):
        """两件事的**期望值必须相反** —— 这是该小节的全部价值。"""
        src = self._src()
        i = src.find("验证两件事")
        assert i > 0
        block = src[i:i + 2600]
        assert "h5i" in block and "引擎探针" in block, "应分别列出 h5i 与引擎探针两项"
        assert "相反" in block or "同时成立才是正确" in block, (
            "必须点明两件期望是**相反**的、且同时成立才对 —— "
            "否则读者仍会只验一件")
        assert "不代表失败" in block or "本来就不会改善" in block, (
            "必须预先否掉「data_lag_days 没改善 = 回填失败」这个误读")

    def test_names_the_provenance_fields_that_record_it(self):
        """应指向留痕里那两个字段 —— 让读的人能自己追溯。"""
        src = self._src()
        i = src.find("验证两件事")
        block = src[i:i + 2600]
        assert "engine_probe_unchanged" in block, "未指向 engine_probe_unchanged"
        assert "affects_selection" in block, "未指向 affects_selection"


class TestErrorMagnitudeIsNotAttributionBasis:
    """『错误的规模不作归因依据』必须写进 ⑤ 子节 (2026-09-25 用户要求)。"""

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_rule_present_in_form5_block(self):
        src = self._src()
        i = src.find("### ⑤ 归因在中间层丢失")
        j = src.find("### ⑥ ", i)
        assert i > 0 and j > i
        block = src[i:j]
        # 实际措辞是 `错误的"规模"不作归因依据`(**带引号**) —— 我第一次写成不带引号的
        # `规模不作归因依据` 就匹配不到。教训: 断言文档时, 搜索串要**照抄原文**,
        # 不要凭记忆写。
        assert "不作归因依据" in block, "⑤ 子节缺「规模不作归因依据」"
        assert "只作优先级依据" in block, "应说明规模只能作**优先级**依据"

    def test_uses_the_two_real_magnitudes_as_evidence(self):
        """必须用本仓**真实**的两个量级作对照(28 股 vs 100 倍), 而不是泛泛而谈。"""
        src = self._src()
        i = src.find("不作归因依据")
        assert i > 0, "找不到该小节"
        block = src[i:i + 4200]
        assert "28" in block, "缺 28 股那个极小差异的实例"
        assert "100" in block, "缺 100 倍那个极大差异的实例"
        assert "机制" in block and "结构" in block, (
            "应给出替代判据(机制可否解释 / 是否稳定结构)")

    def test_uses_the_12_of_5212_case_where_magnitude_misled_twice(self):
        """本仓最有说服力的实例: 12/5212 —— **规模给了两次相反的暗示, 两次都错**。

        小规模 ⇒ "应该没事"(会放行真缺口); 中等比例 ⇒ "整日废掉"(会丢 5200 行)。
        必须写进去, 因为它是唯一一个"两个方向都被规模骗到"的实例。
        """
        src = self._src()
        i = src.find("不作归因依据")
        assert i > 0
        block = src[i:i + 4200]
        assert "12" in block and "5212" in block, "缺 12/5212 那个实例"
        assert "停牌" in block, "应说明这 12 行查出来是停牌(正常状态)"


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
