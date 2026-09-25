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


class TestEveryYamlUsingTestIsMarked:
    """用了 yaml 的用例**必须**挂 `@_needs_yaml` (2026-09-25 用户要求)。

    ## 为什么这条值得单独做成守卫

    2026-09-25 我新加了一条 `test_filter_logic_accepts_per_table_thresholds`,
    它调用 `_load_rules()`(内部 `import yaml`) —— 却**漏了 `@_needs_yaml`**。
    后果: 在 `.venv314`(无 PyYAML)下它**不是 skip 而是 ERROR**,
    把全量测试从 `0 failed` 变成 `1 failed`。

    **新加的守卫自己成了破坏源** —— 这正是本仓 DISC-2 要防的形态, 只是这次
    破坏者是守卫本身。人工核对靠不住(我当时也以为标了), 故做成**静态扫描**:
    凡"用了 yaml"的用例, 装饰器里必须有 `_needs_yaml`。

    ## 判据为什么用 AST 而不是正则

    正则会把**注释里提到 yaml** 也当成"用了 yaml"(本仓注释里大量出现 `alert_rules.yml`),
    于是产出大量假阳性、最后被人加白名单绕过 —— 那等于没有守卫。
    AST 只看**函数体里真实的调用**, 没有这个问题。
    """

    def test_no_yaml_using_case_misses_the_marker(self):
        import ast as _ast
        import glob as _glob
        missing = []
        total = 0
        # 本守卫**自己**也要按同一规则检查 —— 它里面出现了那些模式串(在提示文本里),
        # 若不过滤就会**自我误报**(实测第一次就报了它自己)。
        # 过滤是本测试的一个**输入**(文件名), 不是"跳过检查"的例外。
        _SELF = os.path.basename(__file__)
        for p in sorted(_glob.glob(os.path.join(_REPO, "tests", "*.py"))):
            src = open(p, encoding="utf-8").read()
            if "yaml" not in src:
                continue
            tree = _ast.parse(src)
            for cls in [n for n in _ast.walk(tree) if isinstance(n, _ast.ClassDef)]:
                if cls.name == "TestEveryYamlUsingTestIsMarked":
                    continue          # 见上: 本守卫的提示文本含模式串, 会自我误报
                # 同理: 那个守卫用**样本源码字符串**做反向验证, 样本里必然出现
                # 模式串 —— 它不是在"用 yaml", 而是在**构造关于 yaml 的样本**。
                # 这是守卫自指涉的第三种形态: 判据命中"关于判据的数据"。
                if cls.name == "TestNoTestCallsAnUndefinedHelper":
                    continue
                for fn in [n for n in cls.body if isinstance(n, _ast.FunctionDef)]:
                    body = _ast.get_source_segment(src, fn) or ""
                    uses = (("import yaml" in body) or ("yaml.safe_load" in body)
                            or ("_load_rules(" in body) or ("safe_load(" in body))
                    if not uses:
                        continue
                    total += 1
                    deco = " ".join(_ast.unparse(d) for d in fn.decorator_list)
                    if "_needs_yaml" not in deco:
                        missing.append(f"{os.path.basename(p)}::{cls.name}::{fn.name}")
        assert total > 0, "扫描没找到任何用 yaml 的用例 —— 守卫本身失效了, 需检查判据"
        assert not missing, (
            f"以下用例用了 yaml 但没挂 @_needs_yaml —— 在无 PyYAML 的解释器上它们会"
            f"**ERROR 而不是 skip**, 把全量测试变成 failed ({_SELF} 自身除外):\n  "
            + "\n  ".join(missing))
        # 顺带证明"过滤只排除了本守卫", 而不是把别的也漏掉
        assert total >= 13, f"扫描到的用 yaml 用例只有 {total} 个, 判据可能过窄"

    def test_the_marker_exists_in_files_that_use_it(self):
        """点名的 `_needs_yaml` 必须**真的定义**在用到它的文件里 —— 否则是 NameError。"""
        import glob as _glob
        for p in sorted(_glob.glob(os.path.join(_REPO, "tests", "*.py"))):
            src = open(p, encoding="utf-8").read()
            if "_needs_yaml" not in src:
                continue
            assert "_needs_yaml = pytest.mark.skipif" in src, (
                f"{os.path.basename(p)} 用了 _needs_yaml 但没定义它")


class TestGuardSelfReferenceIsDocumented:
    """DISC-2 ⑥ 的补充实例「**守卫自指涉**」必须写进纪律 (用户 2026-09-25 要求)。

    ## 为什么单列这一条

    它与「守卫依赖缺失」并列但形态不同:

    | 形态 | 现象 |
    |---|---|
    | 守卫依赖缺失 | 守卫**没跑** ⇒ 它保护的路径无人看守 |
    | **守卫自指涉** | 守卫**跑了、也报了**, 但它报的是**它自己** |

    实测: 2026-09-25 新加的静态守卫
    `TestEveryYamlUsingTestIsMarked` **第一次运行就报了它自己** ——
    因为它的**提示文本里**含 `yaml.safe_load` 这个模式串, 而
    `ast.get_source_segment` 返回的片段**包含 docstring**。
    即**"用来描述规则的话"被"规则的判据"当成了输入**。

    **良性与恶性的分界**: 本次它**报错而非静默通过**(良性, 改掉即可);
    但若某守卫的模式串**恰好只匹配它自己**、真实目标一个都不匹配,
    它会**永远通过**并报告"全部合格" —— 那就退化成 ① 假信心测试,
    且更隐蔽: 它看起来真的在检查。
    """

    _DOC_ = _DOC

    def test_documented_under_form6(self):
        src = open(self._DOC_, encoding="utf-8").read()
        i = src.find("### ⑥ 降级过程无告警")
        assert i > 0
        j = src.find("### 关于「已提交 vs 已推送」", i)
        assert j > i, "找不到 ⑥ 节的结束边界"
        block = src[i:j]
        assert "守卫自指涉" in block, "⑥ 节缺「守卫自指涉」这个实例"
        assert "守卫依赖缺失" in block, "应与「守卫依赖缺失」并列对照"

    def _form6_block(self):
        """⑥ 节的正文块 —— 用**标题**定位, 不用关键字(关键字可能先出现在别处)。

        [2026-09-25 自查] 本类原先用 `src.find("守卫自指涉")` 定位,
        而章首「守卫的设计原则」里也提到了"守卫自指涉", 于是**取到了章首那段** ⇒
        断言在一个完全无关的片段上失败(报"缺 docstring")。
        教训与之前几次同源: **定位要用唯一锚点(标题), 不要用可能在多处出现的词**。
        """
        src = open(self._DOC_, encoding="utf-8").read()
        i = src.find("#### ⑥ 的补充实例")
        assert i > 0, "找不到 ⑥ 的补充实例小节"
        j = src.find("### 关于「已提交 vs 已推送」", i)
        assert j > i, "找不到该小节的结束边界"
        return src[i:j]

    def test_explains_the_benign_vs_malignant_boundary(self):
        """必须说清**良性(报错)与恶性(永远通过)**的分界 —— 这才是它的价值。"""
        block = self._form6_block()
        assert "假信心测试" in block, "应点明它可能退化成 ① 假信心测试"
        assert "永远通过" in block, "应说明恶性形态是'永远通过'"
        assert "docstring" in block, "应说明根因(get_source_segment 含 docstring)"

    def test_gives_three_operational_rules(self):
        """必须给出可操作做法, 而不是只描述现象。"""
        block = self._form6_block()
        assert "声明" in block and "排除" in block, (
            "应要求守卫**声明它排除了什么**")
        assert "下限" in block or "total >=" in block or "扫描量" in block, (
            "应要求加一条'扫描量下限'断言, 防止判据意外变窄")
        # 「守不变量」那条设计原则现在住在**章首**(用户要求)—— 故这里断言它
        # 在本小节里被**引用**(指向章首), 而不是要求它复述全文。
        assert "不变量" in block, "应点明'守卫要守不变量, 不要守实现细节'"

    def test_the_described_guard_actually_has_the_safeguards(self):
        """文档描述的那三道保险必须**真的在代码里** —— 否则文件在撒谎。

        这是本仓一贯要求(文档点名的东西必须能被点到名)。
        """
        src = open(os.path.join(_REPO, "tests", "test_discipline_doc.py"),
                   encoding="utf-8").read()
        i = src.find("class TestEveryYamlUsingTestIsMarked")
        assert i > 0
        block = src[i:i + 3000]
        assert "TestEveryYamlUsingTestIsMarked" in block, "自排除必须按**类名**(不是路径)"
        assert "total >=" in block, "缺'扫描量下限'断言"
        assert "本守卫自身除外" in block or "自身除外" in block, (
            "排除必须写在断言文本里(声明的输入, 不是偷偷跳过)")

    def test_three_rows_distinguish_benign_from_malignant(self):
        """表格必须区分**良性/恶性两档** —— 用户指出这个区分是关键。

        两档的后果差一个数量级: 良性**报错 ⇒ 立刻可见**;
        恶性**永远通过 ⇒ 退化成 ① 假信心测试**。
        """
        block = self._form6_block()
        assert "守卫自指涉(良性)" in block, "缺『守卫自指涉(良性)』这一行"
        assert "守卫自指涉(恶性)" in block, "缺『守卫自指涉(恶性)』这一行"
        assert "守卫依赖缺失" in block, "缺『守卫依赖缺失』这一行"
        assert "报的是它自己" in block

    def test_scan_volume_floor_is_called_the_only_gate(self):
        """必须点明「扫描量下限」是防恶性形态的**唯一闸门** (用户原话)。

        为什么单锁这句: 它是**唯一的机制性保险**; 别的都是写法建议,
        而写法建议靠注意力维持 —— 只有这条断言会在判据变窄时**主动失败**。
        """
        block = self._form6_block()
        assert "唯一闸门" in block, "未点明扫描量下限是唯一闸门"
        assert "total >= 13" in block, "未写出具体下限值"
        assert "无人察觉" in block, "应说明没有它会退化成'无人察觉'"

    def test_guard_design_principle_is_in_the_opening(self):
        """「守卫要守不变量, 不要守实现细节」必须写进**章首** (用户要求)。

        用户原话: 「这条建议应写入 DISC-2 章首或索引表, 作为所有守卫的设计原则」。

        **为什么放章首而不是索引表**: 索引表列的是**六种失效形态**
        ("守卫可能怎么失效"), 而这条是**设计原则**("怎么写才不容易失效")——
        两者维度不同, 硬塞进形态表会污染该表的语义(它每行是一个"形态")。
        章首已有"依赖一次对照"那条共同原则, 设计原则与之并列最合适。
        """
        src = open(self._DOC_, encoding="utf-8").read()
        i = src.find("## DISC-2:")
        j = src.find("### 失效形态索引", i)
        assert i > 0 and j > i
        opening = src[i:j]
        assert "守「不变量」" in opening or "守不变量" in opening, (
            "DISC-2 章首缺『守卫要守不变量』这条设计原则")
        assert "实现细节" in opening, "应点明不要守实现细节"
        assert "正常演进" in opening, "应说明为何(正常演进时会撞到它)"
        assert "判断方法" in opening, "应给出可操作的判断方法"

    def test_design_principle_is_distinct_from_the_six_forms(self):
        """设计原则必须**显式声明它与六种形态维度不同** —— 否则读者会以为它是第 7 种形态。"""
        src = open(self._DOC_, encoding="utf-8").read()
        i = src.find("守「不变量」")
        block = src[i:i + 2000]
        assert "设计" in block and "形态" in block, (
            "应说清: 六种形态说的是『守卫可能怎么失效』, 这条说的是『怎么写』")


