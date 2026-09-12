# -*- coding: utf-8 -*-
"""置换检验回归测试 (2026-09-13).

守护本轮修复: 原实现对"窗口汇总值"先做 permutation 再取 mean, 而均值是置换
不变量 —— 置换分布退化为常数(实测标准差 ~1e-16 的浮点误差), p 恒为 1.0,
该检查在 n>=12 时永不可能通过。现改为精确符号翻转置换检验。

用例全部离线, 不依赖网络与真实回测数据。
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


# 12 个窗口的 Sharpe (取自 12 窗口 OOS 实测值)
SHARPES_12 = [2.588, 3.144, 0.067, 2.183, 3.728, 1.296,
              4.382, 1.944, 2.301, 0.160, 4.025, 1.132]


# --------------------------------------------------------------------------
# 1. 核心回归: 原假设分布不得退化
# --------------------------------------------------------------------------
def test_null_distribution_is_not_degenerate(ot):
    """置换分布必须有真实离散度 —— 这正是旧实现失效的地方.

    旧实现: 统计量 = mean(permutation(x)), 恒等于 mean(x) => std ≈ 1e-16,
            且 perm_mean == actual_mean (完全相同)。
    """
    t = ot.sign_flip_test(np.array(SHARPES_12))
    assert t["perm_std"] > 0.01, f"置换分布退化为常数: std={t['perm_std']}"
    # H0 下统计量期望为 0 (旧实现下会等于 actual_mean)
    assert abs(t["perm_mean"]) < 1e-9
    assert t["perm_mean"] != pytest.approx(t["actual_mean"], abs=0.1)


# --------------------------------------------------------------------------
# 2. 精确枚举性质
# --------------------------------------------------------------------------
def test_exact_enumeration_for_12_windows(ot):
    """n<=16 走精确枚举, 单侧 p 的最小可达值为 1/2^n."""
    t = ot.sign_flip_test(np.array(SHARPES_12))
    assert t["exact"] is True
    assert t["n"] == 12
    assert t["p_min"] == pytest.approx(1.0 / 4096)
    # 12 个窗口全为正收益: 只有全 + 符号向量能达到观测均值
    assert t["p_value"] == pytest.approx(1.0 / 4096)
    assert t["rank_pct"] == pytest.approx(100.0)


def test_monte_carlo_fallback_beyond_max_exact(ot):
    """超过 max_exact 个窗口时退化为固定种子蒙特卡洛, 结果应稳定可复现."""
    x = np.array(SHARPES_12 * 2)          # n=24
    a = ot.sign_flip_test(x, n_mc=20000)
    b = ot.sign_flip_test(x, n_mc=20000)
    assert a["exact"] is False
    assert "蒙特卡洛" in a["scheme"]
    assert a["p_value"] == b["p_value"]   # 固定种子 -> 可复现
    assert 0.0 < a["p_value"] < 0.10


# --------------------------------------------------------------------------
# 3. 判别力: 有技能 vs 无技能
# --------------------------------------------------------------------------
def test_all_positive_windows_are_significant(ot):
    """全部窗口为正 -> 显著 (p < 0.10)."""
    t = ot.sign_flip_test(np.array(SHARPES_12))
    assert t["p_value"] < 0.10


def test_symmetric_windows_are_not_significant(ot):
    """正负对称的窗口序列 -> 不应判为显著."""
    x = np.array([1.0, -1.0, 2.0, -2.0, 3.0, -3.0,
                  1.5, -1.5, 0.5, -0.5, 2.5, -2.5])
    t = ot.sign_flip_test(x)
    assert t["p_value"] > 0.10
    assert t["actual_mean"] == pytest.approx(0.0)
    assert t["p_two_sided"] > 0.10


def test_two_sided_p_never_below_one_sided(ot):
    t = ot.sign_flip_test(np.array(SHARPES_12))
    assert t["p_two_sided"] >= t["p_value"] - 1e-12


def test_single_window_raises(ot):
    with pytest.raises(ValueError):
        ot.sign_flip_test(np.array([1.0]))


# --------------------------------------------------------------------------
# 4. 端到端: test_permutation 现在可以 PASS
# --------------------------------------------------------------------------
def _write_results(path, sharpes_per_window):
    rows = []
    for i, s in enumerate(sharpes_per_window):
        rows.append({"day": f"20{20 + i}-01-01", "ok": True,
                     "stats": {"sharpe_ratio": s, "total_return": s * 10.0,
                               "annual_return": s * 20.0, "max_ddpercent": -8.0}})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


def test_test_permutation_passes_on_12_positive_windows(ot, tmp_path, monkeypatch):
    """端到端: 12 个正 Sharpe 窗口应让两项置换检查全部 PASS."""
    fp = tmp_path / "nonoverlap.json"
    _write_results(str(fp), SHARPES_12)
    monkeypatch.setenv("OVERFIT_RESULTS_FILE", str(fp))

    r = ot.test_permutation()
    assert r is not None
    assert r["n"] == 12 and r["exact"] is True
    assert r["p_value"] < 0.10
    assert r["p_value"] != pytest.approx(1.0), "p 值不得恒为 1 (旧实现缺陷)"

    statuses = [it["status"] for it in ot.R.items[-2:]]
    assert statuses == ["PASS", "PASS"], f"置换检验应全部通过, 实际 {statuses}"
    assert all(it["category"] == "置换检验" for it in ot.R.items[-2:])


def test_test_permutation_symmetric_windows_fail(ot, tmp_path, monkeypatch):
    """端到端: 无技能(正负对称)的窗口序列应判 FAIL, 证明检查有判别力."""
    sym = [1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 1.5, -1.5, 0.5, -0.5, 2.5, -2.5]
    fp = tmp_path / "sym.json"
    _write_results(str(fp), sym)
    monkeypatch.setenv("OVERFIT_RESULTS_FILE", str(fp))

    r = ot.test_permutation()
    assert r["p_value"] > 0.10
    assert ot.R.items[-2]["status"] == "FAIL"
