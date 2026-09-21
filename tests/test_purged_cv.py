# -*- coding: utf-8 -*-
"""purged_cv 的单元测试 (purge / embargo / CPCV 路径数 / DSR / MinTRL / CSCV-PBO).

覆盖重点
--------
1. purge 的**精确索引集合**: 构造已知 t1 区间, 把手算结果写成常量断言 ——
   实现与手算不一致(多剔/少剔一个样本)会立刻失败。
2. embargo 的**边界**: 恰好 floor(pct * n) 个、不多剔一个、只在测试块之后生效。
3. CPCV: 组合数与回测路径条数 C(n,k)*k/n = C(n-1,k-1) 的整数公式, 以及
   "每个样本恰好出现在 n_paths 个组合的测试集里"这一可交叉验证的恒等式。
4. DSR: N=1 退化为 PSR, sr0 随 N 单调不减 / DSR 单调不增, sr<=0 时 DSR 很小,
   年化<->单期换算自洽, sr_variance 的口径(年化方差)。
5. MinTRL: 闭式解逐位对齐、随 target_sr 单调增、sr<=target 返回 inf。
6. PBO: 与仓库既有 pbo_cscv.cscv_pbo 数值一致(同一输入同一 PBO), 并有两个
   解析已知答案: "常数优势配置" -> PBO == 0; "完全反相关的一对配置" -> PBO == 1。
7. 数值稳定性: 样本不足 / 零方差 / 常量序列 / NaN 的行为都被显式断言
   (返回 NaN 或抛 ValueError, 不静默返回 0)。

跑法(repo 根目录): .venv314\\Scripts\\python.exe -m pytest tests/test_purged_cv.py -q
conftest.py 已把 src/ 加入 sys.path, 因此可直接 import purged_cv。
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import purged_cv as pc  # noqa: E402
import pbo_cscv  # noqa: E402
from purged_cv import (  # noqa: E402
    CombinatorialPurgedCV,
    PurgedKFold,
    deflated_sharpe_ratio,
    min_track_record_length,
    norm_cdf,
    norm_ppf,
    probabilistic_sharpe_ratio,
    probability_backtest_overfitting,
    sharpe_ratio,
)

LOG_HALF = math.log(0.5)


# ==========================================================================
# 0. 正态分布工具 (DSR/MinTRL 的地基, 单独验证)
# ==========================================================================
def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == 0.5
    assert norm_cdf(1.96) == pytest.approx(0.9750021048517795, abs=1e-12)
    assert norm_cdf(-1.96) == pytest.approx(0.024997895148220435, abs=1e-12)
    assert norm_cdf(1.0) == pytest.approx(0.8413447460685429, abs=1e-12)
    assert norm_cdf(math.inf) == 1.0
    assert norm_cdf(-math.inf) == 0.0
    assert math.isnan(norm_cdf(float("nan")))
    # 对称性: Phi(-x) = 1 - Phi(x)
    for x in (0.1, 0.7, 1.5, 3.0):
        assert norm_cdf(-x) == pytest.approx(1.0 - norm_cdf(x), abs=1e-15)


def test_norm_ppf_known_values():
    assert norm_ppf(0.5) == 0.0
    assert norm_ppf(0.975) == pytest.approx(1.959963984540054, abs=1e-9)
    assert norm_ppf(0.025) == pytest.approx(-1.959963984540054, abs=1e-9)
    assert norm_ppf(0.95) == pytest.approx(1.6448536269514722, abs=1e-9)
    assert norm_ppf(0.999) == pytest.approx(3.090232306167813, abs=1e-9)
    assert norm_ppf(1e-6) == pytest.approx(-4.753424308822899, abs=1e-9)


def test_norm_ppf_cdf_roundtrip():
    """Phi(Phi^-1(p)) == p: 分位数与 CDF 必须自洽(尾部用相对误差, 因为 1-erf 有抵消)."""
    for p in (0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99):
        assert norm_cdf(norm_ppf(p)) == pytest.approx(p, abs=1e-12)
    for p in (1e-6, 1e-4, 0.9999, 1.0 - 1e-9):
        assert norm_cdf(norm_ppf(p)) == pytest.approx(p, rel=1e-6)


def test_norm_ppf_symmetry():
    for p in (0.01, 0.2, 0.4):
        assert norm_ppf(p) == pytest.approx(-norm_ppf(1.0 - p), rel=1e-12)


def test_norm_ppf_boundaries_and_errors():
    assert norm_ppf(0.0) == -math.inf
    assert norm_ppf(1.0) == math.inf
    assert norm_ppf(-0.5) == -math.inf
    assert norm_ppf(1.5) == math.inf
    for bad in (float("nan"),):
        with pytest.raises(ValueError):
            norm_ppf(bad)


# ==========================================================================
# 1. sharpe_ratio
# ==========================================================================
def test_sharpe_ratio_annualization_factor():
    rng = np.random.RandomState(0)
    x = rng.normal(0.001, 0.01, size=200)
    raw = sharpe_ratio(x, annualize=False)
    ann = sharpe_ratio(x, annualize=True)
    assert ann == pytest.approx(raw * math.sqrt(252), rel=1e-12)
    assert sharpe_ratio(x, annualize=True, trading_days=100) == pytest.approx(raw * 10.0, rel=1e-12)


def test_sharpe_ratio_known_value():
    x = np.array([0.01, 0.02, 0.03, 0.04, 0.05])
    expect = float(np.mean(x) / np.std(x, ddof=1) * math.sqrt(252))
    assert sharpe_ratio(x) == pytest.approx(expect, rel=1e-15)


def test_sharpe_ratio_matches_pbo_cscv_sharpe():
    """本模块的 Sharpe 必须与仓库既有 pbo_cscv.sharpe 逐位一致(PBO 交叉验证的前提)."""
    rng = np.random.RandomState(3)
    for _ in range(5):
        x = rng.normal(0.0005, 0.01, size=120)
        assert sharpe_ratio(x) == pbo_cscv.sharpe(x)


def test_sharpe_ratio_nan_behaviors():
    assert math.isnan(sharpe_ratio(np.zeros(50)))              # 常量序列(零方差)
    assert math.isnan(sharpe_ratio(np.array([0.01, 0.02])))    # 有效观测 < 3
    assert math.isnan(sharpe_ratio(np.array([])))              # 空输入
    assert math.isnan(sharpe_ratio(np.array([np.nan] * 10)))   # 全 NaN
    assert math.isnan(sharpe_ratio(np.array([0.0, 0.0, 0.0, 0.0])))
    # NaN/inf 被丢弃而不是污染结果
    clean = np.array([0.01, 0.02, 0.03, 0.04, 0.05])
    dirty = np.concatenate([clean, [np.nan, np.inf, -np.inf]])
    assert sharpe_ratio(dirty) == pytest.approx(sharpe_ratio(clean), rel=1e-15)


def test_sharpe_ratio_zero_vol_returns_nan_not_zero():
    """回归防线: 常量收益必须给 NaN, 不能静默给 0(否则掩盖数据问题)."""
    sr = sharpe_ratio(np.full(100, 0.001))
    assert math.isnan(sr) and sr != 0.0


def test_sharpe_ratio_annualize_flag_false_is_per_period():
    """annualize=False 必须给单期 Sharpe, 不能偷偷乘 sqrt(252)."""
    x = np.array([0.01, -0.005, 0.02, 0.0, 0.015])
    raw = float(np.mean(x) / np.std(x, ddof=1))
    got = sharpe_ratio(x, annualize=False)
    assert got == pytest.approx(raw, rel=1e-15)
    assert got == pytest.approx(0.7715885154726595, rel=1e-9)      # 手算过的锚点
    assert sharpe_ratio(x) == pytest.approx(got * math.sqrt(252), rel=1e-12)


# ==========================================================================
# 2. PurgedKFold -- 基本接口
# ==========================================================================
def test_purged_kfold_get_n_splits_and_repr():
    cv = PurgedKFold(n_splits=4, embargo_pct=0.02)
    assert cv.get_n_splits() == 4
    assert cv.get_n_splits(X=np.zeros((10, 1))) == 4      # sklearn 约定: 参数被忽略
    assert "n_splits=4" in repr(cv)


def test_purged_kfold_test_folds_partition_all_samples():
    """测试折必须完整划分样本, 且折长与 sklearn KFold 口径一致(n=10,k=3 -> [4,3,3])."""
    n = 10
    cv = PurgedKFold(n_splits=3, embargo_pct=0.0)
    folds = list(cv.split(np.zeros((n, 1))))
    assert len(folds) == 3
    tests = [te.tolist() for _, te in folds]
    assert tests == [[0, 1, 2, 3], [4, 5, 6], [7, 8, 9]]
    union = np.concatenate([te for _, te in folds])
    assert sorted(union.tolist()) == list(range(n))
    assert all(a.dtype.kind == "i" for a in union)          # numpy 整数数组


def test_purged_kfold_matches_sklearn_kfold_fold_boundaries():
    """无 t1 + 无 embargo 时, 分折与 sklearn.model_selection.KFold 完全相同."""
    sklearn_model = pytest.importorskip("sklearn.model_selection")
    X = np.zeros((23, 2))
    got = [te.tolist() for _, te in PurgedKFold(n_splits=5, embargo_pct=0.0).split(X)]
    want = [te.tolist() for _, te in sklearn_model.KFold(n_splits=5).split(X)]
    assert got == want


def test_purged_kfold_indices_sorted_disjoint_and_not_shared():
    rng = np.random.RandomState(0)
    X = np.zeros((60, 3))
    t1 = np.arange(60) + rng.randint(0, 5, size=60)
    for train, test in PurgedKFold(n_splits=4, embargo_pct=0.02).split(X, t1=t1):
        assert list(train) == sorted(train.tolist())        # 升序
        assert list(test) == sorted(test.tolist())
        assert set(train.tolist()).isdisjoint(set(test.tolist()))
        assert train.dtype.kind == "i" and test.dtype.kind == "i"


def test_purged_kfold_does_not_mutate_inputs():
    X = np.zeros((30, 2))
    t1 = np.arange(30)
    X_copy, t1_copy = X.copy(), t1.copy()
    list(PurgedKFold(n_splits=3, embargo_pct=0.1).split(X, t1=t1))
    assert np.array_equal(X, X_copy) and np.array_equal(t1, t1_copy)


# ==========================================================================
# 3. PurgedKFold -- purge: 与手算索引集合精确比对
# ==========================================================================
def _purge_case():
    """n=10, k=5 (每折 2 个样本), 标签区间 t1 手写;

    第 2 折 test=[4,5], 对应区间 [4,7] 与 [5,8];
    手算: 训练集 [0,1,2,3,6,7,8,9] 中被剔的是 {2,3,6,7,8}(与测试区间有重叠),
    保留 [0,1,9]。端点相切算重叠: i=2 的 t1=4 正好等于 test 起点 4;
    i=8 的 t0=8 正好等于 test 最长终点 8。
    """
    t1 = np.array([1, 2, 4, 5, 7, 8, 10, 11, 13, 14])
    return np.zeros((10, 1)), t1


def test_purge_removes_exactly_the_overlapping_samples():
    X, t1 = _purge_case()
    folds = list(PurgedKFold(n_splits=5, embargo_pct=0.0).split(X, t1=t1))
    train, test = folds[2]
    assert test.tolist() == [4, 5]
    assert train.tolist() == [0, 1, 9]                       # 手算结果
    purged = sorted(set(range(10)) - set(test.tolist()) - set(train.tolist()))
    assert purged == [2, 3, 6, 7, 8]                         # 精确匹配手算被剔集合


def test_purge_boundary_touching_is_purged_and_one_past_is_kept():
    """闭区间判据: t1_train == t0_test 剔(i=2), t0_train == max t1_test 剔(i=8), 再远一个保留(i=9)."""
    X, t1 = _purge_case()
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.0).split(X, t1=t1))[2]
    assert 2 not in train.tolist()      # t1=4 与 test 起点 4 相切 -> 剔
    assert 8 not in train.tolist()      # t0=8 与 test 终点 8 相切 -> 剔
    assert 9 in train.tolist()          # t0=9 > 8 -> 保留
    assert 1 in train.tolist()          # t1=2 < 4 -> 保留


def test_purge_only_touches_training_set_not_test_set():
    X, t1 = _purge_case()
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.0).split(X, t1=t1))[2]
    assert test.tolist() == [4, 5]                           # purge 不改变测试集


def test_purge_with_t1_equal_to_position_removes_nothing():
    """t1 == 自身位置(标签瞬时确定)时, 训练样本区间只覆盖自己 -> 一个都不该剔."""
    n = 40
    X = np.zeros((n, 1))
    for train, test in PurgedKFold(n_splits=4, embargo_pct=0.0).split(X, t1=np.arange(n)):
        assert set(train.tolist()) == set(range(n)) - set(test.tolist())


def test_purge_with_long_labels_removes_forward_samples():
    """标签很长(t1 = i + 20)时, 测试块**之前**的样本也会被剔: 它们的标签结束时间
    落在测试期内, 是最典型的向前泄露。

    n=60, k=6 -> 第 2 折 test=[20..29], 测试区间为 [20,40]..[29,49];
    手算: 训练样本 i 的区间 [i, i+20] 与测试区间重叠 <=> i <= 49 且 i+20 >= 20
    (后一条对 i >= 0 恒成立), 故 0..19 与 30..49 全部被剔, 只剩 [50..59]。
    """
    n = 60
    X = np.zeros((n, 1))
    t1 = np.arange(n) + 20
    train, test = list(PurgedKFold(n_splits=6, embargo_pct=0.0).split(X, t1=t1))[2]
    assert test.tolist() == list(range(20, 30))
    assert train.tolist() == list(range(50, 60))
    purged = sorted(set(range(n)) - set(test.tolist()) - set(train.tolist()))
    assert purged == list(range(0, 20)) + list(range(30, 50))


def test_no_label_interval_degrades_to_embargo_only():
    """没有 t1 信息时不 purge, 只做 embargo —— 显式退化, 不假装做了 purge."""
    n = 100
    X = np.zeros((n, 1))
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.05).split(X))[1]
    assert test.tolist() == list(range(20, 40))
    expect = list(range(0, 20)) + list(range(45, 100))       # [40,44] 被 embargo
    assert train.tolist() == expect


def test_no_label_interval_zero_embargo_equals_plain_kfold():
    n = 21
    X = np.zeros((n, 1))
    for train, test in PurgedKFold(n_splits=3, embargo_pct=0.0).split(X):
        assert set(train.tolist()) == set(range(n)) - set(test.tolist())


def test_t1_taken_from_y_dataframe_column():
    """y 是带 't1' 列的 DataFrame 时, 直接用它做标签区间(无需显式传参)."""
    import pandas as pd

    X, t1 = _purge_case()
    y = pd.DataFrame({"t1": t1})
    train, _ = list(PurgedKFold(n_splits=5, embargo_pct=0.0).split(X, y=y))[2]
    assert train.tolist() == [0, 1, 9]


def test_explicit_t1_argument_overrides_constructor_and_y():
    import pandas as pd

    X, t1 = _purge_case()
    y = pd.DataFrame({"t1": t1})
    cv = PurgedKFold(n_splits=5, embargo_pct=0.0, t1=t1)
    # 显式 t1 = 位置 -> 无 purge, 覆盖构造参数与 y['t1']
    train, _ = list(cv.split(X, y=y, t1=np.arange(10)))[2]
    assert train.tolist() == [0, 1, 2, 3, 6, 7, 8, 9]


def test_constructor_t1_is_used_when_split_has_none():
    X, t1 = _purge_case()
    train, _ = list(PurgedKFold(n_splits=5, embargo_pct=0.0, t1=t1).split(X))[2]
    assert train.tolist() == [0, 1, 9]


def test_t1_as_pandas_series_with_positional_scale():
    import pandas as pd

    X, t1 = _purge_case()
    train, _ = list(PurgedKFold(n_splits=5, embargo_pct=0.0).split(X, t1=pd.Series(t1)))[2]
    assert train.tolist() == [0, 1, 9]


def test_purge_with_datetime_index_uses_real_timestamps():
    """时间口径: t0 = DatetimeIndex, t1 = 观测日 + 2 天; 手算被剔集合 {2,3,6,7}."""
    import pandas as pd

    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    X = pd.DataFrame({"x": np.zeros(10)}, index=idx)
    t1 = pd.Series(idx + pd.Timedelta(days=2), index=idx)
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.0).split(X, t1=t1))[2]
    assert test.tolist() == [4, 5]
    assert train.tolist() == [0, 1, 8, 9]


# ==========================================================================
# 4. PurgedKFold -- embargo: 精确个数与边界
# ==========================================================================
def test_embargo_removes_exactly_floor_count():
    n = 100
    X = np.zeros((n, 1))
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.05).split(X))[1]
    removed = sorted(set(range(n)) - set(test.tolist()) - set(train.tolist()))
    assert removed == [40, 41, 42, 43, 44]                   # 恰好 5 个 = 0.05 * 100
    assert len(removed) == 5


def test_embargo_does_not_remove_one_sample_too_many():
    n = 100
    X = np.zeros((n, 1))
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.05).split(X))[1]
    assert 45 in train.tolist()          # 第 6 个样本必须留在训练集
    assert len(train) == 100 - 20 - 5    # 只少了测试块 + 恰好 5 个 embargo


def test_embargo_never_touches_samples_before_the_test_block():
    n = 100
    X = np.zeros((n, 1))
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.05).split(X))[1]
    assert all(i in train.tolist() for i in range(0, 20))


def test_embargo_at_last_fold_removes_nothing():
    """测试块已在样本末尾时无样本可 embargo —— 这是定义使然, 不是漏做."""
    n = 100
    X = np.zeros((n, 1))
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.05).split(X))[4]
    assert test.tolist() == list(range(80, 100))
    assert len(train) == 80


def test_embargo_floor_absorbs_float_noise():
    """0.29 * 100 在浮点里是 28.999...996; embargo 必须按十进制意图取 29 而不是 28."""
    assert float(0.29 * 100) < 29.0 and int(0.29 * 100) == 28         # 记录这个坑
    assert pc._embargo_count(100, 0.29) == 29
    X = np.zeros((100, 1))
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.29).split(X))[0]
    assert test.tolist() == list(range(0, 20))
    assert len(train) == 100 - 20 - 29
    assert 48 not in train.tolist() and 49 in train.tolist()


def test_embargo_zero_removes_nothing():
    n = 50
    X = np.zeros((n, 1))
    for train, test in PurgedKFold(n_splits=5, embargo_pct=0.0).split(X):
        assert set(train.tolist()) == set(range(n)) - set(test.tolist())


def test_embargo_can_leave_train_empty_without_raising():
    """极端 embargo 下训练集为空是数据事实: 返回空数组, 不抛错也不静默改口径."""
    train, test = list(PurgedKFold(n_splits=2, embargo_pct=0.99).split(np.zeros((100, 1))))[0]
    assert test.tolist() == list(range(0, 50))
    assert train.tolist() == []
    train2, _ = list(PurgedKFold(n_splits=2, embargo_pct=0.99).split(np.zeros((100, 1))))[1]
    assert len(train2) == 50


# ==========================================================================
# 5. PurgedKFold -- 参数校验
# ==========================================================================
@pytest.mark.parametrize("bad", [0, 1, -3, 2.5, "3", None])
def test_purged_kfold_invalid_n_splits_raises(bad):
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=bad)


@pytest.mark.parametrize("bad", [-0.1, 1.0, 1.5, float("nan"), float("inf")])
def test_purged_kfold_invalid_embargo_raises(bad):
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=3, embargo_pct=bad)


def test_purged_kfold_n_splits_greater_than_samples_raises():
    with pytest.raises(ValueError):
        list(PurgedKFold(n_splits=20, embargo_pct=0.0).split(np.zeros((10, 1))))


def test_purged_kfold_empty_or_bad_X_raises():
    with pytest.raises(ValueError):
        list(PurgedKFold(n_splits=2).split(np.zeros((0, 1))))
    with pytest.raises(ValueError):
        list(PurgedKFold(n_splits=2).split(12345))


def test_purged_kfold_t1_length_mismatch_raises():
    with pytest.raises(ValueError, match="长度"):
        list(PurgedKFold(n_splits=3).split(np.zeros((10, 1)), t1=np.arange(3)))


def test_purged_kfold_nan_t1_raises():
    t1 = np.arange(10, dtype=float)
    t1[7] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        list(PurgedKFold(n_splits=3).split(np.zeros((10, 1)), t1=t1))


def test_purged_kfold_mismatched_time_scale_raises():
    """X 无 index -> t0 是位置; t1 是时间戳 -> 尺度不匹配必须显式报错."""
    import pandas as pd

    t1 = pd.date_range("2024-01-01", periods=10, freq="D")
    with pytest.raises(ValueError, match="尺度"):
        list(PurgedKFold(n_splits=5, embargo_pct=0.0).split(np.zeros((10, 1)), t1=t1))


def test_purged_kfold_row_order_is_positional_for_list_input():
    """X 是 list 时没有 index, t0 退化为位置口径, t1 也用位置 -> 工作正常."""
    X = [[0.0]] * 10
    train, test = list(PurgedKFold(n_splits=5, embargo_pct=0.0).split(X, t1=np.arange(10)))[2]
    assert test.tolist() == [4, 5] and train.tolist() == [0, 1, 2, 3, 6, 7, 8, 9]


# ==========================================================================
# 6. CombinatorialPurgedCV -- 路径数与组合数公式
# ==========================================================================
@pytest.mark.parametrize("n,k,want", [(6, 2, 5), (8, 2, 7), (6, 1, 1), (10, 3, 36), (4, 2, 3), (12, 4, 165)])
def test_backtest_paths_hand_computed(n, k, want):
    """路径条数 = C(n,k) * k / n, 手工算好的常数."""
    assert CombinatorialPurgedCV.backtest_paths(n, k) == want


@pytest.mark.parametrize("n,k", [(5, 2), (6, 3), (7, 1), (9, 4), (11, 5), (20, 7)])
def test_backtest_paths_equals_comb_n_minus_1(n, k):
    """恒等式: C(n,k)*k/n == C(n-1,k-1)(整数)."""
    assert CombinatorialPurgedCV.backtest_paths(n, k) == math.comb(n - 1, k - 1)


def test_backtest_paths_returns_int_not_float():
    got = CombinatorialPurgedCV.backtest_paths(10, 3)
    assert isinstance(got, int) and got == 36 and got != 36.000000000000004


def test_n_paths_property_matches_static_and_get_n_splits_is_combinations():
    cv = CombinatorialPurgedCV(n_splits=6, n_test_splits=2)
    assert cv.n_paths == 5
    assert cv.n_paths == CombinatorialPurgedCV.backtest_paths(6, 2)
    assert cv.get_n_splits() == math.comb(6, 2) == 15        # 组合数, 不是路径数
    assert "n_paths=5" in repr(cv)


def test_each_sample_is_test_in_exactly_n_paths_combinations():
    """路径数公式的可交叉验证含义: 每个样本恰好出现在 n_paths 个组合的测试集里."""
    n, n_splits, k = 12, 6, 2
    cv = CombinatorialPurgedCV(n_splits=n_splits, n_test_splits=k, embargo_pct=0.0)
    counts = np.zeros(n, dtype=int)
    for _, test in cv.split(np.zeros((n, 1))):
        counts[test] += 1
    assert counts.tolist() == [cv.n_paths] * n
    assert cv.n_paths == math.comb(n_splits - 1, k - 1)


def test_combinations_order_matches_split_block_sets():
    n, n_splits, k = 12, 6, 2
    cv = CombinatorialPurgedCV(n_splits=n_splits, n_test_splits=k, embargo_pct=0.0)
    groups = cv.assign_groups(n_samples=n)
    combos = cv.combinations()
    assert combos[0] == (0, 1) and combos[-1] == (n_splits - k, n_splits - 1)
    for combo, (_, test) in zip(combos, cv.split(np.zeros((n, 1)))):
        assert sorted(set(groups[test].tolist())) == list(combo)


# ==========================================================================
# 7. CombinatorialPurgedCV -- purge / embargo / 参数
# ==========================================================================
def _cpcv_case():
    """n=12, 6 块(每块 2), k=2; 手写 t1 区间用于手算 purge."""
    t1 = np.array([1, 2, 4, 5, 7, 8, 10, 11, 13, 14, 16, 17])
    return np.zeros((12, 1)), t1


def test_cpcv_purge_removes_exactly_the_overlapping_samples():
    """组合 (0,1) -> test=[0,1,2,3] (区间 [0,1],[1,2],[2,4],[3,5]): 手算保留 [6..11]."""
    X, t1 = _cpcv_case()
    cv = CombinatorialPurgedCV(n_splits=6, n_test_splits=2, embargo_pct=0.0)
    train, test = list(cv.split(X, t1=t1))[0]
    assert test.tolist() == [0, 1, 2, 3]
    assert train.tolist() == [6, 7, 8, 9, 10, 11]
    purged = sorted(set(range(12)) - set(test.tolist()) - set(train.tolist()))
    assert purged == [4, 5]


def test_cpcv_keeps_samples_inside_test_span_that_do_not_overlap():
    """组合 (0,5) -> test=[0,1,10,11]: 中间样本 3,4,5 不重叠任何测试区间, 必须保留.

    这是"逐对判据"而非"测试集跨度"的判定依据: 用跨度 [0, 17] 会把全部训练样本
    误剔成空集, 那是过度 purge, 会让样本量凭空蒸发。
    """
    X, t1 = _cpcv_case()
    cv = CombinatorialPurgedCV(n_splits=6, n_test_splits=2, embargo_pct=0.0)
    train, test = list(cv.split(X, t1=t1))[4]                # 第 5 个组合 = (0,5)
    assert test.tolist() == [0, 1, 10, 11]
    assert train.tolist() == [3, 4, 5]
    assert len(train) > 0                                    # 跨度法会得到空训练集


def test_cpcv_test_sets_are_unions_of_contiguous_blocks():
    n, n_splits, k = 24, 6, 2
    cv = CombinatorialPurgedCV(n_splits=n_splits, n_test_splits=k, embargo_pct=0.0)
    groups = cv.assign_groups(n_samples=n)
    for _, test in cv.split(np.zeros((n, 1))):
        blocks = sorted(set(groups[test].tolist()))
        assert len(blocks) == k                              # 恰好 k 个块
        for b in blocks:
            idx = groups[test]
            assert test[idx == b].tolist() == sorted(test[idx == b].tolist())
            assert len(test[idx == b]) in (n // n_splits, n // n_splits + 1)


def test_cpcv_embargo_exact_count_and_position():
    """n=100, 10 块, k=2, embargo 2% -> 组合 (0,1) 后 2 个样本被剔."""
    n = 100
    cv = CombinatorialPurgedCV(n_splits=10, n_test_splits=2, embargo_pct=0.02)
    train, test = list(cv.split(np.zeros((n, 1))))[0]
    assert test.tolist() == list(range(0, 20))
    removed = sorted(set(range(n)) - set(test.tolist()) - set(train.tolist()))
    assert removed == [20, 21]
    assert 22 in train.tolist()


def test_cpcv_no_t1_degrades_to_embargo_only():
    n = 100
    cv = CombinatorialPurgedCV(n_splits=10, n_test_splits=2, embargo_pct=0.02)
    train, test = list(cv.split(np.zeros((n, 1))))[0]
    assert train.tolist() == list(range(22, 100))


def test_cpcv_train_and_test_disjoint_in_every_combination():
    rng = np.random.RandomState(1)
    n = 60
    t1 = np.arange(n) + rng.randint(0, 4, size=n)
    cv = CombinatorialPurgedCV(n_splits=6, n_test_splits=3, embargo_pct=0.05)
    seen = 0
    for train, test in cv.split(np.zeros((n, 1)), t1=t1):
        seen += 1
        assert set(train.tolist()).isdisjoint(set(test.tolist()))
        assert list(train) == sorted(train.tolist())
        assert train.dtype.kind == "i" and test.dtype.kind == "i"
    assert seen == math.comb(6, 3)


def test_cpcv_assign_groups_blocks_by_position():
    """分组 = np.array_split 的块编号: n 除不尽时**前 n % k 块**多一个样本.

    n=10, k=6 -> 块长 [2,2,2,2,1,1] -> 分组 [0,0,1,1,2,2,3,3,4,5]
    (注意不是 floor(i*k/n) = [0,0,1,1,2,3,3,4,4,5], 那样会与 split() 的块边界错位)
    """
    assert CombinatorialPurgedCV(n_splits=6, n_test_splits=2).assign_groups(12).tolist() == \
        [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert CombinatorialPurgedCV(n_splits=6, n_test_splits=2).assign_groups(n_samples=10).tolist() == \
        [0, 0, 1, 1, 2, 2, 3, 3, 4, 5]


def test_cpcv_assign_groups_is_consistent_with_split_blocks_when_n_not_divisible():
    """回归防线: n 不能被 n_splits 整除时, assign_groups 的块边界必须与 split() 一致."""
    n, n_splits, k = 10, 6, 2
    cv = CombinatorialPurgedCV(n_splits=n_splits, n_test_splits=k, embargo_pct=0.0)
    groups = cv.assign_groups(n_samples=n)
    for combo, (_, test) in zip(cv.combinations(), cv.split(np.zeros((n, 1)))):
        assert sorted(set(groups[test].tolist())) == list(combo)


def test_cpcv_assign_groups_accepts_X_and_reuses_last_split_size():
    cv = CombinatorialPurgedCV(n_splits=4, n_test_splits=2, embargo_pct=0.0)
    assert cv.assign_groups(X=np.zeros((8, 1))).tolist() == [0, 0, 1, 1, 2, 2, 3, 3]
    with pytest.raises(ValueError):
        cv.assign_groups()                                   # 还没 split 过, 无从推断
    list(cv.split(np.zeros((8, 1))))
    assert cv.assign_groups().tolist() == [0, 0, 1, 1, 2, 2, 3, 3]


@pytest.mark.parametrize("bad", [0, 1, -2, 2.5])
def test_cpcv_invalid_n_splits_raises(bad):
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_splits=bad, n_test_splits=1)


@pytest.mark.parametrize("bad", [0, -1, 6, 7, 2.5])
def test_cpcv_invalid_n_test_splits_raises(bad):
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_splits=6, n_test_splits=bad)


@pytest.mark.parametrize("bad", [-0.01, 1.0, float("nan")])
def test_cpcv_invalid_embargo_raises(bad):
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_splits=6, n_test_splits=2, embargo_pct=bad)


def test_cpcv_backtest_paths_invalid_args_raise():
    for args in ((1, 1), (0, 0), (6, 0), (6, 7), (6, -1), (6, 2.5)):
        with pytest.raises(ValueError):
            CombinatorialPurgedCV.backtest_paths(*args)


def test_cpcv_n_splits_greater_than_samples_raises():
    with pytest.raises(ValueError):
        list(CombinatorialPurgedCV(n_splits=12, n_test_splits=2).split(np.zeros((6, 1))))


# ==========================================================================
# 8. PSR / DSR
# ==========================================================================
def test_dsr_n_trials_one_equals_psr():
    """N=1 无选择偏差: sr0=0, DSR 必须退化为 PSR(0)."""
    out = deflated_sharpe_ratio(1.0, n_trials=1, n_obs=500)
    assert out["sr0"] == 0.0
    assert out["e_max_sr"] == 0.0
    assert out["dsr"] == out["psr"]
    assert out["dsr"] == pytest.approx(probabilistic_sharpe_ratio(1.0, 500), rel=1e-15)


def test_psr_is_half_when_sr_equals_benchmark():
    assert probabilistic_sharpe_ratio(0.0, 500) == 0.5
    assert probabilistic_sharpe_ratio(1.3, 500, benchmark=1.3) == 0.5


def test_psr_matches_hand_computed_formula():
    sr, n_obs, skew, kurt = 1.2, 300, -0.4, 4.5
    sr_pp = sr / math.sqrt(252)
    var = (1.0 + 0.5 * sr_pp ** 2 - skew * sr_pp + (kurt - 3.0) / 4.0 * sr_pp ** 2) / (n_obs - 1)
    z = sr_pp / math.sqrt(var)
    assert probabilistic_sharpe_ratio(sr, n_obs, skew=skew, kurtosis=kurt) == \
        pytest.approx(norm_cdf(z), rel=1e-12)


def test_dsr_keys_present_and_scales_flagged():
    out = deflated_sharpe_ratio(1.0, 5, 250)
    for key in ("dsr", "sr0", "psr", "z", "e_max_sr", "n_trials", "n_obs"):
        assert key in out
    assert out["n_trials"] == 5 and out["n_obs"] == 250
    assert "scale" in out and "sr_pp" in out and "sigma_sr" in out


def test_dsr_sr0_monotone_nondecreasing_in_n_trials():
    sr0s = [deflated_sharpe_ratio(1.0, N, 500)["sr0"] for N in (1, 2, 3, 5, 10, 50, 100, 1000, 10000)]
    assert all(b >= a - 1e-15 for a, b in zip(sr0s, sr0s[1:])), sr0s
    assert sr0s[0] == 0.0 and sr0s[-1] > sr0s[1]


def test_dsr_monotone_nonincreasing_in_n_trials():
    dsrs = [deflated_sharpe_ratio(1.0, N, 500)["dsr"] for N in (1, 2, 3, 5, 10, 50, 100, 1000, 10000)]
    assert all(b <= a + 1e-15 for a, b in zip(dsrs, dsrs[1:])), dsrs
    assert dsrs[0] > 0.9 and dsrs[-1] < 0.05


def test_dsr_small_for_nonpositive_sr():
    """sr <= 0 时 DSR 必须很小(接近 0), 且随 sr 下降继续变小."""
    d0 = deflated_sharpe_ratio(0.0, 100, 1000)["dsr"]
    dm1 = deflated_sharpe_ratio(-1.0, 100, 1000)["dsr"]
    assert d0 < 0.01 and dm1 < 1e-4 and dm1 < d0
    assert deflated_sharpe_ratio(-3.0, 100, 1000)["dsr"] < dm1


def test_dsr_increases_with_sr():
    dsrs = [deflated_sharpe_ratio(sr, 20, 400)["dsr"] for sr in (0.0, 0.5, 1.0, 2.0, 3.0)]
    assert all(b > a for a, b in zip(dsrs, dsrs[1:])), dsrs


def test_dsr_annualization_consistency():
    """年化口径与单期口径必须自洽: DSR(sr_ann, td=252) == DSR(sr_ann/sqrt(252), td=1)."""
    a = deflated_sharpe_ratio(1.0, 10, 500)
    b = deflated_sharpe_ratio(1.0 / math.sqrt(252), 10, 500, trading_days=1.0)
    assert a["dsr"] == pytest.approx(b["dsr"], rel=1e-12)
    assert a["sr0"] == pytest.approx(b["sr0"], rel=1e-12)
    assert a["e_max_sr"] == pytest.approx(b["e_max_sr"] * math.sqrt(252), rel=1e-12)


def test_dsr_sr_variance_zero_means_no_deflation():
    out = deflated_sharpe_ratio(1.0, 50, 500, sr_variance=0.0)
    assert out["sr0"] == 0.0 and out["e_max_sr"] == 0.0
    assert out["dsr"] == pytest.approx(probabilistic_sharpe_ratio(1.0, 500), rel=1e-15)


def test_dsr_sr_variance_widens_benchmark_and_lowers_dsr():
    base = deflated_sharpe_ratio(1.0, 50, 500)
    wide = deflated_sharpe_ratio(1.0, 50, 500, sr_variance=4.0 * base["var_sr_trials"] * 252)
    assert wide["e_max_sr"] > base["e_max_sr"]
    assert wide["dsr"] < base["dsr"]


def test_dsr_sr_variance_is_annualized_scale():
    """传"默认方差的年化值"必须复现默认 sr0, 证明内部做了 / trading_days 换算."""
    base = deflated_sharpe_ratio(1.0, 30, 400)
    again = deflated_sharpe_ratio(1.0, 30, 400, sr_variance=base["var_sr_trials"] * 252)
    assert again["sr0"] == pytest.approx(base["sr0"], rel=1e-12)
    assert again["dsr"] == pytest.approx(base["dsr"], rel=1e-12)


def test_dsr_skew_and_kurtosis_change_the_answer_in_expected_direction():
    """负偏 + 厚尾会放大 Sharpe 的标准误 -> 同样的 sr 应给出更低的 DSR."""
    normal = deflated_sharpe_ratio(1.5, 20, 400)["dsr"]
    skewed = deflated_sharpe_ratio(1.5, 20, 400, skew=-1.5, kurtosis=8.0)["dsr"]
    assert skewed < normal


@pytest.mark.parametrize("kwargs", [
    dict(sr=1.0, n_trials=0, n_obs=100),
    dict(sr=1.0, n_trials=-2, n_obs=100),
    dict(sr=1.0, n_trials=1.5, n_obs=100),
    dict(sr=1.0, n_trials=10, n_obs=1),
    dict(sr=1.0, n_trials=10, n_obs=0),
    dict(sr=float("nan"), n_trials=10, n_obs=100),
    dict(sr=float("inf"), n_trials=10, n_obs=100),
    dict(sr=1.0, n_trials=10, n_obs=100, sr_variance=-0.1),
    dict(sr=1.0, n_trials=10, n_obs=100, sr_variance=float("nan")),
    dict(sr=1.0, n_trials=10, n_obs=100, skew=float("nan")),
    dict(sr=1.0, n_trials=10, n_obs=100, kurtosis=float("inf")),
    dict(sr=1.0, n_trials=10, n_obs=100, trading_days=0),
    dict(sr=1.0, n_trials=10, n_obs=100, trading_days=-252),
])
def test_dsr_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        deflated_sharpe_ratio(**kwargs)


def test_dsr_pathological_moments_raise_instead_of_faking_zero():
    """极端 skew 会让方差近似式变负 —— 必须报错, 不能返回一个假的显著性."""
    with pytest.raises(ValueError):
        deflated_sharpe_ratio(30.0, 10, 50, skew=500.0)


# ==========================================================================
# 9. MinTRL
# ==========================================================================
def test_mintrl_matches_closed_form():
    sr, target, prob = 2.0, 0.5, 0.95
    sr_pp, tgt_pp = sr / math.sqrt(252), target / math.sqrt(252)
    sigma_sq = 1.0 + 0.5 * sr_pp ** 2 - 0.0 * sr_pp + (3.0 - 3.0) / 4.0 * sr_pp ** 2
    expect = 1.0 + sigma_sq * (norm_ppf(prob) / (sr_pp - tgt_pp)) ** 2
    assert min_track_record_length(sr, target, prob=prob) == pytest.approx(expect, rel=1e-12)
    # 与 1 + (1 - skew*SR + (kurt-1)/4*SR^2)*(Z/(SR-SR*))^2 的另一种写法一致
    alt = 1.0 + (1.0 - 0.0 * sr_pp + (3.0 - 1.0) / 4.0 * sr_pp ** 2) * (norm_ppf(prob) / (sr_pp - tgt_pp)) ** 2
    assert min_track_record_length(sr, target, prob=prob) == pytest.approx(alt, rel=1e-12)


def test_mintrl_known_anchor_value():
    """年化 SR=2、目标 0 时约 173 个交易日(约 8 个月)—— 与 Bailey 论文量级一致."""
    v = min_track_record_length(2.0, 0.0)
    assert 150.0 < v < 200.0


def test_mintrl_increases_with_target_sr():
    vals = [min_track_record_length(2.0, t) for t in (0.0, 0.5, 1.0, 1.5, 1.9)]
    assert all(b > a for a, b in zip(vals, vals[1:])), vals


def test_mintrl_infinite_when_sr_not_above_target():
    assert min_track_record_length(1.0, 1.0) == math.inf
    assert min_track_record_length(0.5, 1.0) == math.inf
    assert min_track_record_length(0.0, 0.0) == math.inf


def test_mintrl_decreases_with_higher_sr():
    vals = [min_track_record_length(sr, 0.0) for sr in (1.0, 1.5, 2.0, 3.0)]
    assert all(b < a for a, b in zip(vals, vals[1:])), vals


def test_mintrl_higher_confidence_needs_more_observations():
    assert min_track_record_length(2.0, 0.0, prob=0.99) > min_track_record_length(2.0, 0.0, prob=0.95)


def test_mintrl_fat_tails_and_negative_skew_need_more_observations():
    base = min_track_record_length(2.0, 0.0)
    assert min_track_record_length(2.0, 0.0, kurtosis=8.0) > base
    assert min_track_record_length(2.0, 0.0, skew=-1.0) > base


def test_mintrl_annualization_consistency():
    a = min_track_record_length(2.0, 1.0)
    b = min_track_record_length(2.0 / math.sqrt(252), 1.0 / math.sqrt(252), trading_days=1.0)
    assert a == pytest.approx(b, rel=1e-12)


@pytest.mark.parametrize("bad", [0.5, 0.4, 1.0, 0.0, -0.1, float("nan")])
def test_mintrl_invalid_prob_raises(bad):
    with pytest.raises(ValueError):
        min_track_record_length(2.0, 0.0, prob=bad)


@pytest.mark.parametrize("kwargs", [
    dict(sr=float("nan")),
    dict(sr=1.0, target_sr=float("inf")),
    dict(sr=1.0, skew=float("nan")),
    dict(sr=1.0, kurtosis=float("nan")),
    dict(sr=1.0, trading_days=0),
])
def test_mintrl_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        min_track_record_length(**kwargs)


# ==========================================================================
# 10. PBO: 与仓库既有 CSCV 实现的一致性
# ==========================================================================
def _pbo_matrix(seed=12345, T=120, N=6):
    return np.random.RandomState(seed).normal(0.0, 0.01, size=(T, N))


def test_pbo_matches_repo_cscv_exactly():
    """硬性要求: 同一输入上, 本模块与 pbo_cscv.cscv_pbo 必须给出同一个 PBO."""
    r = _pbo_matrix()
    blocks = np.array_split(r, 8, axis=0)
    mine = probability_backtest_overfitting(r, n_splits=8)
    ref = pbo_cscv.cscv_pbo(blocks)
    assert mine["pbo"] == ref["pbo"]
    assert mine["lambda_mean"] == pytest.approx(ref["lambda_mean"], rel=1e-12)
    assert mine["n_combos"] == ref["n_combos"] == math.comb(8, 4)
    assert mine["source"] == "pbo_cscv"


def test_pbo_local_fallback_matches_repo_cscv():
    """兜底实现(import 不到 pbo_cscv 时走的那条)也必须与仓库实现数值一致."""
    r = _pbo_matrix(seed=999, T=240, N=5)
    blocks = np.array_split(r, 8, axis=0)
    ref = pbo_cscv.cscv_pbo(blocks)
    local = pc._cscv_pbo_local(blocks)
    assert local["pbo"] == ref["pbo"]
    assert local["lambda_mean"] == pytest.approx(ref["lambda_mean"], rel=1e-12)
    assert local["lambda_std"] == pytest.approx(ref["lambda_std"], rel=1e-12)
    assert local["prob_oos_loss"] == ref["prob_oos_loss"]


def test_pbo_falls_back_when_pbo_cscv_is_unimportable(monkeypatch):
    """把 pbo_cscv 从 sys.modules 里屏蔽掉, 走 try/except 兜底路径, 结果必须一模一样."""
    r = _pbo_matrix(seed=31337, T=160, N=4)
    ref = probability_backtest_overfitting(r, 8)
    monkeypatch.setitem(sys.modules, "pbo_cscv", None)        # import pbo_cscv -> ImportError
    fallback = probability_backtest_overfitting(r, 8)
    assert fallback["source"] == "local"
    assert fallback["pbo"] == ref["pbo"] == pbo_cscv.cscv_pbo(np.array_split(r, 8, axis=0))["pbo"]


def test_module_import_contract_is_stdlib_plus_numpy_only():
    """硬约束的机器可验证版本: 模块级只 import 标准库 + numpy, 不碰 scipy/pandas/项目模块."""
    import ast

    src = open(pc.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    names = set()
    for node in tree.body:                                     # 只看模块级 import
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= {"__future__", "itertools", "math", "typing", "numpy"}, names
    assert "scipy" not in names and "pandas" not in names
    # 唯一的项目内引用必须写在函数体里并被 try/except 包住
    assert "import pbo_cscv" in src and "except Exception" in src
    body_imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all("pbo_cscv" not in ast.dump(n) for n in body_imports)


def test_pbo_explicit_repo_metric_matches_default():
    """显式传入 pbo_cscv.sharpe 作 metric, 结果必须与默认(年化 Sharpe)相同."""
    r = _pbo_matrix(seed=7)
    a = probability_backtest_overfitting(r, n_splits=8)
    b = probability_backtest_overfitting(r, n_splits=8, metric=pbo_cscv.sharpe)
    c = probability_backtest_overfitting(r, n_splits=8, metric=lambda col: sharpe_ratio(col))
    assert a["pbo"] == b["pbo"] == c["pbo"]


def test_pbo_dataframe_input_matches_ndarray():
    import pandas as pd

    r = _pbo_matrix(seed=11, T=160, N=4)
    df = pd.DataFrame(r, columns=[f"cfg{i}" for i in range(r.shape[1])])
    assert probability_backtest_overfitting(df, 8)["pbo"] == \
        probability_backtest_overfitting(r, 8)["pbo"]


def test_pbo_returns_documented_keys():
    res = probability_backtest_overfitting(_pbo_matrix(), 8)
    for key in ("pbo", "n_blocks", "n_configs", "n_combos", "lambda_mean", "lambda_std",
                "prob_oos_loss", "is_oos_slope", "n_splits", "n_obs", "metric", "source"):
        assert key in res
    assert res["n_splits"] == 8 and res["n_blocks"] == 8 and res["n_configs"] == 6
    assert res["n_obs"] == 120 and res["source"] in ("pbo_cscv", "local")
    assert 0.0 <= res["pbo"] <= 1.0


# ==========================================================================
# 11. PBO: 确定性已知答案
# ==========================================================================
def test_pbo_zero_when_one_config_dominates_everywhere():
    """解析答案: 常数优势配置必然 IS 最优且 OOS 排名第一 -> PBO == 0, lambda == log(N)."""
    rng = np.random.RandomState(7)
    T, N = 240, 3
    r = rng.normal(0.0, 0.02, size=(T, N))
    r[:, 0] += 0.01                                       # 每个块上都有真实优势
    res = probability_backtest_overfitting(r, n_splits=8)
    assert res["pbo"] == 0.0
    assert res["lambda_mean"] == pytest.approx(math.log(N), rel=1e-12)
    assert res["prob_oos_loss"] == 0.0


def test_pbo_one_when_two_configs_are_perfectly_anti_correlated():
    """解析答案: B == -A 且整段样本均值为 0 时, IS 赢家在 OOS 必然垫底 -> PBO == 1.

    推导: IS 与 OOS 天数相等且 sum(A) == 0, 于是 mean_A(OOS) == -mean_A(IS);
    IS 赢家的 IS 均值为正 => 其 OOS 均值为负 => 在 N=2 中排名最后 => lambda < 0.
    """
    T, S = 240, 8
    half = T // 2
    drift = np.concatenate([np.full(half, 0.01), np.full(T - half, -0.01)])
    noise = np.random.RandomState(2024).normal(0.0, 0.0005, size=T)
    noise = noise - noise.mean()                          # 保证 sum(A) == 0
    A = drift + noise
    assert abs(A.sum()) < 1e-15
    B = -A
    res = probability_backtest_overfitting(np.column_stack([A, B]), n_splits=S)
    assert res["pbo"] == 1.0
    assert res["lambda_mean"] == pytest.approx(LOG_HALF, rel=1e-12)   # 全为 logit(1/3)
    assert res["prob_oos_loss"] == 1.0


def test_pbo_pure_noise_mean_is_near_half():
    """纯噪声(所有配置同分布、无真实优势)下 PBO 的均值应接近 0.5.

    注意: 单次 CSCV 估计的离散度很大(实测 std ≈ 0.21, 因为 C(8,4)=70 个组合
    高度重叠、有效独立样本远少于 70), 所以这里断言的是**多种子均值**,
    而不是单次值落在 0.5 附近 —— 后者在数学上并不成立。
    """
    vals = []
    for seed in range(1000, 1012):
        r = np.random.RandomState(seed).normal(0.0, 0.01, size=(1000, 10))
        vals.append(probability_backtest_overfitting(r, n_splits=8)["pbo"])
    mean = float(np.mean(vals))
    assert 0.40 <= mean <= 0.70, f"纯噪声 PBO 均值={mean:.3f} 偏离 0.5 过多: {vals}"
    assert float(np.std(vals, ddof=1)) > 0.05, "PBO 不随数据变化, 疑似退化为常数"
    assert all(0.0 <= v <= 1.0 for v in vals)


def test_pbo_is_reproducible_for_a_fixed_seed():
    r = np.random.RandomState(4242).normal(0.0, 0.01, size=(800, 8))
    a = probability_backtest_overfitting(r, n_splits=8)["pbo"]
    b = probability_backtest_overfitting(r.copy(), n_splits=8)["pbo"]
    assert a == b
    assert 0.0 <= a <= 1.0


def test_pbo_custom_metric_is_honoured():
    """换成"平均收益"作度量时, 常数优势配置同样必胜 -> PBO == 0."""
    rng = np.random.RandomState(5)
    r = rng.normal(0.0, 0.01, size=(240, 4))
    r[:, 2] += 0.005
    res = probability_backtest_overfitting(r, n_splits=8, metric=lambda col: float(np.nanmean(col)))
    assert res["pbo"] == 0.0
    assert res["metric"] == "<lambda>"


# ==========================================================================
# 12. PBO: 参数与边界
# ==========================================================================
@pytest.mark.parametrize("bad", [3, 5, 7, 2])
def test_pbo_invalid_n_splits_raises(bad):
    with pytest.raises(ValueError):
        probability_backtest_overfitting(_pbo_matrix(), n_splits=bad)


def test_pbo_invalid_shapes_raise():
    with pytest.raises(ValueError):
        probability_backtest_overfitting(np.zeros(100), n_splits=8)          # 一维
    with pytest.raises(ValueError):
        probability_backtest_overfitting(np.zeros((100, 1)), n_splits=8)     # 只有 1 个配置
    with pytest.raises(ValueError):
        probability_backtest_overfitting(np.zeros((5, 4)), n_splits=8)       # T < S


def test_pbo_non_callable_metric_raises():
    with pytest.raises(ValueError):
        probability_backtest_overfitting(_pbo_matrix(), n_splits=8, metric="sharpe")


def test_pbo_all_nan_input_raises_instead_of_faking_a_value():
    """收益矩阵全为 NaN 时无任何组合可算 -> 必须 ValueError, 不能静默给 0.

    (注: T == S 即"每块 1 天"时 IS 仍有 S/2 天可用, 度量照样算得出来, 因此
    本函数不像最初设想的那样在"块太短"时报错 —— 统计意义由调用方负责,
    这里只保证"算不出来"时不返回假值。)
    """
    with pytest.raises(ValueError):
        probability_backtest_overfitting(np.full((80, 4), np.nan), n_splits=8)


def test_pbo_all_nan_column_is_skipped_not_counted():
    """某配置整列为 NaN 时, IS 最优仍应能选出有限值的那一个 -> 不抛错."""
    r = np.random.RandomState(3).normal(0.0, 0.01, size=(240, 4))
    r[:, 1] = np.nan
    res = probability_backtest_overfitting(r, n_splits=8)
    assert 0.0 <= res["pbo"] <= 1.0 and res["n_configs"] == 4