class TestNetworkTroubleshootingHasTwoBranches:
    """「网络看着正常但 git 连不上」必须分成**两种**可能 (用户 2026-09-25 要求)。

    用户原话: 「这两次形态不同, 不能一概归因到配置」。

    | 症状 | 真因 | 处置 |
    |---|---|---|
    | `TLS connect error` / `SSL routines` | 本仓配置(代理 + sslbackend) | **改配置** |
    | `Could not connect` / `Connection was reset` | 传输层波动 | **重试, 不动配置** |
    """

    def test_both_branches_present_with_different_remedies(self):
        src = open(_DOC, encoding="utf-8").read()
        i = src.find("网络看着正常但 git 连不上", src.find("## 环境不可用项"))
        assert i > 0, "缺「两种可能」小节"
        block = src[i:i + 3200]
        assert "TLS connect error" in block, "缺 TLS 那一支"
        assert "Could not connect" in block or "Connection was reset" in block, (
            "缺传输层波动那一支")
        assert "--local --unset" in block, "TLS 支应给改配置的修法"
        assert "重试" in block, "传输层支应给「重试」的修法"

    def test_warns_against_attributing_everything_to_config(self):
        """必须写明**不能一概归因到配置** —— 这正是用户强调的点。

        代价: 改配置去修一个本来会自愈的问题 ⇒ 配置改动**会留下来**,
        下次再出问题时多一个变量要排除。
        """
        src = open(_DOC, encoding="utf-8").read()
        i = src.find("网络看着正常但 git 连不上", src.find("## 环境不可用项"))
        block = src[i:i + 3200]
        assert "一概归因" in block, "未点明「不能一概归因」"
        assert "自愈" in block, "应说明传输层波动会自愈"
        assert "留下来" in block or "多一个变量" in block, (
            "应说明误改配置的代价(改动会留下来)")

    def test_gives_the_classification_procedure(self):
        """必须给**先分类再动手**的可操作判据, 而不是只列两种现象。"""
        src = open(_DOC, encoding="utf-8").read()
        i = src.find("网络看着正常但 git 连不上", src.find("## 环境不可用项"))
        block = src[i:i + 3200]
        assert "关键词" in block or "看错误文本" in block, "应教人看错误文本关键词分类"
        assert "Test-NetConnection" in block, "应用网络探测区分"
        assert "api.github.com" in block, (
            "应说明『某个 GitHub 端点通』不能推断『git 的端点也通』")


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

    def test_end_to_end_backfill_is_marked_unverified(self):
        """自动回填的端到端必须**明确标为未验证** (用户 2026-09-25 要求)。

        用户原话: 「标注『自动回填端到端待验证』—— 判定通过 ≠ 自动回填已验证」。

        **为什么这条值得守卫**: 触发判据有 35 例守卫、全部通过, 而"判据通过"
        与"端到端能用"是**两件事**。不写明的话, 下一次会话看到满屏绿色
        很自然会以为自动回填已经就绪 —— 而这正是"看起来做了 vs 实际生效"。
        """
        src = self._src()
        assert "自动回填端到端" in src, "缺「自动回填端到端待验证」的标注"
        assert "尚未验证" in src or "待验证" in src, "未显式说明它还没验证"
        assert "判定通过 ≠ " in src or "不是一回事" in src, (
            "应写明『判定通过 ≠ 自动回填已验证』这个区分")
        # 提到"从未实跑"这个事实, 而不是含糊的"待完善"
        assert "未实跑" in src or "从未" in src, "应说明『从未实跑过』这个具体事实"
        # 必须给出验证步骤(否则"待验证"就成了一句免责声明)
        assert "backfill_trigger.py --evaluate" in src, "应给出可执行的验证入口"
        assert "BACKFILL_ENABLED=1" in src, "应给出开启方式"


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


