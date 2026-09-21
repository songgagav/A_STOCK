# -*- coding: utf-8 -*-
"""`live_source` 判据的回归测试 (2026-09-21).

问题
----
原逻辑是「候选池里**任一只**缺价 ⇒ 整批标 `duckdb_reference`」。这个字段本应回答
**「我现在是按实时价撮合吗」**, 但那个判据让它**失去区分能力**:

  实测当天: 池 10 只中有个别缺价 ⇒ 整批标 `duckdb_reference`,
  可持仓 `301655.SZ` 的价格其实是**实时在动**的 (80 秒内 23.29 → 23.20)。

于是一个**账户完全健康**的会话, 在状态文件里看起来和"账户被静态价兜底"一模一样。
这正是本仓最忌讳的那类信号退化: **字段还在, 但已经不携带信息**。

新语义
------
  `akshare_spot`            持仓与候选池全部取到实时价
  `duckdb_reference_pool`   仅候选池有缺价 —— 账户仍是实时估值, 影响的是**下次调仓**
  `duckdb_reference_held`   持仓有缺价 —— 账面已用最近收盘价兜底, **不能**当实时看
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

realtime_engine = pytest.importorskip("realtime_engine")
_f = realtime_engine._live_src_label


class TestLiveSrcLabel:
    def test_all_live(self):
        assert _f([], []) == "akshare_spot"

    def test_pool_missing_only_is_not_a_held_problem(self):
        """**本次修复的核心**: 仅候选池缺价时, 账户仍是实时估值。"""
        assert _f([], ["600000.SH", "000001.SZ"]) == "duckdb_reference_pool"

    def test_held_missing_dominates(self):
        """持仓缺价 ⇒ 必须标成 held —— 这是唯一该被当作"账面不可信"的情形。"""
        assert _f(["301655.SZ"], ["600000.SH"]) == "duckdb_reference_held"
        assert _f(["301655.SZ"], []) == "duckdb_reference_held"

    def test_held_takes_precedence_over_pool(self):
        """两者都缺时, 必须报更严重的那个, 不能退化成 pool。"""
        assert _f(["A"], ["B", "C"]) != "duckdb_reference_pool"

    def test_old_behaviour_would_have_mislabelled(self):
        """固化旧行为之弊: 旧判据 `if missing: 'duckdb_reference'` 在"仅池缺价"时
        会把一个**实时估值**的账户标成与"持仓被兜底"无法区分。此处断言新标签
        与旧的单一值**不同**, 使回退到旧判据会立刻被抓住。
        """
        old = "duckdb_reference"
        assert _f([], ["600000.SH"]) != old, "仅池缺价不应再退化成无法区分的单一标记"
        assert _f(["301655.SZ"], []) != old, "持仓缺价也应有自己的标记"

    def test_all_three_labels_are_distinct(self):
        labels = {_f([], []), _f([], ["X"]), _f(["X"], [])}
        assert len(labels) == 3, f"三种状态必须可区分, 实得 {labels}"
