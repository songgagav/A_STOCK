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

    def test_placeholder_disciplines_are_marked_as_candidates(self):
        """留位的纪律必须**标明是候选**, 不能看起来像已生效。"""
        src = open(_DOC, encoding="utf-8").read()
        for n in ("DISC-2", "DISC-3"):
            if n in src:
                idx = src.find(n)
                near = src[idx:idx + 120]
                assert "候选" in near, f"{n} 未标明「候选」 —— 会被误读为已生效纪律"