class TestGuardMustNotDependOnGitignoredMachineState:
    """『守卫不得依赖 gitignored 的机器状态』必须写进 DISC-2 共同原则 (2026-09-25)。

    **为什么这条值得单独立守卫**: 它是本仓第一个「**按预案正常操作**却让守卫变红**」的
    实例 —— `data/backfill_switch.json` 按 09-28 预案建成 `{"enabled": true}` 之后,
    `assert BT.is_enabled({}) is False` 就失败了(机器上多了一个 gitignored 文件)。

    这类失效**最难自查**, 因为: 代码全对、失败信息指向错误的方向("默认值不对"),
    而最省事的处理是**放宽断言** —— 正是 DISC-2「守卫的设计原则」描述的退化路径。
    所以除了写进文档, 还要机械断言"守卫自己已经不再依赖机器状态"。
    """

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_documented_as_a_paid_for_instance(self):
        """必须列进「已经付出过代价的实例」, 而不是只当一条泛泛的原则。"""
        src = self._src()
        i = src.find("共同原则: **依赖一次对照, 不依赖注意力**")
        assert i > 0, "找不到共同原则小节"
        block = src[i:i + 9000]
        assert "版本控制之外" in block or "gitignored" in block, (
            "实例里必须点明「版本控制之外/ignored」这个关键词")
        assert "五个" in block, "实例数应从四个更新为五个"

    def test_names_the_actual_file_and_the_actual_test(self):
        """用真实文件名与用例名, 不写泛泛的"某个配置文件"。

        [2026-09-26] 细节已从章首叙述块**升格**为正文 ③b 一节
        (用户要求「③b 与 ③ 并列」)。章首块因此被压成"索引 + 一句话摘要",
        **不再**重复文件名/用例名 —— 这是刻意的(同一件事写两处, 迟早只剩一处是对的)。
        故真实文件名/用例名只在 ③b 一处取证即可。
        """
        src = self._src()
        i = src.find("断言依赖 <u>gitignored 的机器状态</u>")
        assert i > 0, "找不到章首该实例(③b 的索引块)"
        blk = src[i:i + 1500]
        # 章首块只负责"指路": 必须点名正式形态, 且一句话说清后果
        assert "③b" in blk, "章首块应指向正式形态 ③b"
        assert "环境事实" in blk and "显式输入" in blk, (
            "章首摘要应保留「环境事实 -> 显式输入」这个修法")
        assert "代码完全正确" in blk or "代码是对的" in blk or "而代码完全正确" in blk, (
            "章首摘要必须点明最反直觉的一点: **代码是对的**")
        # 正式形态 ③b 那一节必须点名真实的文件/用例/计数
        k = src.find("#### ③b")
        assert k > 0, "缺正式形态 ③b 小节"
        sec = src[k:k + 6000]
        assert "backfill_switch.json" in sec, "③b 一节必须点名真实的开关文件"
        assert "test_disabled_by_default" in sec, "③b 一节必须点名真实的失败用例"
        assert "2431" in sec, "必须给出当时的真实计数(2431 passed / 1 failed)"

    def test_explains_why_it_is_not_a_regression(self):
        """关键区分: **代码是对的**, 错的是断言把环境事实当成了显式输入。

        若读者以为这是"改了代码导致回归", 他会去改代码; 正确的动作是改断言。
        """
        src = self._src()
        k = src.find("#### ③b")
        assert k > 0, "缺正式形态 ③b 小节"
        sec = src[k:k + 6000]
        assert "不是回归" in sec, "必须明确写出「这不是回归」"
        assert "显式输入" in sec, "必须给出正解: 把前提改成**显式输入**"

    def test_distinguishes_it_from_form3(self):
        """必须与 ③「前置状态没造出来」区分开 —— 否则形态边界就糊了。"""
        src = self._src()
        k = src.find("#### ③b")
        assert k > 0, "缺正式形态 ③b 小节"
        sec = src[k:k + 3000]
        assert "没造出来" in sec and "外部" in sec, (
            "应说清: ③ 是自己没造前置状态, ③b 是被**外部**改变了")
        assert "并列" in sec, "应声明 ③b 与 ③ **并列**而不是第 9 种形态"

    def test_gives_the_one_line_judgement_rule(self):
        """必须有一条可机械执行的判据, 否则又退回"靠注意力"。"""
        src = self._src()
        i = src.find("这条断言依赖的任何东西")
        assert i > 0, "缺一句话判据"
        block = src[i:i + 600]
        for kw in ("logs", "环境变量"):
            assert kw in block, f"判据里应把 {kw} 一并列出(同类风险源)"

    def test_says_the_conflict_will_grow_not_shrink(self):
        """附注必须说明「这类冲突会随运维成熟而增加」—— 这是"现在就立"的理由。"""
        src = self._src()
        i = src.find("为什么这类问题会越来越多")
        assert i > 0, "缺该附注"
        block = src[i:i + 600]
        assert "增加" in block and "不会减少" in block

    def test_form3b_and_form7_are_both_in_the_index(self):
        """③b 与 ⑦ 必须**同时**出现在索引表里 —— 否则读者扫不到。"""
        src = self._src()
        i = src.find("### 失效形态索引")
        j = src.find("### 详细判据", i)
        idx = src[i:j]
        assert "| **③b" in idx, "索引里缺 ③b 行"
        assert "| **⑦" in idx, "索引里缺 ⑦ 行"
        # 两条都必须是**独立行**, 不能并进 ③ / ⑥ 的格里
        assert idx.count("| **③b") == 1, "③b 应恰好一行"
        assert idx.count("| **⑦") == 1, "⑦ 应恰好一行"

    def test_the_real_test_now_injects_the_switch_path(self):
        """反向验证: 真实用例必须**已经**把开关路径变成显式输入(不是只写了文档)。"""
        fp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tests", "test_backfill_trigger.py")
        src = open(fp, encoding="utf-8").read()
        i = src.find("class TestDefaultOff")
        assert i > 0, "找不到 TestDefaultOff"
        j = src.find("\nclass ", i + 1)
        block = src[i:j if j > 0 else len(src)]
        assert "switch_fp=" in block, (
            "TestDefaultOff 必须**显式注入** switch_fp, 否则又依赖机器上有没有那个文件")
        assert "no_such_switch.json" in block, "应注入一个确定不存在的路径"

    def test_the_real_test_locks_purity_of_decide(self):
        """再反向验证: 必须有用例锁住 `decide()` 的**纯函数性**。

        这是本条的**机械化**形式 —— 不靠人去记住"别在 decide 里读环境",
        而是把"读环境/读文件"变成一条会自动变红的断言。
        """
        fp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tests", "test_backfill_trigger.py")
        src = open(fp, encoding="utf-8").read()
        i = src.find("test_machine_switch_file_does_not_break_the_pure_decide")
        assert i > 0, "缺 decide 纯函数性守卫"
        block = src[i:i + 900]
        assert "getsource" in block, "应检查 **源码** 而非行为(行为会漏)"
        assert "os.environ" in block and "_load_switch_file" in block, (
            "应同时禁止读环境变量与读开关文件")


