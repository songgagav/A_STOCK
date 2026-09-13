# -*- coding: utf-8 -*-
"""估值覆盖率哨兵的判级逻辑 (scripts/valuation_coverage_sentinel.classify)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))


def _c(raw_pe, patched_usable, raw_pb=1.0, raw_fs=1.0):
    from valuation_coverage_sentinel import classify
    return classify(raw_pe, patched_usable, raw_pb, raw_fs)


def test_healthy_day_is_ok():
    assert _c(0.85, 0.60)[0] == "OK"


def test_normal_loss_maker_share_is_not_flagged():
    """77%~90% 属于历史正常区间(缺口≈亏损公司), 不应告警."""
    assert _c(0.78, 0.55)[0] == "OK"


def test_source_gap_with_patch_backup_is_warn():
    """主表缺口但补丁兜住了 -> 上游有问题, 因子层尚可, 记 WARN."""
    lvl, why = _c(0.05, 0.72)
    assert lvl == "WARN"
    assert "上游写入缺口" in why


def test_source_gap_without_backup_is_critical():
    lvl, why = _c(0.05, 0.30)
    assert lvl == "CRITICAL"
    assert "因子层已实质退化" in why


def test_pb_collapse_is_critical_regardless_of_pe():
    lvl, why = _c(0.90, 0.80, raw_pb=0.40)
    assert lvl == "CRITICAL"
    assert "pb" in why


def test_mildly_low_pe_is_warn():
    assert _c(0.60, 0.55)[0] == "WARN"


def test_float_shares_gap_is_warn():
    lvl, why = _c(0.85, 0.60, raw_fs=0.80)
    assert lvl == "WARN"
    assert "float_shares" in why
