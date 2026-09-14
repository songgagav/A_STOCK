# -*- coding: utf-8 -*-
"""估值覆盖率哨兵的判级逻辑 (scripts/valuation_coverage_sentinel.classify).

口径 (2026-09-14 观察期首日校准):
    主判据是 **可用 ep 率** (pe_ttm > 0, 主表 ∪ 补丁), 而非 pe_ttm 非空率。
    因为重建把亏损公司的负 PE 置 NULL, 而快照保留负值, 两者非空率差很多
    (70.6% vs 100.0%), 但可用 ep 率几乎相同 (70.6% vs 72.0%)。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))


def _c(ep_usable, raw_pe, raw_pb=1.0, raw_fs=1.0):
    from valuation_coverage_sentinel import classify
    return classify(ep_usable, raw_pe, raw_pb, raw_fs)


def test_healthy_day_is_ok():
    assert _c(0.70, 0.70)[0] == "OK"


def test_snapshot_day_with_negative_pe_is_ok():
    """快照源保留负 PE: 非空率 100% 但可用 ep 仅 72%, 属正常."""
    assert _c(0.72, 1.00)[0] == "OK"


def test_rebuild_day_at_structural_ceiling_is_ok():
    """重建源: 非空率≈可用 ep≈69.7%, 是结构上限(亏损公司无正值 PE), 不应告警.

    这是 2026-09-14 的回归点 —— 旧判据 (非空率 < 70% -> WARN) 会天天误报。
    """
    assert _c(0.697, 0.697)[0] == "OK"


def test_source_gap_with_patch_backup_is_warn():
    """主表非空率极低但补丁兜住可用 ep -> 上游有问题, 因子层尚可, 记 WARN."""
    lvl, why = _c(0.65, 0.05)
    assert lvl == "WARN"
    assert "上游写入缺口" in why


def test_factor_layer_degradation_is_critical():
    lvl, why = _c(0.30, 0.30)
    assert lvl == "CRITICAL"
    assert "因子层已实质退化" in why


def test_pb_collapse_is_critical_regardless_of_pe():
    lvl, why = _c(0.70, 0.70, raw_pb=0.40)
    assert lvl == "CRITICAL"
    assert "pb" in why


def test_below_structural_range_is_warn():
    lvl, why = _c(0.55, 0.55)
    assert lvl == "WARN"
    assert "结构区间" in why


def test_float_shares_gap_is_warn():
    lvl, why = _c(0.70, 0.70, raw_fs=0.80)
    assert lvl == "WARN"
    assert "float_shares" in why