class TestProductionDaemonInterpreterIsNotTheToolOne:
    """`docs/disciplines.md` 必须写对**生产守护用的是哪个解释器** (2026-09-26 更正)。

    ## 为什么这条值得单独立守卫

    09-25 我据「生产解释器无 torch」把 `DrlEnvMissing` 标成**结构性误报**;
    实际 `src/daemon.py:35-40` 是 `PY = sys.executable`(可被 `TRAE_PYTHON` 覆盖),
    metrics_server 与 run_daily **共用这一个 `PY`**, 而生产守护由
    `scripts/start_daemon.ps1` 用 **`.venv310`** 启动 —— 那里四项齐备, 规则本应 inactive。

    **代价**: 一条真告警被仓库自己的文字标注成"已知误报",
    将来真故障时值班人会照注释忽略它 —— 比没有告警更坏。

    **故这里锁两件事**:
      1. 那张表必须把"生产 VM 工具解释器"与"生产守护用的解释器"**分开列**;
      2. 必须写明 `daemon.py` 的**单一 `PY` 来源**这一机制 —— 否则后人会重新推断出
         "探针与训练是两个解释器"。
    """

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    #: 出现"误报"时, 同一行必须同时出现这些**更正/否定**信号之一, 否则视为"主张式误报"
    _CORRECTION_MARKERS = ("不是误报", "曾", "错标", "更正", "⚠️", "不应", "不再")

    @staticmethod
    def _is_narrative(src: str) -> list:
        """标记每行是否属于**病历/更正叙述**(可以原样引用错误)。

        两类都是"引用错误", 都不是主张:
          · `>` 引用的块;
          · **⑦ 的实例小节**(2026-09-26 起, 病历从块引用搬进了正文,
            因为 ⑦ 成了正式形态)—— 从 `### ⑦` 到下一个非引用 `### ` 为止。

        ## 定位小节标题时必须同时满足两个条件(两次踩坑的结论)

        1. **行首锚定**(`line.startswith("### ⑦")`) —— 否则正文里
          「`### ⑦ 证据无判别力`」这种**引用**也会被当成标题(④b 的近亲);
        2. **不是引用行**(`not line.startswith(">")`) —— 否则 `> #### 追加判据`
          这类**被引用的小节**会被当成真正的小节起点。

        第一版只做了 1 的近似(`src.find("### ⑦")`), 结果命中了**块引用里的
        一个 `> ####` 标题**(因为该块内恰好也含 `⑦` 字样) ⇒ 叙述区被算到几千行,
        把正文的**主张**也划进了"允许引用"的范围 ⇒ 主守卫静默失效。
        **这正是本条要防的: 识别条件与实际格式不符。**
        """
        lines = src.splitlines()
        flags = [ln.lstrip().startswith(">") for ln in lines]
        start = None
        for n, ln in enumerate(lines):
            if flags[n]:
                continue                      # 引用行里的小节标题不算
            if ln.startswith("### ⑦"):
                start = n
                break
        if start is not None:
            end = len(lines)
            for n in range(start + 1, len(lines)):
                if flags[n]:
                    continue                  # 引用行里的小节标题不算
                if lines[n].startswith("### "):
                    end = n
                    break
            for n in range(start, end):
                flags[n] = True
        return flags

    def test_does_not_call_drlenvmissing_a_false_positive(self):
        """**关键**: 正文/表格里不得把 `DrlEnvMissing` **主张**成误报。

        ## 判据为什么是"误报 且 无更正信号", 而不是"行里没有误报"

        写这条时我连踩三次假阳性, 三次都值得记(③b 的近亲):

        1. **第一版扫全文** ⇒ 报了我自己: 更正的叙述里必须**原样引用**当初的错误
           (「我把告警 `DrlEnvMissing` 标注成『已知结构性误报』」)—— 那是病历,
           不是主张。**禁止引用错误 = 禁止记录错误**, 而那正是本仓最看重的部分。
        2. **第二版只排除 `>` 叙述块** ⇒ 仍报: 那张**汇总表**里也有一行是更正
           (「**不是误报**！曾于 09-25 被错标, 09-26 已更正」)。
        3. **第三版(2026-09-26)** ⇒ 又报: 用户要求把 ⑦ 升格为正式形态后,
           病历从 `>` 块搬进了正文 `### ⑦` 小节 ⇒ 第 2 版的排除条件失效。

        ⇒ 判据最终定为: **出现"误报" 且 同一行没有任何更正/否定信号** 才算违规,
        且"叙述区"同时覆盖 `>` 块**与** ⑦ 的实例小节。
        **一个会误报的守卫等于没有**(本仓守卫设计原则), 故这三次返工必须记下。
        """
        src = self._src()
        flags = self._is_narrative(src)
        offenders = []
        for n, ln in enumerate(src.splitlines()):
            if n < len(flags) and flags[n]:
                continue
            if "DrlEnvMissing" in ln and "误报" in ln:
                if not any(m in ln for m in self._CORRECTION_MARKERS):
                    offenders.append(ln.strip())
        assert not offenders, (
            "docs 在**正文/表格**里把 DrlEnvMissing 主张成误报, 会误导值班人:\n  "
            + "\n  ".join(offenders))

    def test_the_guard_is_not_vacuous(self):
        """反向验证: 判据必须能抓到"主张式误报"这一**真实**形态。

        不做这条, 上一条可能因排除条件写宽而变成**空守卫**(永远通过)。
        这里直接喂样本: 主张式(该报)、两种更正式(不该报)。
        """
        markers = self._CORRECTION_MARKERS

        def is_offender(ln, narrative=False):
            return ("DrlEnvMissing" in ln and "误报" in ln and not narrative
                    and not any(m in ln for m in markers))

        assert is_offender("| `DrlEnvMissing` | metrics_server 的解释器无 torch | 结构性误报 |"), \
            "抓不到主张式误报 —— 守卫是空的"
        assert not is_offender("| `DrlEnvMissing` 告警 | —— | **不是误报**！曾于 09-25 被错标 |"), \
            "更正式表述被误报"
        assert not is_offender("我把 `DrlEnvMissing` 标注成「已知结构性误报」, 这是错的",
                               narrative=True), "叙述区里的引用被误报"

    def test_the_narrative_regions_are_detected(self):
        """叙述区必须**真的**识别出来, 且**不得过大** —— 否则排除条件是空转或过宽。

        [2026-09-26 两次失效]
        · 第一版只认 `>` 块 ⇒ 病历搬进 `### ⑦` 后立刻误报;
        · 第二版用 `src.find("### ⑦")` 定位小节 ⇒ 命中了**块引用里的一个标题**
          (该块恰好也含 `⑦` 字样) ⇒ 叙述区被算到几千行, 把正文**主张**也划了进去
          ⇒ 主守卫静默失效。**故这里必须同时断言"识别到了"与"没有识别过头"。**
        """
        src = self._src()
        flags = self._is_narrative(src)
        lines = src.splitlines()
        n_quote = sum(1 for n, ln in enumerate(lines)
                      if n < len(flags) and flags[n] and "误报" in ln)
        assert n_quote > 0, "没有任何叙述行引用「误报」—— 排除条件在空转"
        # ⑦ 小节必须被标进叙述区(且必须是**正文那个**, 不是引用里的)
        start = next(n for n, ln in enumerate(lines)
                     if ln.startswith("### ⑦"))
        assert flags[start], "正文 ⑦ 小节未被识别为叙述区(病历在那里)"
        # `>` 块也必须仍被识别
        q = [n for n, ln in enumerate(lines) if ln.lstrip().startswith(">")]
        assert q and all(flags[n] for n in q), "`>` 块未被识别为叙述区"
        # **不得识别过头**: 叙述区不能占全文一大半(否则主守卫等于失效)
        assert sum(flags) < len(lines) * 0.5, (
            f"叙述区占了 {sum(flags)}/{len(lines)} 行 —— "
            "识别条件过宽, 主守卫已静默失效")
        # 注: **刻意不断言"叙述区段数"** —— 本 doc 的 `>` 块本来就散落在全文各处
        # (实测 34 段), 拿"段数少"当判据是错的判据(会误报)。
        # 真正能抓住"识别过头"的是上面的**占幅**断言: 本次的 bug 是把叙述区
        # 算到了几千行, 占幅会立刻超过一半。**判据要选能区分对错的那一个**,
        # 而不是"看起来相关的那个"。

    def test_the_narrative_is_allowed_to_quote_the_error(self):
        """更正叙述里**必须**能原样引用错误 —— 否则后人不知道曾经错过什么。"""
        src = self._src()
        narrative = [ln for ln in src.splitlines()
                     if ln.strip().startswith(">") and "DrlEnvMissing" in ln and "误报" in ln]
        assert narrative, (
            "更正叙述里应**原样引用**当初的错误标注 —— 否则后人不知道曾经错过什么")

    def test_names_venv310_as_the_daemon_interpreter(self):
        src = self._src()
        i = src.find("生产守护用的是")
        assert i > 0, "缺「生产守护用的是哪个解释器」这一小节"
        block = src[i:i + 2600]
        assert ".venv310" in block, "必须点明守护用 .venv310"
        assert "start_daemon.ps1" in block, "必须给出可查证的启动点"
        assert "vm\\tools\\python" in block or "vm\\tools" in block, (
            "必须把那个工具解释器也列出来做**对照**, 否则读者仍会混淆两者")

    def test_states_the_single_py_mechanism_with_line_refs(self):
        """机制必须带**行号**引用 —— 否则"查启动点"无法复核。"""
        src = self._src()
        i = src.find("生产守护用的是")
        block = src[i:i + 2600]
        assert "daemon.py:35" in block, "必须给出 daemon.py 的行号"
        assert "TRAE_PYTHON" in block, "必须点明覆盖它的环境变量"
        assert "同源" in block, "必须说清结论: 所有子进程天然同源"

    def test_records_the_pyyaml_bridge_recipe(self):
        """必须记 PyYAML 桥接法 —— 否则 YAML 用例永远 skip(skip 被读成通过)。"""
        src = self._src()
        i = src.find("桥接法")
        assert i > 0, "缺桥接法小节"
        block = src[i:i + 1200]
        assert "PYTHONPATH" in block, "桥接靠 PYTHONPATH"
        assert "getsitepackages" in block, "必须给出取 site-packages 的确切写法"
        assert "skip" in block, "必须说明不桥接的后果是**静默 skip**"

    def test_evidence_fitting_both_hypotheses_is_documented(self):
        """必须立「一条证据同时支持两个互斥假设 ⇒ 不构成确认」这条判据。

        这是本次错标的**根因**, 也是最可复用的一课 —— 若只记"我查错了解释器",
        后人遇到同类"证据看着对、方向却错"的情形会重犯。
        """
        src = self._src()
        i = src.find("同时支持两个互斥假设")
        assert i > 0, "缺「证据同时支持两个假设」这条判据"
        block = src[i:i + 2600]
        assert "判别力" in block or "会不一样吗" in block, (
            "必须给出可操作的判据(如果反过来才是真的, 这条证据会不一样吗)")
        assert "比没有告警更坏" in block, (
            "必须写明后果量级 —— 否则这条判据会被当成纯粹的思辨")
        assert "被文字解释掉了" in block, (
            "应点明形态: ⑥ 的反向(告警在, 但被文字解释掉了)")

    def test_guard_self_reference_third_form_is_documented(self):
        """「守卫自指涉」必须补上**第三种形态**: 判据命中「关于判据的数据」。"""
        src = self._src()
        i = src.find("守卫自指涉的**第三种形态**")
        assert i > 0, "缺守卫自指涉第三种形态"
        block = src[i:i + 2200]
        assert "样本" in block, (
            "必须点明第三种形态命中的是**样本字符串**, 不是守卫自己的源码")
        assert "排除" in block and "理由" in block, (
            "必须要求排除项写明理由 —— 否则分不清「已知副作用」与「为变绿而加的例外」")

    def test_empty_guard_case_history_is_documented(self):
        """「连负样本都抓不到的守卫」必须留病历 —— 空守卫比没有守卫更危险。"""
        src = self._src()
        i = src.find("空守卫")
        assert i > 0, "缺「空守卫」病历"
        block = src[i:i + 2200]
        assert "v1" in block and "v3" in block, "应保留三版返工的过程"
        assert "负样本" in block, (
            "必须写明「负样本必须被抓到」是这类守卫的存在性证明")
        assert "全绿" in block, "必须点明空守卫**显示为全绿**这一要点"


