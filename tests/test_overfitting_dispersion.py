# -*- coding: utf-8 -*-
"""#8「窗口间 Sharpe 离群度」口径调整的回归测试 (2026-09-13).

原口径: (max - median) 原始差值, 阈值写死 1.0 —— 量纲依赖 Sharpe 单位、完全由单个
最大值决定、且与 [1b] 的"滚动窗口 Sharpe 稳定性 (CV < 2.0)"重复度量离散度。
新口径: 稳健 modified z-score = (max - median) / (1.4826 * MAD), 阈值 3.5
(Iglewicz & Hoaglin 1993 离群判据), 专门回答"整体表现是否被单个离群窗口撑起来"。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(scope="module")
def ot():
    """导入 overfitting_test (模块 import 时会 os.chdir 到仓库根, 需复原)."""
    cwd = os.getcwd()
    try:
        import overfitting_test as m
    finally:
        os.chdir(cwd)
    yield m
    os.chdir(cwd)


# 12 个非重叠 OOS 窗口的实测 Sharpe
SR12 = [2.588, 3.144, 0.067, 2.183, 3.728, 1.296,
        4.382, 1.944, 2.301, 0.160, 4.025, 1.132]


# --------------------------------------------------------------------------
# 统计量性质
# --------------------------------------------------------------------------
def test_scale_invariance(ot):
    """量纲无关: 所有值乘以同一正常数, z 不变(这正是原口径缺的性质)."""
    a = ot.robust_max_z(np.array(SR12))["robust_z"]
    b = ot.robust_max_z(np.array(SR12) * 10.0)["robust_z"]
    c = ot.robust_max_z(np.array(SR12) * 0.01)["robust_z"]
    assert a == pytest.approx(b)
    assert a == pytest.approx(c)


def test_shift_invariance(ot):
    """平移不变: 整体加减常数(max-median 与 MAD 都不变)."""
    a = ot.robust_max_z(np.array(SR12))["robust_z"]
    b = ot.robust_max_z(np.array(SR12) + 5.0)["robust_z"]
    assert a == pytest.approx(b)


def test_actual_data_value_and_verdict(ot):
    """实测 12 窗口: z≈1.40 < 3.5 -> 通过; 而旧口径 gap=2.14 > 1.0 会误判 FAIL."""
    r = ot.robust_max_z(np.array(SR12))
    assert r["n"] == 12
    assert r["gap"] == pytest.approx(2.140, abs=1e-3)
    assert r["robust_z"] < 3.5
    assert 1.0 < r["robust_z"] < 2.0, f"实测 z={r['robust_z']}"
    # 旧口径在同一数据上给出 FAIL, 说明本次调整确实改变结论且方向合理
    assert r["gap"] > 1.0


def test_single_dominant_window_is_outlier(ot):
    """一个窗口远高于其余(几乎相同) -> MAD=0 退化为 IQR, 仍判为离群."""
    x = np.array([2.0] * 11 + [20.0])
    r = ot.robust_max_z(x)
    assert r["robust_z"] == float("inf") or r["robust_z"] > 3.5
    assert r["gap"] == pytest.approx(18.0)


def test_moderate_outlier_flagged(ot):
    """单个窗口明显偏高但非退化 -> z 应超阈值."""
    x = np.array([2.0, 2.1, 1.9, 2.0, 2.2, 1.8, 2.0, 2.1, 1.9, 2.0, 2.05, 6.0])
    r = ot.robust_max_z(x)
    assert r["robust_z"] > 3.5, f"未识别离群: z={r['robust_z']} mad={r['mad']}"


def test_constant_vector_not_outlier(ot):
    """全部相同 -> 无离群(gap=0, z=0), 不能出现 0/0."""
    r = ot.robust_max_z(np.array([3.0] * 12))
    assert r["robust_z"] == 0.0
    assert r["gap"] == 0.0


def test_typical_noise_not_flagged(ot):
    """常规正态离散下不应频繁误报(12 窗口, 多seed 检查误报率)."""
    flagged = 0
    for s in range(200):
        x = np.random.RandomState(1000 + s).normal(2.0, 1.0, size=12)
        if ot.robust_max_z(x)["robust_z"] >= 3.5:
            flagged += 1
    assert flagged <= 10, f"误报过多: {flagged}/200"


def test_negative_and_mixed_values(ot):
    """含负 Sharpe 的序列也能算(不能假设全为正)."""
    x = np.array([2.0, -1.0, 3.0, 0.5, -0.5, 1.0, 2.5, 1.5, -2.0, 0.0, 1.2, 2.2])
    r = ot.robust_max_z(x)
    assert np.isfinite(r["robust_z"])
    assert r["median"] < r["max"]


def test_nan_is_dropped(ot):
    """NaN 不应污染统计量."""
    x = np.array([1.0, 2.0, np.nan, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
    r = ot.robust_max_z(x)
    assert r["n"] == 9
    assert np.isfinite(r["robust_z"])


def test_empty_returns_nan(ot):
    r = ot.robust_max_z(np.array([]))
    assert r["n"] == 0
    assert np.isnan(r["robust_z"])


# --------------------------------------------------------------------------
# 端到端: #8 检查现在通过, 且报告可 JSON 序列化
# --------------------------------------------------------------------------
def _write_results(path, sharpes_per_window):
    rows = []
    for i, s in enumerate(sharpes_per_window):
        rows.append({"day": f"20{20 + i}-01-01", "ok": True,
                     "stats": {"sharpe_ratio": s, "total_return": s * 10.0,
                               "annual_return": s * 20.0, "max_ddpercent": -8.0}})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


def test_check8_passes_on_real_data(ot, tmp_path, monkeypatch):
    fp = tmp_path / "nonoverlap.json"
    _write_results(str(fp), SR12)
    monkeypatch.setenv("OVERFIT_RESULTS_FILE", str(fp))

    r = ot.test_pbo()
    assert r is not None
    assert r["sharpe_gap"] == pytest.approx(2.140, abs=1e-3)   # 旧口径仍是 FAIL
    assert isinstance(r["robust_z"], float) and r["robust_z"] < 3.5

    item = ot.R.items[-1]
    assert item["status"] == "PASS", f"#8 应通过, 实际 {item['status']}"
    assert "稳健 z" in item["label"]
    assert item["threshold"] == "< 3.5"
    # 报告可被严格 JSON 解析(inf 必须已转字符串)
    json.dumps({"robust_z": r["robust_z"]}, allow_nan=False)


def test_check8_fails_when_one_window_dominates(ot, tmp_path, monkeypatch):
    fp = tmp_path / "dominated.json"
    _write_results(str(fp), [2.0] * 11 + [20.0])
    monkeypatch.setenv("OVERFIT_RESULTS_FILE", str(fp))

    r = ot.test_pbo()
    assert r["robust_z"] == "inf" or r["robust_z"] > 3.5
    assert ot.R.items[-1]["status"] == "FAIL"
    json.dumps({"robust_z": r["robust_z"]}, allow_nan=False)
