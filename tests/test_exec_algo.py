# -*- coding: utf-8 -*-
"""单元测试: src/exec_algo.py —— TWAP / VWAP 拆单规划 + Almgren-Chriss 滑点仿真.

覆盖重点(与任务书一一对应):
  1. `sum(slices) == total_qty` 在多组 (total_qty, n_slices, lot) 下成立(整手可执行量口径);
  2. TWAP 各档差异 <= 1 手;
  3. VWAP 高成交量档分到更多手数(弱单调 + Spearman 相关), 0 占比档严格不分单且不除零;
  4. 非法 algo / 非法 volume_profile / 非法参数一律 ValueError(不静默回退、不静默修补);
  5. `participation_cap_slices` 在极小 ADV 下如实返回 `unscheduled > 0` 与原因;
  6. `simulate_execution` 的 saving_bps 可正可负(大单低 ADV 为正; 执行风险主导为负),
     证明它没有被 `max(0, ·)` 粉饰;
  7. 整手约束: 买入拆单每档都是 lot 的整数倍;
  8. 模块卫生: 只 import 标准库 + numpy + slippage_model, 不拉 paper_book/config。

conftest.py 已把 src/ 加入 sys.path, 故直接 `import exec_algo`。
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import numpy as np
import pytest

import exec_algo
from exec_algo import (
    DEFAULT_N_SLICES_PLACEHOLDER,
    PARTICIPATION_CAP_PCT_PLACEHOLDER,
    lot_split,
    naive_slippage_bps,
    participation_cap_slices,
    plan_execution,
    schedule_twap,
    schedule_vwap,
    simulate_execution,
    _main,
)

REQUIRED_PLAN_KEYS = {"algo", "slices", "total_scheduled", "unscheduled", "n_slices", "notes"}
REQUIRED_CAP_KEYS = {"slices", "n_slices", "unscheduled", "reason"}
REQUIRED_SIM_KEYS = {
    "slice_vwap_price", "naive_price", "slippage_bps_sliced", "slippage_bps_naive",
    "saving_bps", "per_slice", "assumptions",
}


# --------------------------------------------------------------------------
# 测试工具
# --------------------------------------------------------------------------
def _spearman(xs, ys) -> float:
    """手写 Spearman 秩相关(避免引入 scipy 依赖); 秩用平均秩处理并列值。"""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return np.asarray(r, dtype=np.float64)

    ra, rb = ranks(list(xs)), ranks(list(ys))
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    return 0.0 if denom == 0 else float((ra * rb).sum() / denom)


# ==========================================================================
# 1) lot_split: 零头必须被显式暴露
# ==========================================================================
def test_lot_split_returns_tradable_and_remainder():
    assert lot_split(1000, 100) == (1000, 0)
    assert lot_split(1050, 100) == (1000, 50)
    assert lot_split(50, 100) == (0, 50)
    assert lot_split(0, 100) == (0, 0)
    assert lot_split(150, 1) == (150, 0)


def test_lot_split_conserves_total_qty():
    for qty in (0, 1, 50, 99, 100, 101, 1050, 999_999):
        for lot in (1, 10, 100):
            tradable, rem = lot_split(qty, lot)
            assert tradable + rem == qty
            assert tradable % lot == 0
            assert 0 <= rem < lot


@pytest.mark.parametrize("bad_qty,bad_lot", [(-1, 100), (10.5, 100), (100, 0), (100, -5), (100, None)])
def test_lot_split_invalid_args_raise(bad_qty, bad_lot):
    with pytest.raises(ValueError):
        lot_split(bad_qty, bad_lot)


# ==========================================================================
# 2) schedule_twap
# ==========================================================================
@pytest.mark.parametrize("total_qty,n_slices,lot", [
    (1000, 1, 100),
    (1000, 3, 100),
    (1000, 7, 100),
    (5000, 10, 100),
    (400, 3, 100),
    (2000, 4, 100),
    (300, 2, 100),
    (12345, 8, 100),
    (500, 10, 100),      # 档数 > 可分手数
    (100, 100, 100),     # 档数 >> 可分手数
    (1000, 6, 10),       # 非百股 lot
    (999, 5, 1),         # lot=1
])
def test_twap_sum_equals_tradable_qty(total_qty, n_slices, lot):
    slices = schedule_twap(total_qty, n_slices, lot=lot)
    tradable, remainder = lot_split(total_qty, lot)
    assert len(slices) == n_slices
    assert sum(slices) == tradable               # 整手可执行量全部被安排
    assert tradable + remainder == total_qty     # 零头在 lot_split 里, 未凭空消失
    assert all(s % lot == 0 for s in slices)
    assert all(s >= 0 for s in slices)


@pytest.mark.parametrize("total_qty,n_slices", [(1000, 3), (1000, 7), (5000, 10), (2000, 4), (100000, 10)])
def test_twap_sum_equals_total_qty_when_lot_multiple(total_qty, n_slices):
    """total_qty 是 lot 整数倍时, sum(slices) 严格等于 total_qty(任务书要求的等式)."""
    slices = schedule_twap(total_qty, n_slices, lot=100)
    assert sum(slices) == total_qty


@pytest.mark.parametrize("total_qty,n_slices,lot", [
    (1000, 3, 100), (1000, 7, 100), (12345, 8, 100), (400, 3, 100), (500, 10, 100), (999, 5, 1),
])
def test_twap_max_min_diff_at_most_one_lot(total_qty, n_slices, lot):
    """TWAP 各档差异 <= 1 手(含"档数多于手数"时尾部空档的情形)."""
    slices = schedule_twap(total_qty, n_slices, lot=lot)
    assert max(slices) - min(slices) <= lot


def test_twap_front_loaded_remainder_exact():
    """余数规则: 前重后轻 + 逐手摊 —— 1000 股 / 3 档 → [400, 300, 300]."""
    assert schedule_twap(1000, 3) == [400, 300, 300]
    assert schedule_twap(1050, 3) == [400, 300, 300]     # 零头 50 不进任何档
    assert schedule_twap(1000, 7) == [200, 200, 200, 100, 100, 100, 100]


def test_twap_more_slices_than_lots_fills_head_then_zeros():
    """档数多于可分手数: 前几档各 1 手, 尾部空档为 0(不做 lot 以下的小数拆分)."""
    slices = schedule_twap(500, 10)
    assert slices == [100] * 5 + [0] * 5
    assert len(slices) == 10
    assert sum(1 for s in slices if s == 0) == 5
    assert all(s == 0 for s in slices[5:])


def test_twap_total_less_than_lot_all_zero_and_remainder_reported():
    """total_qty < lot: 全部档为 0, 整个母单都是零头(不静默丢弃)."""
    slices = schedule_twap(50, 3)
    assert slices == [0, 0, 0]
    assert lot_split(50, 100) == (0, 50)
    plan = plan_execution(50, algo="twap", n_slices=3)
    assert plan["total_scheduled"] == 0
    assert plan["unscheduled"] == 50
    assert any("零头" in n for n in plan["notes"])


def test_twap_zero_total_all_zero():
    assert schedule_twap(0, 4) == [0, 0, 0, 0]


def test_twap_deterministic():
    assert schedule_twap(12345, 8) == schedule_twap(12345, 8)


@pytest.mark.parametrize("kwargs", [
    {"total_qty": -100, "n_slices": 3},
    {"total_qty": 1000, "n_slices": 0},
    {"total_qty": 1000, "n_slices": -1},
    {"total_qty": 1000, "n_slices": 2.5},
    {"total_qty": 1000, "n_slices": 3, "lot": 0},
    {"total_qty": 1000, "n_slices": 3, "lot": -100},
    {"total_qty": 1000, "n_slices": 3, "side": "hold"},
    {"total_qty": 1000, "n_slices": 3, "side": "BUY"},
    {"total_qty": 1000.5, "n_slices": 3},
])
def test_twap_invalid_args_raise(kwargs):
    with pytest.raises(ValueError):
        schedule_twap(**kwargs)


def test_twap_sell_lot1_allows_odd_lots():
    """卖出零股: 必须由调用方显式传 lot=1, 而不是靠 side='sell' 偷偷放宽. ——"""
    assert schedule_twap(150, 2, lot=1, side="sell") == [75, 75]
    assert schedule_twap(150, 2, lot=100, side="sell") == [100, 0]   # 默认仍按整手, 零头报在 lot_split
    assert lot_split(150, 100) == (100, 50)


# ==========================================================================
# 3) schedule_vwap
# ==========================================================================
@pytest.mark.parametrize("total_qty,lot", [(1000, 100), (12345, 100), (5000, 100), (999, 1), (100, 100)])
def test_vwap_sum_equals_tradable_qty(total_qty, lot):
    profile = [1.0, 3.0, 0.0, 6.0, 2.0]
    slices = schedule_vwap(total_qty, profile, lot=lot)
    tradable, remainder = lot_split(total_qty, lot)
    assert len(slices) == len(profile)
    assert sum(slices) == tradable
    assert tradable + remainder == total_qty
    assert all(s % lot == 0 for s in slices)


def test_vwap_sum_equals_total_qty_when_lot_multiple():
    assert sum(schedule_vwap(1000, [1, 2, 3, 4])) == 1000
    assert sum(schedule_vwap(1000, [0.1, 0.2, 0.7])) == 1000


def test_vwap_exact_allocation_known_case():
    assert schedule_vwap(1000, [3, 1]) == [800, 200]
    assert schedule_vwap(1000, [1, 1, 1, 10]) == [100, 100, 100, 700]
    assert schedule_vwap(1000, [1, 1, 1, 1]) == [300, 300, 200, 200]   # 最大余数法 + 索引小者优先
    # 小数部分 0.952/0.905/0.857/0.476 最大 → 第 1/3/5/0 档各补 1 手
    assert schedule_vwap(1000, [1, 2, 3, 4, 5, 6]) == [100, 100, 100, 200, 200, 300]


def test_vwap_scale_invariance():
    """占比无需归一化: 整体缩放不改变结果."""
    assert schedule_vwap(1000, [1, 3, 6]) == schedule_vwap(1000, [100, 300, 600])
    assert schedule_vwap(1000, [0.1, 0.3, 0.6]) == schedule_vwap(1000, [1, 3, 6])


def test_vwap_flat_profile_matches_twap_shape():
    """平坦量分布时, VWAP 的最大余数法退化为 TWAP 的"前重后轻"(规则自洽性检查)."""
    for n in (1, 2, 3, 4, 7, 10):
        for qty in (0, 50, 100, 1000, 1050, 12345):
            assert schedule_vwap(qty, [1.0] * n) == schedule_twap(qty, n)


def test_vwap_zero_buckets_get_nothing_and_no_zero_division():
    """0 占比档不分单, 且不触发除零(归一化分母只用正值之和)."""
    assert schedule_vwap(5000, [0.0, 2.0, 0.0, 3.0, 0.0]) == [0, 2000, 0, 3000, 0]
    assert schedule_vwap(1000, [0, 0, 7, 0]) == [0, 0, 1000, 0]
    assert schedule_vwap(100, [0, 0, 0, 0, 1]) == [0, 0, 0, 0, 100]


def test_vwap_zero_buckets_do_not_receive_leftover_lots():
    """余数补发阶段也不会把零头漏给 0 占比档(999999 股 / 双正档)."""
    slices = schedule_vwap(999_999, [0, 1, 0, 1])
    assert slices[0] == 0 and slices[2] == 0
    assert sum(slices) == 999_900
    assert abs(slices[1] - slices[3]) <= 100


@pytest.mark.parametrize("profile", [
    [1.0, 2.0, 3.0, 4.0, 5.0],
    [0.0, 1.0, 5.0, 0.0, 20.0],
    [9.0, 8.0, 7.0, 2.0, 1.0],
    [0.0, 0.0, 1.0, 0.0, 0.0],
    [1.0, 1.0, 1.0, 1.0],
    [0.001, 100.0, 0.001, 50.0],
])
def test_vwap_weak_monotone_in_profile(profile):
    """VWAP 弱单调: 占比更大的档, 手数不会更少(最大余数法的配额性质保证)."""
    for qty in (1000, 12345, 100000, 350):
        slices = schedule_vwap(qty, profile)
        lots = [s // 100 for s in slices]
        for i in range(len(profile)):
            for j in range(len(profile)):
                if profile[i] > profile[j]:
                    assert lots[i] >= lots[j], (profile, lots, qty)


def test_vwap_higher_volume_bucket_gets_more_lots_and_positive_correlation():
    """高成交量档分到更多手数: Spearman 秩相关 = 1(严格递增的占比)."""
    profile = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    slices = schedule_vwap(2100, profile)
    assert slices == [100, 200, 300, 400, 500, 600]
    assert _spearman(profile, [s // 100 for s in slices]) == pytest.approx(1.0)
    # argmax 一致性: 量最大的档手数最多
    big = schedule_vwap(1000, [1, 1, 1, 10])
    assert big.index(max(big)) == 3


def test_vwap_more_slices_than_lots_favors_high_volume_buckets():
    """档数多于可分手数时, VWAP 把有限手数给**占比最高**的档(与 TWAP 的前置铺满不同)."""
    assert schedule_vwap(100, [1, 1, 1, 10]) == [0, 0, 0, 100]
    assert schedule_vwap(200, [1, 1, 1, 10]) == [0, 0, 0, 200]
    assert schedule_vwap(300, [1, 9, 1, 0]) == [0, 300, 0, 0]


def test_vwap_total_less_than_lot_all_zero():
    assert schedule_vwap(50, [1, 2, 3]) == [0, 0, 0]
    assert schedule_vwap(0, [1, 2, 3]) == [0, 0, 0]


@pytest.mark.parametrize("profile", [
    [-1.0, 2.0],            # 含负值: 不取绝对值/不截断
    [1.0, -0.5, 3.0],
    [],                     # 空序列
    [0.0, 0.0, 0.0],        # 全 0: 无法归一化
    [float("nan"), 1.0],    # NaN
    [float("inf"), 1.0],    # Inf
    [[1.0, 2.0], [3.0, 4.0]],   # 二维
    "not-a-profile",        # 非数值序列
    None,
])
def test_vwap_invalid_profile_raises(profile):
    with pytest.raises(ValueError):
        schedule_vwap(1000, profile)


@pytest.mark.parametrize("kwargs", [
    {"total_qty": -100, "volume_profile": [1, 2]},
    {"total_qty": 1000, "volume_profile": [1, 2], "lot": 0},
    {"total_qty": 1000, "volume_profile": [1, 2], "side": "sell_short"},
])
def test_vwap_invalid_args_raise(kwargs):
    with pytest.raises(ValueError):
        schedule_vwap(**kwargs)


# ==========================================================================
# 4) participation_cap_slices
# ==========================================================================
def test_cap_normal_case_schedules_everything():
    res = participation_cap_slices(1000, adv=1e6, cap_pct=0.1, lot=100, n_slices=10)
    assert set(REQUIRED_CAP_KEYS) <= set(res)
    assert res["slices"] == [100] * 10
    assert res["scheduled_qty"] == 1000
    assert res["unscheduled"] == 0
    assert res["reason"].startswith("ok")
    assert res["cap_per_slice"] == 100000
    assert res["daily_cap"] == 1000000


def test_cap_tiny_adv_reports_unscheduled_honestly():
    """极小 ADV: 如实返回吃不掉的数量与原因, 绝不假装全成."""
    res = participation_cap_slices(100000, adv=5000, cap_pct=0.1, lot=100, n_slices=10)
    assert res["unscheduled"] == 95000
    assert res["unscheduled"] > 0
    assert res["scheduled_qty"] == 5000
    assert sum(res["slices"]) == 5000
    assert res["slices"] == [500] * 10
    assert "adv_too_small" in res["reason"]
    assert "95000" in res["reason"]


def test_cap_adv_below_one_lot_yields_zero_slices():
    res = participation_cap_slices(100000, adv=50, cap_pct=0.1, lot=100, n_slices=10)
    assert res["cap_per_slice"] == 0
    assert res["daily_cap"] == 0
    assert res["scheduled_qty"] == 0
    assert res["slices"] == [0] * 10
    assert res["unscheduled"] == 100000
    assert "adv_too_small" in res["reason"]


def test_cap_per_slice_is_lot_multiple_and_daily_cap_consistent():
    res = participation_cap_slices(100000, adv=12345, cap_pct=0.1, lot=100, n_slices=7)
    assert res["cap_per_slice"] % 100 == 0
    assert res["cap_per_slice"] == 1200          # floor(0.1*12345/100)*100
    assert res["daily_cap"] == 1200 * 7
    assert sum(res["slices"]) == res["scheduled_qty"] == 1200 * 7
    assert res["unscheduled"] == 100000 - 8400


def test_cap_slices_are_all_lot_multiples_and_front_loaded():
    res = participation_cap_slices(10000, adv=3500, cap_pct=0.1, lot=100, n_slices=6)
    assert set(res["slices"]) == {300}
    # 单档上限 300 股 × 6 档 = 1800 >= 1000, 上限未绑定 → 10 手按前重后轻铺满: [2,2,2,2,1,1]
    res2 = participation_cap_slices(1000, adv=3500, cap_pct=0.1, lot=100, n_slices=6)
    assert res2["slices"] == [200, 200, 200, 200, 100, 100]      # 前重后轻
    assert res2["unscheduled"] == 0
    assert max(res2["slices"]) - min(res2["slices"]) <= 100


def test_cap_odd_lot_is_reported_in_reason_and_unscheduled():
    res = participation_cap_slices(1050, adv=1e7, cap_pct=0.1, lot=100, n_slices=10)
    assert res["odd_lot"] == 50
    assert res["tradable_qty"] == 1000
    assert res["scheduled_qty"] == 1000
    assert res["unscheduled"] == 50
    assert "odd_lot" in res["reason"]
    assert res["reason"].startswith("ok")        # 流动性够, 只是零头进不去


def test_cap_placeholder_flag_is_exposed():
    res = participation_cap_slices(1000, adv=1e6)
    assert res["cap_pct"] == PARTICIPATION_CAP_PCT_PLACEHOLDER
    assert res["cap_pct_is_placeholder"] is True
    assert res["n_slices"] == DEFAULT_N_SLICES_PLACEHOLDER
    res2 = participation_cap_slices(1000, adv=1e6, cap_pct=0.03)
    assert res2["cap_pct_is_placeholder"] is False


def test_cap_single_slice_is_literal_one_day_budget():
    """n_slices=1 时退化为字面意义的"一天": 窗口上限 == 单档上限 == cap_pct × ADV."""
    res = participation_cap_slices(100000, adv=500000, cap_pct=0.1, lot=100, n_slices=1)
    assert res["cap_per_slice"] == 50000
    assert res["daily_cap"] == 50000
    assert res["scheduled_qty"] == 50000
    assert res["slices"] == [50000]
    assert res["unscheduled"] == 50000
    assert "adv_too_small" in res["reason"]
    assert "跨多个窗口/多日执行" in res["reason"]


@pytest.mark.parametrize("kwargs", [
    {"total_qty": -1, "adv": 1e6},
    {"total_qty": 1000, "adv": 0},
    {"total_qty": 1000, "adv": -1e6},
    {"total_qty": 1000, "adv": float("nan")},
    {"total_qty": 1000, "adv": 1e6, "cap_pct": 0},
    {"total_qty": 1000, "adv": 1e6, "cap_pct": -0.1},
    {"total_qty": 1000, "adv": 1e6, "cap_pct": 1.5},
    {"total_qty": 1000, "adv": 1e6, "lot": 0},
    {"total_qty": 1000, "adv": 1e6, "n_slices": 0},
])
def test_cap_invalid_args_raise(kwargs):
    with pytest.raises(ValueError):
        participation_cap_slices(**kwargs)


# ==========================================================================
# 5) plan_execution
# ==========================================================================
def test_plan_returns_required_keys_and_types():
    plan = plan_execution(1000, algo="twap", n_slices=5)
    assert REQUIRED_PLAN_KEYS <= set(plan)
    assert plan["algo"] == "twap"
    assert isinstance(plan["slices"], list) and len(plan["slices"]) == 5
    assert plan["n_slices"] == 5
    assert plan["total_scheduled"] == sum(plan["slices"]) == 1000
    assert plan["unscheduled"] == 0
    assert isinstance(plan["notes"], list) and plan["notes"]
    assert plan["side"] == "buy" and plan["lot"] == 100


@pytest.mark.parametrize("algo", ["TWAP", "VWAP", "vwap2", "twap ", "", "momentum", None, 1])
def test_plan_invalid_algo_raises_no_silent_fallback(algo):
    """非法 algo 必须抛 ValueError —— 不静默回退到 twap."""
    with pytest.raises(ValueError):
        plan_execution(1000, algo=algo, n_slices=4)


def test_plan_vwap_requires_volume_profile():
    with pytest.raises(ValueError):
        plan_execution(1000, algo="vwap", n_slices=4)
    with pytest.raises(ValueError):
        plan_execution(1000, algo="vwap", n_slices=4, volume_profile=None)


def test_plan_vwap_profile_length_mismatch_raises():
    """长度与档数不符: 明确抛 ValueError(不重采样/不截断)."""
    with pytest.raises(ValueError):
        plan_execution(1000, algo="vwap", n_slices=10, volume_profile=[1, 2, 3, 4, 5])
    with pytest.raises(ValueError):
        plan_execution(1000, algo="vwap", n_slices=2, volume_profile=[1, 2, 3])
    # 对齐后即通过
    assert sum(plan_execution(1000, algo="vwap", n_slices=5,
                              volume_profile=[1, 2, 3, 4, 5])["slices"]) == 1000


def test_plan_twap_with_volume_profile_raises():
    with pytest.raises(ValueError):
        plan_execution(1000, algo="twap", n_slices=4, volume_profile=[1, 2, 3, 4])


def test_plan_unscheduled_accounts_for_odd_lot():
    plan = plan_execution(1050, algo="twap", n_slices=3)
    assert plan["slices"] == [400, 300, 300]
    assert plan["total_scheduled"] == 1000
    assert plan["unscheduled"] == 50
    assert any("零头" in n for n in plan["notes"])


def test_plan_notes_when_slices_exceed_tradable_lots():
    plan = plan_execution(500, algo="twap", n_slices=10)
    assert plan["slices"] == [100] * 5 + [0] * 5
    assert plan["unscheduled"] == 0
    assert any("多于可分手数" in n for n in plan["notes"])


def test_plan_adv_cap_scales_down_keeps_shape_and_reports_shortfall():
    plan = plan_execution(100000, algo="twap", n_slices=10, adv=5000, cap_pct=0.1)
    assert plan["total_scheduled"] == 5000
    assert plan["unscheduled"] == 95000
    assert plan["slices"] == [500] * 10
    assert plan["cap_applied"] is True
    assert all(s % 100 == 0 for s in plan["slices"])
    assert any("参与率上限触发" in n for n in plan["notes"])


def test_plan_adv_cap_preserves_vwap_shape():
    """缩量按原各档手数等比进行, VWAP 的"量大的档更多手"形状不被破坏."""
    profile = [1, 1, 1, 7]
    plan = plan_execution(4000, algo="vwap", n_slices=4, volume_profile=profile,
                          adv=5000, cap_pct=0.1)
    # 窗口上限 = floor(0.1*5000/100)*100 * 4 = 2000 股 (原计划 4000)
    assert plan["total_scheduled"] == 2000
    assert plan["unscheduled"] == 2000
    lots = [s // 100 for s in plan["slices"]]
    assert lots == sorted(lots) and lots[-1] == max(lots)
    assert sum(plan["slices"]) == plan["total_scheduled"]


def test_plan_cap_placeholder_is_warned_in_notes():
    """使用占位默认 cap_pct=0.10 时必须留下显式告警(仓库纪律: 不得悄悄引入无依据数字)."""
    plan = plan_execution(1000, algo="twap", n_slices=10, adv=1e6)
    assert plan["cap_pct"] == PARTICIPATION_CAP_PCT_PLACEHOLDER
    assert any("占位默认值" in n and "cap_pct" in n for n in plan["notes"])


def test_plan_cap_without_adv_is_noted_not_silently_ignored():
    plan = plan_execution(1000, algo="twap", n_slices=10, cap_pct=0.05)
    assert plan["total_scheduled"] == 1000          # 无 ADV 则无法约束
    assert plan["cap_applied"] is False
    assert any("未生效" in n for n in plan["notes"])


def test_plan_cap_not_triggered_keeps_full_size():
    plan = plan_execution(1000, algo="twap", n_slices=10, adv=1e7, cap_pct=0.2)
    assert plan["total_scheduled"] == 1000
    assert plan["unscheduled"] == 0
    assert plan["cap_applied"] is False
    assert any("未触发" in n for n in plan["notes"])


@pytest.mark.parametrize("total_qty,n_slices,lot", [
    (1000, 5, 100), (1050, 3, 100), (500, 10, 100), (12345, 8, 100),
])
def test_plan_buy_slices_are_lot_multiples(total_qty, n_slices, lot):
    """整手约束: 买入拆单每档都是 lot 的整数倍(含 ADV 缩量后)."""
    plan = plan_execution(total_qty, algo="twap", n_slices=n_slices, lot=lot,
                          adv=1e6 if total_qty < 10000 else 3000, cap_pct=0.1)
    assert all(s % lot == 0 for s in plan["slices"])
    vplan = plan_execution(1000, algo="vwap", n_slices=4, volume_profile=[1, 2, 3, 4],
                           adv=5000, cap_pct=0.1)
    assert all(s % 100 == 0 for s in vplan["slices"])


def test_plan_vwap_zero_profile_bucket_note_and_result():
    plan = plan_execution(1000, algo="vwap", n_slices=4, volume_profile=[0, 1, 3, 0])
    assert plan["slices"] == [0, 300, 700, 0]
    assert any("占比为 0" in n for n in plan["notes"])


@pytest.mark.parametrize("kwargs", [
    {"total_qty": -1},
    {"total_qty": 1000, "n_slices": 0},
    {"total_qty": 1000, "n_slices": -3},
    {"total_qty": 1000, "lot": 0},
    {"total_qty": 1000, "side": "both"},
    {"total_qty": 1000, "algo": "twap", "adv": -5},
    {"total_qty": 1000, "algo": "twap", "adv": 1e6, "cap_pct": 2.0},
    {"total_qty": 1000, "algo": "vwap", "n_slices": 3, "volume_profile": [-1, 2, 3]},
])
def test_plan_invalid_args_raise(kwargs):
    with pytest.raises(ValueError):
        plan_execution(**kwargs)


def test_plan_deterministic():
    a = plan_execution(12345, algo="vwap", n_slices=5, volume_profile=[1, 2, 3, 4, 5])
    b = plan_execution(12345, algo="vwap", n_slices=5, volume_profile=[1, 2, 3, 4, 5])
    assert a == b


# ==========================================================================
# 6) naive_slippage_bps / simulate_execution
# ==========================================================================
def test_naive_slippage_bps_reuses_ac_and_clips_at_max():
    assert naive_slippage_bps(1e4, 5e8, 0.02) == pytest.approx(1.0, abs=0.01)
    assert naive_slippage_bps(1e6, 5e6, 0.03) == pytest.approx(200.0, abs=0.01)  # 封顶 200bps
    with pytest.raises(ValueError):
        naive_slippage_bps(1e6, 0.0, 0.03)


def _big_plan(qty=100000, n=10):
    return plan_execution(qty, algo="twap", n_slices=n)


def test_sim_returns_required_keys():
    plan = _big_plan()
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=5e6, volatility=0.03)
    assert REQUIRED_SIM_KEYS <= set(sim)
    assert len(sim["per_slice"]) == 10
    assert isinstance(sim["assumptions"], list) and len(sim["assumptions"]) >= 5
    assert all(isinstance(x, str) for x in sim["assumptions"])


def test_sim_large_order_low_adv_saving_positive():
    """大单 / 低 ADV: 拆单显著降低市场冲击, saving_bps > 0."""
    sim = simulate_execution(_big_plan(100000), [10.0] * 10,
                             avg_daily_volume=5e6, volatility=0.03)
    assert sim["slippage_bps_naive"] > sim["slippage_bps_sliced"]
    assert sim["saving_bps"] > 0
    assert sim["saving_bps"] == pytest.approx(168.55, abs=0.05)
    assert sim["slippage_bps_naive"] == pytest.approx(200.0, abs=0.01)   # 一次性下单顶到封顶


def test_sim_execution_risk_dominant_saving_negative_not_clamped():
    """执行风险主导: 拆单比一次性更贵 → saving_bps 必须是**负数**(证明没被 max(0,·) 粉饰)."""
    plan = plan_execution(10000, algo="twap", n_slices=10)
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=1e7,
                             volatility=0.06, window_horizon_days=3.0)
    assert sim["slippage_bps_sliced"] > sim["slippage_bps_naive"]
    assert sim["saving_bps"] < 0, "执行风险主导时 saving_bps 必须为负, 否则说明被粉饰"
    assert sim["saving_bps"] == pytest.approx(-9.0, abs=0.05)
    # 拆单的滑点里确实含执行风险分量, 而基准没有
    assert sim["per_slice"][0]["execution_risk_bps"] > 0
    assert sim["per_slice"][0]["horizon_days"] == pytest.approx(0.3)


def test_sim_saving_equals_naive_minus_sliced_exactly():
    """saving_bps 的定义是 plain 减法, 负数原样返回."""
    plan = plan_execution(10000, algo="twap", n_slices=10)
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=1e7,
                             volatility=0.06, window_horizon_days=3.0)
    assert sim["saving_bps"] == pytest.approx(
        sim["slippage_bps_naive"] - sim["slippage_bps_sliced"], abs=0.011)


def test_sim_per_slice_matches_decompose_slippage():
    """逐档滑点必须与本仓 slippage_model.decompose_slippage 完全一致(复用而非另写)."""
    from slippage_model import decompose_slippage
    plan = _big_plan(50000, 5)
    sim = simulate_execution(plan, [10.0] * 5, avg_daily_volume=1e7, volatility=0.04,
                             participation_per_slice=0.05)
    hor = sim["per_slice_horizon_days"]
    for row in sim["per_slice"]:
        dec = decompose_slippage(row["order_amount"], 1e7, 0.04,
                                 execution_horizon_days=hor, urgency_kappa=1.0)
        assert row["total_bps"] == pytest.approx(dec["total_bps"], abs=1e-9)
        assert row["market_impact_bps"] == pytest.approx(dec["market_impact_bps"], abs=1e-9)
        assert row["execution_risk_bps"] == pytest.approx(dec["execution_risk_bps"], abs=1e-9)


def test_sim_naive_crosscheck_uses_almglen_chriss():
    plan = _big_plan(10000, 10)
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=5e6, volatility=0.03)
    assert sim["naive_ac_crosscheck_bps"] == pytest.approx(sim["slippage_bps_naive"], abs=0.011)


def test_sim_single_slice_zero_horizon_equals_naive():
    """1 档 + 0 时长 = 与一次性下单完全等价 → saving 恰好为 0(无凭空收益)."""
    plan = plan_execution(10000, algo="twap", n_slices=1)
    sim = simulate_execution(plan, [10.0], avg_daily_volume=1e7, volatility=0.03,
                             window_horizon_days=0.0)
    assert sim["saving_bps"] == 0.0
    assert sim["slice_vwap_price"] == pytest.approx(sim["naive_price"], abs=1e-9)


def test_sim_price_extrapolation_is_noted():
    plan = _big_plan(100000, 10)
    sim = simulate_execution(plan, [10.0, 10.2], avg_daily_volume=5e6, volatility=0.03)
    assert [r["ref_price"] for r in sim["per_slice"]] == [10.0, 10.2] + [10.2] * 8
    assert any("外推" in a for a in sim["assumptions"])
    assert any("忽略价格漂移" in a for a in sim["assumptions"])


def test_sim_longer_price_list_is_truncated_and_noted():
    plan = _big_plan(100000, 10)
    sim = simulate_execution(plan, [10.0] * 15, avg_daily_volume=5e6, volatility=0.03)
    assert len(sim["per_slice"]) == 10
    assert any("截断" in a for a in sim["assumptions"])


@pytest.mark.parametrize("prices", [[], [10.0, -1.0], [0.0, 1.0], [float("nan")], [[1.0], [2.0]]])
def test_sim_invalid_prices_raise(prices):
    with pytest.raises(ValueError):
        simulate_execution(_big_plan(1000, 2), prices, avg_daily_volume=5e6, volatility=0.03)


def test_sim_zero_qty_slices_are_idle_and_excluded_from_weighted_price():
    plan = plan_execution(500, algo="twap", n_slices=10)
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=5e6, volatility=0.03)
    idle = [r for r in sim["per_slice"] if r["idle"]]
    assert len(idle) == 5
    assert all(r["qty"] == 0 and r["total_bps"] == 0.0 for r in idle)
    assert all(r["exec_price"] == r["ref_price"] for r in idle)
    assert sim["total_qty"] == 500
    assert sim["slice_vwap_price"] > 10.0     # 买入滑点方向正确


def test_sim_side_direction_affects_prices_but_not_saving():
    """买入价高于参考价、卖出价低于参考价; 而 saving_bps 只比滑点幅度, 与 side 无关."""
    buy = plan_execution(10000, algo="twap", n_slices=5, side="buy")
    sell = plan_execution(10000, algo="twap", n_slices=5, side="sell")
    kw = dict(avg_daily_volume=5e6, volatility=0.03)
    sb = simulate_execution(buy, [10.0] * 5, **kw)
    ss = simulate_execution(sell, [10.0] * 5, **kw)
    assert sb["slice_vwap_price"] > 10.0 > ss["slice_vwap_price"]
    assert sb["saving_bps"] == pytest.approx(ss["saving_bps"], abs=0.011)
    assert sb["slippage_bps_sliced"] == pytest.approx(ss["slippage_bps_sliced"], abs=0.011)


def test_sim_urgency_kappa_increases_risk_and_reduces_saving():
    plan = _big_plan(10000, 10)
    kw = dict(avg_daily_volume=1e7, volatility=0.03, window_horizon_days=2.0)
    low = simulate_execution(plan, [10.0] * 10, urgency_kappa=0.3, **kw)
    high = simulate_execution(plan, [10.0] * 10, urgency_kappa=2.0, **kw)
    assert high["slippage_bps_sliced"] > low["slippage_bps_sliced"]
    assert high["saving_bps"] < low["saving_bps"]


def test_sim_long_window_can_flip_saving_negative():
    """同一计划: 窗口越长执行风险越大, saving_bps 由正转负(单调恶化的可证伪性)."""
    plan = _big_plan(50000, 10)          # 单档参与率 1% 左右: 分散冲击的收益有限
    kw = dict(avg_daily_volume=1e7, volatility=0.05)
    short = simulate_execution(plan, [10.0] * 10, window_horizon_days=0.0, **kw)
    long = simulate_execution(plan, [10.0] * 10, window_horizon_days=60.0, **kw)
    assert short["saving_bps"] > 0
    assert long["saving_bps"] < short["saving_bps"]
    assert long["saving_bps"] < 0


def test_sim_placeholder_participation_is_noted():
    plan = _big_plan(10000, 10)          # plan 里没有 cap_pct
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=1e7, volatility=0.03)
    assert sim["participation_used"] == PARTICIPATION_CAP_PCT_PLACEHOLDER
    assert any("占位默认值" in a for a in sim["assumptions"])


def test_sim_uses_plan_cap_pct_when_available():
    plan = plan_execution(10000, algo="twap", n_slices=10, adv=1e7, cap_pct=0.05)
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=1e7, volatility=0.03)
    assert sim["participation_used"] == pytest.approx(0.05)
    assert "plan['cap_pct']" in sim["participation_source"]


def test_sim_explicit_participation_overrides_plan():
    plan = plan_execution(10000, algo="twap", n_slices=10, adv=1e7, cap_pct=0.05)
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=1e7, volatility=0.03,
                             participation_per_slice=0.2)
    assert sim["participation_used"] == pytest.approx(0.2)
    a = simulate_execution(plan, [10.0] * 10, avg_daily_volume=1e7, volatility=0.03,
                           participation_per_slice=0.2)
    assert a["window_horizon_days"] < simulate_execution(
        plan, [10.0] * 10, avg_daily_volume=1e7, volatility=0.03,
        participation_per_slice=0.05)["window_horizon_days"]


def test_sim_plan_without_side_falls_back_with_note():
    sim = simulate_execution({"slices": [1000]}, [10.0], avg_daily_volume=1e7,
                             volatility=0.03, window_horizon_days=0.0)
    assert sim["side"] == "buy"
    assert any("未携带 'side'" in a for a in sim["assumptions"])


def test_sim_horizon_derived_from_participation_and_clipped():
    plan = _big_plan(10000, 10)
    sim = simulate_execution(plan, [10.0] * 10, avg_daily_volume=1e7, volatility=0.03,
                             participation_per_slice=0.00001)
    assert sim["window_horizon_days"] == 252.0        # 反推 1000 天 >> 252 天 → 钳位
    assert any("钳到 252 天" in a for a in sim["assumptions"])


def test_sim_assumptions_document_price_vs_slippage_separation():
    sim = simulate_execution(_big_plan(10000, 10), [10.0] * 10,
                             avg_daily_volume=5e6, volatility=0.03)
    assert any("不必然同号" in a for a in sim["assumptions"])
    assert any("不做 max(0" in a for a in sim["assumptions"])


@pytest.mark.parametrize("kwargs", [
    {"avg_daily_volume": 0, "volatility": 0.03},
    {"avg_daily_volume": -1e6, "volatility": 0.03},
    {"avg_daily_volume": 1e7, "volatility": 0},
    {"avg_daily_volume": 1e7, "volatility": -0.02},
    {"avg_daily_volume": 1e7, "volatility": 0.03, "participation_per_slice": 0},
    {"avg_daily_volume": 1e7, "volatility": 0.03, "participation_per_slice": 1.5},
    {"avg_daily_volume": 1e7, "volatility": 0.03, "window_horizon_days": -1.0},
    {"avg_daily_volume": 1e7, "volatility": 0.03, "urgency_kappa": 0},
])
def test_sim_invalid_market_params_raise(kwargs):
    with pytest.raises(ValueError):
        simulate_execution(_big_plan(1000, 2), [10.0, 10.0], **kwargs)


@pytest.mark.parametrize("plan", [
    None, [1000, 2000], {"n_slices": 3}, {"slices": []}, {"slices": [0, 0]},
    {"slices": [1000, -5]}, {"slices": [1000.5]},
])
def test_sim_invalid_plan_raises(plan):
    with pytest.raises(ValueError):
        simulate_execution(plan, [10.0, 10.0], avg_daily_volume=1e7, volatility=0.03)


def test_sim_vwap_plan_end_to_end():
    """VWAP 计划 + 与量分布对齐的参考价 + 参与率上限, 全链路可跑且账目自洽."""
    profile = [0.0, 1.0, 2.0, 3.0, 4.0]
    plan = plan_execution(6000, algo="vwap", n_slices=5, volume_profile=profile,
                          adv=1e6, cap_pct=0.1)
    sim = simulate_execution(plan, [10.0, 10.1, 10.2, 9.9, 10.05],
                             avg_daily_volume=5e6, volatility=0.02)
    assert plan["slices"][0] == 0
    assert sum(plan["slices"]) == plan["total_scheduled"] == 6000
    assert plan["unscheduled"] == 0
    assert sim["per_slice"][0]["idle"] is True
    assert sim["saving_bps"] == pytest.approx(
        sim["slippage_bps_naive"] - sim["slippage_bps_sliced"], abs=0.011)


# ==========================================================================
# 7) CLI 与模块卫生
# ==========================================================================
def test_cli_selftest_prints_twap_and_vwap_tables(capsys):
    rc = _main(["--selftest"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "TWAP 拆单表" in out and "VWAP 拆单表" in out
    assert "saving_bps" in out
    assert "滑点对比" in out


def test_cli_single_algo_mode(capsys):
    assert _main(["--algo", "vwap", "--qty", "1050", "--slices", "7"]) == 0
    out = capsys.readouterr().out
    assert "VWAP 拆单表" in out
    assert "TWAP 拆单表" not in out
    assert "unscheduled)=50" in out


def test_cli_no_action_prints_usage(capsys):
    assert _main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_cli_bad_algo_exits_with_error():
    with pytest.raises(SystemExit):
        _main(["--algo", "momentum"])


def test_cli_does_not_touch_files_or_network(capsys, monkeypatch):
    """CLI 是纯 print: 任何 open()/urlopen 都应让本用例失败."""
    import builtins
    import urllib.request

    def _boom(*a, **k):
        raise AssertionError("CLI 不允许读写文件/访问网络")

    monkeypatch.setattr(builtins, "open", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert _main(["--selftest"]) == 0
    capsys.readouterr()


def test_module_has_coding_header_and_chinese_docstrings():
    """文件头编码声明 + 每个公开函数的 docstring 必须写清取舍(任务书要求)."""
    src = pathlib.Path(exec_algo.__file__).read_text(encoding="utf-8")
    assert src.startswith("# -*- coding: utf-8 -*-")
    for fn in (lot_split, schedule_twap, schedule_vwap, participation_cap_slices,
               plan_execution, naive_slippage_bps, simulate_execution, _main):
        doc = fn.__doc__ or ""
        assert len(doc) > 200, f"{fn.__name__} 的 docstring 太短, 未写清设计取舍"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in doc), f"{fn.__name__} 的 docstring 应为中文"
    cap_doc = participation_cap_slices.__doc__ or ""
    assert "占位" in cap_doc and "生产接线" in cap_doc
    twap_doc = schedule_twap.__doc__ or ""
    assert "前重后轻" in twap_doc and "零头" in twap_doc


def test_placeholder_constants_are_named_and_documented():
    assert PARTICIPATION_CAP_PCT_PLACEHOLDER == 0.10
    assert DEFAULT_N_SLICES_PLACEHOLDER == 10
    for const in (exec_algo.PARTICIPATION_CAP_PCT_PLACEHOLDER,
                  exec_algo.DEFAULT_N_SLICES_PLACEHOLDER,
                  exec_algo.URGENCY_KAPPA_PLACEHOLDER):
        assert isinstance(const, (int, float)) and not isinstance(const, bool)
        assert const > 0
    assert "占位" in (exec_algo.__doc__ or "") or "占位" in (
        exec_algo.PARTICIPATION_CAP_PCT_PLACEHOLDER.__doc__ or "")


def test_module_reuses_slippage_model_functions():
    """复核"复用而非另写": 模块里的成本函数就是 slippage_model 的那两个对象."""
    import slippage_model
    assert exec_algo.decompose_slippage is slippage_model.decompose_slippage
    assert exec_algo.almglen_chriss_slippage is slippage_model.almglen_chriss_slippage


def test_module_imports_only_whitelisted_modules():
    """AST 级检查: 只允许标准库 + numpy + slippage_model(不得 import paper_book/config)."""
    src = pathlib.Path(exec_algo.__file__).read_text(encoding="utf-8")
    mods: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert "slippage_model" in mods
    assert "paper_book" not in mods and "config" not in mods
    allowed = {"__future__", "argparse", "ast", "math", "pathlib", "subprocess", "sys",
               "typing", "numpy", "slippage_model"}
    assert mods <= allowed, f"出现了白名单外的 import: {sorted(mods - allowed)}"


def test_import_graph_does_not_pull_heavy_modules():
    """真·导入检查(子进程): import exec_algo 不得把 paper_book/config 拉进 sys.modules."""
    src_dir = str(pathlib.Path(exec_algo.__file__).parent)
    code = (
        "import sys; sys.path.insert(0, r'{}'); import exec_algo;"
        "print(int('paper_book' in sys.modules), int('config' in sys.modules),"
        " int('slippage_model' in sys.modules))".format(src_dir)
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    has_paper_book, has_config, has_slippage = proc.stdout.split()
    assert has_paper_book == "0", "import exec_algo 不应拉起 paper_book"
    assert has_config == "0", "import exec_algo 不应拉起 config"
    assert has_slippage == "1"