class TestNoTestCallsAnUndefinedHelper:
    """用例调用的 helper 必须真的存在 —— 静态查, **不需要 PyYAML**。

    ## 被查出来的真实缺陷 (2026-09-26)

    `tests/test_alert_rules_single_source.py` 里**三个**用例写了
    `rules = _load_rules()`, 而那个函数**在该文件里从未定义**。
    三者都挂 `@_needs_yaml`, 而 `.venv314`/`.venv310` 都没有 PyYAML ⇒
    **恒为 skip** ⇒ 报告里"看起来通过", 实际一跑就 `NameError`。

    **这是 ①(假信心)藏在 ②(skip 读成通过)后面**: 只看任一种形态都不够,
    而"用需要 yaml 的解释器跑一次"同时暴露了两者。
    本守卫把这件事变成**不需要 yaml 也真跑**的静态检查。

    ## 判据为什么写得这么窄 (三次返工的教训, 值得完整保留)

    这是本条纪律最贵的部分 —— 我写了**三版**, 前两版都不能用:

    | 版本 | 做法 | 结果 |
    |---|---|---|
    | v1 | 收集 `def`/参数名再比对 | **大量假阳性**: 把内置 `__import__`、函数内局部定义的类(`_Cfg`)全报成"未定义" |
    | v2 | `symtable` 遍历子表 | 仍有假阳性: `seen`/`called`/`tmp_path` 这类**在嵌套作用域里赋值**的名字被误报 |
    | v3 | 只看 `Call` + "任何作用域都没赋过值" + 跳过首字母小写 | **0 假阳性**(全仓 90 个测试文件), 且能抓到原缺陷 |

    **为什么 v2 也会错**: `seen` 常常是 fixture 内部赋的值, 而我只看"本函数的符号表",
    看不到别的函数里的赋值 ⇒ 判据比语言语义**更窄**, 于是误报。

    **为什么 v3 跳过首字母小写**: pytest 的 fixture 是**参数注入** ——
    原名在文件里以 `arg` 出现(已收集), 但像 `monkeypatch.setattr` 之类还会
    引入"用了但没赋值"的名字。跳过小写开头即可覆盖这类约定,
    而**缺陷形态**(helper 函数)习惯上不是小写开头。

    **一次真实的自我打脸**(必须记下): v1 的反向样本我用的是 `_LoadRules()`,
    而当时的过滤器是 `if not nm[:1].isupper(): continue` ——
    `"_"[:1].isupper()` 是 **False** ⇒ 连**反向样本自己都被跳过**,
    于是"负样本没报警"被我误读成"负样本通过"。**一个连负样本都抓不到的守卫,
    会显示为全绿** —— 这正是本仓 ① 形态的教科书例子。
    故下面 `test_negative_samples_are_actually_caught` 是**必需**的, 不是装饰。
    """

    def _test_dir(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests")

    @staticmethod
    def _assigned_anywhere(tree) -> set:
        """收集**任何作用域**里绑定过名字的标识符。"""
        import ast

        out = set()

        class V(ast.NodeVisitor):
            def visit_Name(self, n):
                if isinstance(n.ctx, (ast.Store, ast.Del)):
                    out.add(n.id)
                self.generic_visit(n)

            def visit_arg(self, n):
                out.add(n.arg)
                self.generic_visit(n)

            def visit_FunctionDef(self, n):
                out.add(n.name)
                self.generic_visit(n)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, n):
                out.add(n.name)
                self.generic_visit(n)

            def visit_Import(self, n):
                for a in n.names:
                    out.add(a.asname or a.name.split(".")[0])
                self.generic_visit(n)

            def visit_ImportFrom(self, n):
                for a in n.names:
                    out.add(a.asname or a.name)
                self.generic_visit(n)

            def visit_ExceptHandler(self, n):
                if n.name:
                    out.add(n.name)
                self.generic_visit(n)

            def visit_Global(self, n):
                out.update(n.names)
                self.generic_visit(n)

            def visit_Nonlocal(self, n):
                out.update(n.names)
                self.generic_visit(n)

        V().visit(tree)
        return out

    def _scan(self, src: str) -> list:
        """返回 [(行号, 名字)]: 被调用、却在本文件里查不到任何绑定的名字。"""
        import ast
        import builtins

        tree = ast.parse(src)
        known = self._assigned_anywhere(tree) | set(dir(builtins))
        bad = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                nm = n.func.id
                if nm in known or nm[:1].islower():
                    continue
                bad.append((n.lineno, nm))
        return sorted(set(bad))

    def test_no_test_file_calls_an_undefined_helper(self):
        offenders = []
        for fn in sorted(os.listdir(self._test_dir())):
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(self._test_dir(), fn)
            try:
                bad = self._scan(open(fp, encoding="utf-8").read())
            except (SyntaxError, ValueError) as e:  # pragma: no cover
                offenders.append(f"{fn}: 无法解析 {e}")
                continue
            if bad:
                offenders.append(f"{fn}: 未定义却被调用 {bad}")
        assert not offenders, (
            "存在「调用了不存在的东西」的测试文件 —— 若这些调用又挂着 skip 条件, "
            "就会**静默地永不执行**(2026-09-26 `_load_rules` 实例):\n  "
            + "\n  ".join(offenders))

    def test_negative_samples_are_actually_caught(self):
        """**必须有**: 证明上面的判据不是空的。

        v1 的反向样本(`_LoadRules()`)因过滤器写成 `not nm[:1].isupper()` 而
        **自己也被跳过**, 于是"没报警"被我误读成"通过" —— 全绿的空守卫。

        注: 下面的样本串**刻意不含 PyYAML 的字样** —— 否则会撞上
        `TestEveryYamlUsingTestIsMarked`(它按源码里是否出现该库名来判断
        "这个用例用没用 PyYAML", 而本用例其实与它无关)。
        **这是守卫自指涉的又一实例**: 一条守卫的判据(子串匹配)会命中
        另一条守卫的**说明文字**。
        """
        assert self._scan("def test_a():\n    rules = _load_helper()\n"), (
            "抓不到原始缺陷形态 —— 守卫是空的")
        assert self._scan("def test_a():\n    _SomeHelper()\n"), "抓不到下划线前缀"
        assert self._scan("def test_a():\n    UnknownThing()\n"), "抓不到普通未定义名"

    def test_positive_samples_are_not_flagged(self):
        """已定义的 helper / fixture 注入 / 局部定义**不得**误报。

        会误报的守卫等于没有 —— 而且会被"顺手"删掉(本仓守卫设计原则)。
        """
        assert self._scan(
            "def _load_helper():\n    return {}\n\n\ndef test_a():\n    _load_helper()\n"
        ) == [], "已定义的 helper 被误报"
        assert self._scan(
            "def test_a(some_fixture):\n    assert some_fixture\n"
        ) == [], "fixture 注入被误报"
        assert self._scan(
            "def test_a():\n    class _Cfg:\n        pass\n    _Cfg()\n"
        ) == [], "函数内局部定义被误报"
        assert self._scan(
            "import os as _os\n\n\ndef test_a():\n    assert _os.sep\n"
        ) == [], "import 别名被误报"


class TestBackfillObservationMustRecordCriteria:
    """§6.13/§6.14 的检查单必须把 **criteria 两值(A/B)** 列为**必记项** (2026-09-26 用户要求)。

    ## 为什么这条必须写成纪律而不是"记得看一眼"

    `no_gap` 有**两个来由**, 二值不同、运维含义相反:

    | A_engine_gap | B_h5i_gap | action | 含义 |
    |---|---|---|---|
    | `false` | 任意 | no_gap | **厂商已恢复**(引擎追平 ⇒ 没有"厂商缺席的日子"要补) |
    | `true` | `false` | no_gap | **厂商未恢复, 但 h5i 已补齐**(重复回填无意义) |
    | `true` | `true` | **trigger** | 真正要回填的缺口 |

    前两行**都会打印 `no_gap`**。只记 `action`, 一周后**分不清**当时是哪种局面 ——
    而 09-28 这场自然实验要回答的正是这个问题。

    **通用判据**: 凡"同一个结论有两个不同来由"的地方, 记录时**必须连判据一起记**。
    本仓同族: ⑤ 要求保留每层的 `error` 字段, 而不是只留最终 `ok`。
    """

    _DOC2 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "docs", "stockdb-source-status.md")

    def _src(self):
        return open(self._DOC2, encoding="utf-8").read()

    def test_checklist_has_a_mandatory_criteria_step(self):
        src = self._src()
        i = src.find("### 6.13")
        assert i > 0, "缺 §6.13"
        blk = src[i:i + 4000]
        assert "criteria" in blk, "§6.13 检查单里没提 criteria"
        assert "A_engine_gap" in blk and "B_h5i_gap" in blk, (
            "必须**分别**列出两个布尔值(只写 criteria 不够明确)")
        # 必须是"强制/不可省"级别的措辞, 而不是可选
        assert ("强制" in blk) or ("必记" in blk), (
            "必须标明这是**必记项**, 否则执行时会跳过")

    def test_explains_the_two_reasons_for_no_gap(self):
        """必须给出那张三行表 —— 否则读者不知道"为什么要记两个布尔值"。"""
        src = self._src()
        i = src.find("为什么 `criteria` 是**必记项**")
        assert i > 0, "缺「为什么 criteria 是必记项」小节"
        blk = src[i:i + 2600]
        assert "两个来由" in blk or "两个不同来由" in blk, "应点明 no_gap 有两个来由"
        # 三行都要有
        assert "false" in blk and "true" in blk, "缺判据取值"
        assert "厂商已恢复" in blk, "缺「A=false = 厂商恢复」这一行"
        assert "trigger" in blk, "缺「A 且 B 都真 ⇒ trigger」这一行"

    def test_observation_1_actually_recorded_both_values(self):
        """**以身作则**: 观测 ① 必须真的记了 A/B 两个值, 不能只记 action。"""
        src = self._src()
        i = src.find("#### 观测 ①")
        assert i > 0, "缺观测 ①"
        blk = src[i:i + 2000]
        assert "A_engine_gap" in blk and "B_h5i_gap" in blk, (
            "观测 ① 没记 criteria 两值 —— 那条纪律自己就没被遵守")
        assert "true" in blk and "false" in blk, "观测 ① 的判据取值不全"

    def test_observation_2_checklist_also_requires_criteria(self):
        """走向 A/B 的判定必须**基于 criteria**, 而不是"看引擎日期猜"。"""
        src = self._src()
        i = src.find("#### 观测 ②")
        assert i > 0, "缺观测 ②"
        blk = src[i:i + 2600]
        assert "A_engine_gap" in blk, "观测 ② 的清单必须以 criteria 为判据"
        assert "B 决定 action" in blk or "**B 决定" in blk, (
            "应写清: 厂商未恢复时由 **B** 决定是否 trigger")


class TestGuardRecognitionMustCoverAllFormats:
    """『识别条件必须覆盖该节的所有可能格式』必须写入守护设计原则 (2026-09-26 用户要求)。

    ## 两天内踩了三次同一个坑(每条都是实测)

    | # | 识别条件写的 | 结构怎么变的 | 后果 |
    |---|---|---|---|
    | 1 | 源码文本含 `yaml.safe_load` | 守卫自己的 docstring 里也有 | 报了自己(良性) |
    | 2 | 函数体含 `_load_rules(` | 另一守卫的**样本串**里也有 | 误报样本(良性) |
    | 3 | 叙述区 = `>` 开头的块 | 病历**搬进正文** `### ⑦` | **反向误报**: 去报病历 |

    根因同一个: 把"识别条件"写成了**当时文档的偶然形状**, 而不是**语义特征**。
    """

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_principle_is_stated_as_a_design_rule(self):
        src = self._src()
        i = src.find("识别条件必须覆盖该节的所有可能格式")
        assert i > 0, "缺「识别条件必须覆盖所有格式」这条设计原则"
        blk = src[i:i + 3000]
        assert "语义" in blk, "必须给出正解: 用**语义特征**而非位置/字面量"
        assert "枚举" in blk, "必须要求**枚举**该节的可能格式"

    def test_lists_all_three_real_occurrences(self):
        """三次实测必须都列出来 —— 只写一条会显得像偶发。"""
        src = self._src()
        i = src.find("两天内踩了三次")
        assert i > 0, "必须说明这是**三次**重复, 不是偶发"
        blk = src[i:i + 1600]
        assert "docstring" in blk or "样本" in blk, "缺第 1/2 次(自指涉/样本串)"
        assert "搬进正文" in blk or "⑦" in blk, "缺第 3 次(病历搬进正文)"

    def test_requires_a_nonempty_recognition_region_assertion(self):
        """必须要求一条「识别区非空」的反向验证 —— 否则识别条件可能空转。"""
        src = self._src()
        i = src.find("识别条件必须覆盖该节的所有可能格式")
        blk = src[i:i + 3000]
        assert "识别区非空" in blk or "非空" in blk, (
            "必须要求反向验证: 识别出的区域**确实有内容**")
        assert "识别一切" in blk or "识别不到" in blk, (
            "应说清两种退化方向: 识别一切(主断言变空) / 识别不到(恒真)")

    def test_distinguishes_from_4b(self):
        """必须与 ④b 分工说清 —— 否则两条形态的边界糊掉。"""
        src = self._src()
        i = src.find("与 ④b 的分工")
        assert i > 0, "缺「与 ④b 的分工」"
        blk = src[i:i + 400]
        assert "切片边界" in blk and "识别条件" in blk, (
            "应说清: ④b 管**切片边界**, 本条管**识别条件**")


class TestDocAnchorPhrasesAreUnique:
    """被守卫用作**定位锚点**的关键句, 在全文里必须**只出现一次** (2026-09-26)。

    ## 为什么单独立这条

    多个守卫用 `src.find("某句关键话")` 来**定位某一节**(因为它们要切片取证)。
    而写文档时**引用某节的关键句**是极自然的冲动 —— 一旦引用出现在
    **更靠前的位置**, `find` 就命中那个引用, 切片窗口随之落空 ⇒
    **守卫失败, 而文档本身没有任何问题**。

    **实测(本会话第 5 次"守卫自指涉")**: 我在「验首字节」那节里引用了 DISC-1 的
    留痕原则原句来作类比, 结果 `test_rule_explains_why_guessing_is_worse` 与
    `test_rule_links_to_the_implementation_and_guards` **两条同时失败** ——
    它们以为自己在读 DISC-1, 实际读的是我那段编码说明。

    处置: 把全文里**所有**对该句的引用改成同义描述, 使其**唯一**。
    本条守卫把这个不变量锁住 —— 下次再有人引用, 会**立刻红**,
    而不是让那两条 DISC-1 守卫以"找不到实现名"这种**误导性理由**失败。

    ## 判据

    > 凡被守卫当作**定位锚点**的字符串, 都必须**唯一**;
    > 不唯一 ⇒ 改锚点(用行首锚定的小节标题), 或把其它引用改写成同义描述。
    """

    #: 被守卫用作定位锚点的关键句 -> 它应当出现在哪一节里
    _ANCHORS = {
        "宁可 None, 不猜": "### 同族纪律: 留痕字段",   # DISC-1 的小节标题
    }

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_each_anchor_appears_exactly_once(self):
        src = self._src()
        for anchor, must_be_in in self._ANCHORS.items():
            n = src.count(anchor)
            assert n == 1, (
                f"锚点 {anchor!r} 在全文出现 {n} 次 —— 必须唯一。"
                "多处出现时, 用 src.find() 定位那一节的守卫会命中**更靠前的引用**"
                "⇒ 切片落空 ⇒ 守卫以误导性理由失败(实测过)")
            i = src.find(anchor)
            # 它必须落在预期的那一节里(前后各看几行即可)
            near = src[max(0, i - 200):i + 200]
            assert "留痕字段" in near, (
                f"锚点 {anchor!r} 已不在 DISC-1 的留痕小节附近, 实际上下文: {near[-120:]!r}")

    def test_the_guard_is_not_vacuous(self):
        """反向验证: 重复的锚点必须被抓到。"""
        src = self._src()
        anchor = "宁可 None, 不猜"
        dup = src + "\n" + anchor + "\n"
        for a in self._ANCHORS:
            assert dup.count(a) == 2, "反向样本没造出重复 —— 断言会空转"


class TestSameConclusionDifferentReasonsMustRecordCriteria:
    """『结论相同、来由不同处, 必须连判据一起记』必须写进 DISC-2 ⑦ (2026-09-26 用户要求)。

    用户指出: 它与 ⑤ **同族, 但更一般化** ——
    ⑤ 要求「保留每层 `error` 字段」, 本质就是"不要只留最终那个 `ok`";
    本条把它推广到**所有多来由的结论**(不限于错误信息)。
    """

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def _form7_bounds(self, src):
        """返回 ⑦ **正文小节**的字符区间。

        必须**行首锚定且排除引用行** —— 否则会命中两个非小节的地方
        (实测: 索引表里的一格内容、以及一段引用里的 `### ⑦` 字样),
        这正是「识别条件必须覆盖所有格式」要求的做法。
        """
        lines = src.splitlines(keepends=True)
        start = end = None
        off = 0
        for ln in lines:
            if start is None:
                if ln.startswith("### ⑦"):
                    start = off
            elif ln.startswith("### "):
                end = off
                break
            off += len(ln)
        if start is None:
            return None, None
        return start, (end if end is not None else len(src))

    def test_subitem_present_under_form7(self):
        src = self._src()
        i = src.find("结论相同、来由不同处, 必须连判据一起记")
        assert i > 0, "缺「结论相同、来由不同处必须连判据一起记」这条子条目"
        a, b = self._form7_bounds(src)
        assert a is not None, "找不到 ⑦ 的正文小节标题"
        assert a < i < b, (
            f"该子条目应落在 ⑦ 正文小节内({a}..{b}), 实际位置 {i}")

    def test_the_bounds_helper_is_not_vacuous(self):
        """反向验证: 边界必须是**正文小节**, 不能命中索引表或引用里的同名字样。"""
        src = self._src()
        a, b = self._form7_bounds(src)
        assert a is not None, "没找到 ⑦ 小节"
        # 起点必须是行首, 且前面不是表格行
        assert src[a:a + 4] == "### ", "起点不在行首"
        assert src.rfind("\n", 0, a) >= 0
        prev_line = src[src.rfind("\n", 0, a) + 1:a]
        assert not prev_line.startswith("|"), "命中了索引表的格子, 不是小节标题"
        assert not prev_line.startswith(">"), "命中了引用行"
        # 区间内必须真的含 ⑦ 的三条判据(否则切到了别处)
        seg = src[a:b]
        for kw in ("反过来才是真的", "能区分对错吗", "来由不同"):
            assert kw in seg, f"⑦ 正文小节里缺判据: {kw}"

    def test_gives_the_three_row_backfill_table(self):
        """必须用**本仓真实**的三行判据表作实例, 而不是泛泛举例。"""
        src = self._src()
        i = src.find("结论相同、来由不同处, 必须连判据一起记")
        blk = src[i:i + 3000]
        for kw in ("A_engine_gap", "B_h5i_gap", "no_gap", "trigger"):
            assert kw in blk, f"实例表里缺 {kw}"
        assert "打印出来完全一样" in blk or "打印出来一模一样" in blk, (
            "必须点明前两行**输出相同**(这才是问题所在)")

    def test_explains_relation_to_form5_as_generalization(self):
        """必须写清与 ⑤ 的关系是**更一般化**, 而不是重复。"""
        src = self._src()
        i = src.find("结论相同、来由不同处, 必须连判据一起记")
        blk = src[i:i + 3000]
        assert "更一般化" in blk, "应说明本条是 ⑤ 的推广"
        assert "每层" in blk or "每層" in blk, "应点出 ⑤ 的落点是「保留每层 error」"
        assert "特例" in blk, "应点明 ⑤ 是本条在错误传递场景下的特例"


class TestJudgementRuleMustDiscriminate:
    """⑦ 必须补「这个判据, 能区分对错吗?」—— 且用「叙述区段数 <= 4」作反例 (2026-09-26 用户要求)。"""

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_second_criterion_present(self):
        src = self._src()
        i = src.find("这个判据, 能区分对错吗?")
        assert i > 0, "缺「这个判据能区分对错吗」这条判据"
        blk = src[i:i + 2600]
        assert "两种情况下结果相同" in blk or "两种情况" in blk, (
            "必须给出判据: 两种情况结果是否不同")

    def test_names_the_segment_count_counterexample(self):
        """必须写明反例就是「叙述区段数 <= 4」, 且说明**方向相反**。"""
        src = self._src()
        i = src.find("这个判据, 能区分对错吗?")
        blk = src[i:i + 2600]
        assert "段数" in blk, "反例应点名「段数」这条判据"
        assert "34" in blk, "应给出实测段数(34 段), 否则读者不知为何它错"
        assert "方向相反" in blk or "相反" in blk, (
            "必须点明它不只是无效, 而是**方向相反**(做对了失败、做错了通过)")

    def test_gives_the_replacement_and_contrast_table(self):
        src = self._src()
        i = src.find("这个判据, 能区分对错吗?")
        blk = src[i:i + 2600]
        assert "占幅" in blk, "应给出替换后的判据(占幅 < 半篇)"
        assert "能区分" in blk, "应有对照表(能区分? 列)"

    def test_links_to_negative_sample_rule(self):
        """必须与既有「负样本必须能触发守卫」挂钩, 否则两条规则看着重复。"""
        src = self._src()
        i = src.find("这个判据, 能区分对错吗?")
        blk = src[i:i + 2600]
        assert "负样本" in blk, "应说明它与「负样本必须能触发守卫」是同一件事的两个说法"


class TestFirstBytesMustBeVerified:
    """环境节必须固化「凡要交给别的程序读的文件, 写完都验一次首字节」(2026-09-26 用户要求)。"""

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_rule_present_in_env_section(self):
        src = self._src()
        i = src.find("凡要交给别的程序读的文件, 写完都验一次首字节")
        assert i > 0, "环境节缺「验首字节」这条"
        blk = src[i:i + 3200]
        for kw in ("bytes", "NULs", "BOM"):
            assert kw in blk, f"必须给出三项验收({kw})"

    def test_records_both_real_occurrences(self):
        """本仓实测两次必须都记 —— 只记一次会显得像偶发。"""
        src = self._src()
        i = src.find("凡要交给别的程序读的文件, 写完都验一次首字节")
        blk = src[i:i + 3200]
        assert "backfill_switch.json" in blk, "缺开关文件 BOM 那次"
        assert "NUL" in blk, "缺提交信息 NUL 那次"
        assert "Everything up-to-date" in blk, (
            "应记下它的**连带症状**(push 报 up-to-date, 看起来像已推过)")

    def test_gives_the_one_shot_write_and_read_defence(self):
        src = self._src()
        i = src.find("凡要交给别的程序读的文件, 写完都验一次首字节")
        blk = src[i:i + 3200]
        assert "UTF8Encoding($false)" in blk, "必须给一次性写对的写法"
        assert "utf-8-sig" in blk, "读侧必须给防御(utf-8-sig)"
        assert "吞成默认值" in blk or "吞掉" in blk, (
            "必须点明第 1 次就是被 except 吞掉才变成静默")

    def test_this_doc_does_not_quote_the_anchor_phrase(self):
        """**反向验证**: 这一节**不得**再引用 DISC-1 的定位锚点原句。

        实测: 我第一版正是引用了它 ⇒ 两条 DISC-1 守卫失败(见
        `TestDocAnchorPhrasesAreUnique`)。故这里锁住"本节的写法不会再犯"。
        """
        src = self._src()
        i = src.find("凡要交给别的程序读的文件, 写完都验一次首字节")
        blk = src[i:i + 3200]
        assert "宁可 None, 不猜" not in blk, (
            "本节又引用了 DISC-1 的定位锚点原句 —— 会劫持那两条 DISC-1 守卫")


class TestDisc2FormIndex:
    """DISC-2 的「失效形态索引」必须与详细章节**对得上** (2026-09-23, 用户建议)。

    用户建议把 5 种形态列成一张带**排查优先级**的表。索引的价值全在"能扫一眼决定
    先查哪个"; 一旦它与下面的详细章节脱节(改了形态却没改索引, 或反之),
    它就从"索引"退化成"另一段会腐烂的文字" —— 而**索引腐烂比没有索引更糟**,
    因为它会让人以为已经覆盖了, 实际指向的是过期的形态清单。
    """

    #: 全部形态。**③b 与 ④b 都是"并列"形态**, 故不是简单的 1..7。
    #: 判据 = 「索引表里有一行」且「详细章节里有对应小节/行」。
    _FORMS = ("①", "②", "③", "③b", "④", "④b", "⑤", "⑥", "⑦")
    #: 索引表结束的位置 —— 详细章节的标题。**改标题时这里必须同步**,
    #: 否则切片会一路取到文末, 断言随之变弱(2026-09-26 实测发生, 即 ④b 本身)。
    _DETAIL_HEADING = "### 详细判据"

    def _src(self):
        return open(_DOC, encoding="utf-8").read()

    def test_index_section_exists_and_precedes_details(self):
        src = self._src()
        i_idx = src.find("### 失效形态索引")
        assert i_idx > 0, "缺「失效形态索引」小节"
        i_detail = src.find(self._DETAIL_HEADING)
        assert i_detail > 0, f"缺详细章节(标题应以「{self._DETAIL_HEADING}」开头)"
        assert i_idx < i_detail, (
            "索引必须排在详细章节**之前** —— 它的用途就是"
            "让人先决定查哪个, 再往下读细节")

    def test_the_detail_heading_did_not_rot(self):
        """反向验证: 详细章节标题必须真的存在, 否则切片会静默跑到文末。

        [2026-09-26 实测] 该标题原为 `### 四种"测试看起来正常"…`, 后来改成
        `### 详细判据: 四种… + 三种…`。而**两个用例仍按 `### 四种` 切片** ⇒
        `find` 返回 -1 ⇒ `src[i:-1]` 取到**几乎整篇** ⇒
        断言"索引里没有多列形态"**恒真**(详细章节的内容把它喂饱了)。
        这是 ③b 的近亲: **切片边界依赖了一个被改掉的字面量**。

        ## 为什么用"行首锚定"而不是 `src.count(...)`

        第一版写的是 `src.count(_DETAIL_HEADING) == 1` —— 结果**报了我自己**:
        我在 ④b 的说明里**引用了**该标题的字面量(`find("### 四种")` 这种例子),
        于是计数变成 2。**这正是"识别条件必须覆盖该节所有可能格式"的又一实例**:
        说明文字与真实标题**同形**, 故判据必须收窄到"**行首**且**不是引用行**"。
        """
        src = self._src()
        lines = src.splitlines()

        def is_real_heading(ln):
            # 行首锚定, 且排除 `>` 引用行里被引用的小节标题
            return ln.startswith(self._DETAIL_HEADING)

        hits = [n for n, ln in enumerate(lines) if is_real_heading(ln)]
        assert len(hits) == 1, (
            f"详细章节标题应**恰好一行以它开头**, 实际 {len(hits)} 行: "
            f"{[lines[n][:60] for n in hits]}")
        assert not any(lines[n].lstrip().startswith(">") for n in hits), (
            "命中的是引用行里的小节标题, 不是正文标题")
        i = src.find("### 失效形态索引")
        j = src.find(self._DETAIL_HEADING, i)
        assert 0 < i < j, "索引与详细章节的先后/存在性不对"
        # 索引块必须**短于**详细章节 —— 若索引块异常大, 说明切片边界失效了
        assert j - i < len(src) // 2, (
            "索引块占了半篇以上 —— 切片边界几乎肯定失效(边界字面量被改掉了)")

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
        # 判据也按**行首锚定**: 若只查子串, ④b 的说明里引用了 `### 四种` 这类例子,
        # 会把守卫自己举例的文字当成"夹着的小节"(2026-09-26 实测)。
        head_lines = head.splitlines()
        for sub in ("### 事实依据", "### 关于", "### 详细判据"):
            assert not any(ln.startswith(sub) for ln in head_lines), (
                f"入口与索引之间夹着 `{sub}` —— 入口必须紧邻索引; "
                "读者会先读完那一节才看到索引, 入口形同虚设")

    def test_index_covers_exactly_the_eight_rows(self):
        """索引表必须列出**全部**形态(现为 8 行: ① ② ③ ③b ④ ⑤ ⑥ ⑦)。

        多列 = 索引里有详细章节没写的形态(读者找不到细节);
        少列 = 新形态只写进正文却没进索引(读者扫不到)。
        两个方向都要拦。

        [2026-09-25] 用户要求补第 ⑥ 种「降级过程无告警」, 由五种扩到六种;
        [2026-09-26] 用户要求补第 ⑦ 种「证据无判别力」与 **③b「前置是环境事实」**,
        故由六种扩到八行(③b 与 ③ 并列, 不是第 9 种)。
        **这条断言必须跟着改** —— 若只改文档不改它, 用例会以"找不到 ⑦"失败
        (这是好事: 说明守卫真的在盯索引与正文的一致性)。
        """
        src = self._src()
        i = src.find("### 失效形态索引")
        j = src.find(self._DETAIL_HEADING, i)
        assert 0 < i < j, "索引/详细章节边界不对 —— 先修 test_the_detail_heading_did_not_rot"
        index_block = src[i:j]
        for f in self._FORMS:
            # 行首形如 `| **④ 标题**`; ③b 的 b 在 ** 里面, 故同时接受 `| **③b`
            assert (f"| **{f} " in index_block) or (f"| **{f}**" in index_block), (
                f"索引里缺形态 {f} 的行")
        # 反向: 索引里不得出现 _FORMS 之外的编号(如 ⑧) —— 那说明正文没写
        import re as _re
        numbered = set(_re.findall(r"\*\*([①-⑳])(?:b)?\s", index_block))
        known = {f[0] for f in self._FORMS}       # ③b -> ③
        extra = {n for n in numbered if n not in known}
        assert not extra, f"索引里有正文未写的形态: {sorted(extra)}"
        # 详细章节那边也必须都在: 前几种在对照表里以 `| **① ` 起行,
        # ⑤⑥⑦ 各有独立的 `### ⑤ ...` / `### ⑥ ...` / `### ⑦ ...` 小节标题,
        # ③b 有 `#### ③b ...` 小节。
        detail = src[j:]
        for f in self._FORMS:
            assert f"**{f}" in detail or f"### {f}" in detail or f"#### {f}" in detail, \
                f"详细章节里找不到形态 {f}"

    def test_index_marks_the_hard_to_find_forms_as_high_priority(self):
        """④/④b/⑤/⑥/⑦ 必须被标为**高**优先级, 且理由写出来。

        为什么单锁这几个字符: 用户的原话是「⑤ 和 ④ 应该优先检查 —— 它们最难发现,
        且会把人引向错误方向」; 2026-09-25 要求补 ⑥;
        2026-09-26 要求补 ⑦(证据无判别力)与 ④b(边界腐烂), 均明示与它们同级。
        若将来有人"顺手"把优先级都抹平, 这张表就只剩装饰作用 ——
        而"没有优先级的索引"与"没有索引"在排查时的效果一样。
        """
        src = self._src()
        i = src.find("### 失效形态索引")
        j = src.find(self._DETAIL_HEADING, i)
        block = src[i:j]
        for f in ("④", "④b", "⑤", "⑥", "⑦"):
            row = [ln for ln in block.splitlines() if ln.strip().startswith(f"| **{f}")]
            assert row, f"索引表里找不到形态 {f} 的行"
            assert "高" in row[0], f"形态 {f} 未被标为高优先级: {row[0][:90]}"
        # ③/③b 是中等(它们会红, 但要误判方向才贵)
        for f in ("③", "③b"):
            row = [ln for ln in block.splitlines() if ln.strip().startswith(f"| **{f} ")]
            assert row, f"索引表里找不到形态 {f} 的行"
            assert "中" in row[0], f"{f} 应为中优先级: {row[0][:90]}"
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
        j = src.find(self._DETAIL_HEADING, i)
        assert 0 < i < j, f"索引/详细章节边界不对(边界字面量可能被改掉了)"
        block = src[i:j]
        for f in self._FORMS:
            rows = [ln for ln in block.splitlines()
                    if ln.strip().startswith(f"| **{f} ")]
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
