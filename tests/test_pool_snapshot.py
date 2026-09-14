# -*- coding: utf-8 -*-
"""池快照的口径复算正确性 (pool_snapshot).

关键不变量: 从快照复算的 Top-N 必须与 selector 实时计算的结果**逐位一致**,
否则"秒级敏感性测试"就是错的。单元测试用合成数据, 端到端一致性由
scripts/verify_pool_snapshot.py 在真实数据上验证。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))


@pytest.fixture()
def snap_df():
    """40 只标的: score 单调递增, fml_z 有噪声且刻意让最高 z 者 score 很低."""
    n = 40
    score = np.linspace(0.2, 0.8, n)
    z = np.linspace(-2.0, 2.0, n) + np.sin(np.arange(n)) * 0.3
    return pd.DataFrame({
        "canon": [f"{600000 + i}.SH" for i in range(n)],
        "name": [f"S{i}" for i in range(n)],
        "price": np.full(n, 10.0),
        "signal": np.zeros(n), "trend": np.zeros(n), "vol": np.zeros(n),
        "mom_rev": np.zeros(n), "govern": np.zeros(n), "liquidity": np.zeros(n),
        "score": score,
        "fml_raw": z,
        "fml_z": z,
    })


def test_pct_rank_matches_manual_definition():
    from pool_snapshot import pct_rank
    a = np.array([10.0, 30.0, 20.0, 40.0])
    r = pct_rank(a)
    assert r[np.argmax(a)] == pytest.approx(1.0)
    assert r[np.argmin(a)] == pytest.approx(0.0)
    assert r[2] == pytest.approx(1.0 / 3.0)          # 20 是第二小


def test_rank_by_score_is_pure_score_order(snap_df):
    from pool_snapshot import top_from_snapshot
    top = top_from_snapshot(snap_df, n=5, rank_by="score")
    assert [x["name"] for x in top] == ["S39", "S38", "S37", "S36", "S35"]


def test_fusion_rank_uses_alpha_blend(snap_df):
    from pool_snapshot import fusion_key_from_snapshot, pct_rank, top_from_snapshot
    k = fusion_key_from_snapshot(snap_df, alpha=1.0)
    assert np.allclose(k, pct_rank(snap_df["fml_z"].to_numpy()))
    # alpha=0 时退化为旧排序
    k0 = fusion_key_from_snapshot(snap_df, alpha=0.0)
    assert np.allclose(k0, pct_rank(snap_df["score"].to_numpy()))
    # 纯融合 alpha=1 时, 头部应按 z 而非 score
    top = top_from_snapshot(snap_df, n=3, rank_by="fusion", alpha=1.0)
    assert top[0]["name"] == snap_df.loc[snap_df["fml_z"].idxmax(), "name"]


def test_trim_puts_extreme_head_at_bottom(snap_df):
    """截尾后, 被截者应落到**最低并列档**(等价 selector 的 nanmin 处理).

    注意: 被截者的 z 被置为全池最小, 与"原本最小者"并列; `argsort(argsort())` 会给
    并列值分配**不同**名次(0..k), 所以被截者拿到的是 0~k 中的某一档, 而不是恰好 0.0。
    这一点必须与 selector 完全一致 —— 本测试锁定的正是"落在并列最低档"这个事实。
    """
    from pool_snapshot import fusion_key_from_snapshot, top_from_snapshot
    k = fusion_key_from_snapshot(snap_df, alpha=1.0, trim_q=0.10)
    n = len(snap_df)
    worst_z_idx = int(np.argmax(snap_df["fml_z"].to_numpy()))
    n_trim = max(1, int(round(n * 0.10)))
    assert k[worst_z_idx] <= (n_trim + 1) / (n - 1) + 1e-9   # 在最低并列档内
    assert k[worst_z_idx] < np.median(k)                     # 明确不在头部
    top = top_from_snapshot(snap_df, n=3, rank_by="fusion", alpha=1.0, trim_q=0.10)
    assert snap_df.loc[worst_z_idx, "name"] not in [x["name"] for x in top]


def test_blend_w_replicates_formula_and_trim_zero(snap_df):
    """掺入口径: score' = 0.9*score + 0.1*rank(fml); 被截者 rank 恒为 0.0."""
    from pool_snapshot import blend_score_from_snapshot, pct_rank
    w = 0.10
    out = blend_score_from_snapshot(snap_df, w=w)
    exp = (1 - w) * snap_df["score"].to_numpy() + w * pct_rank(snap_df["fml_raw"].to_numpy())
    assert np.allclose(out, exp)
    out_t = blend_score_from_snapshot(snap_df, w=w, trim_q=0.10)
    hi = int(np.argmax(snap_df["fml_raw"].to_numpy()))
    assert out_t[hi] == pytest.approx((1 - w) * snap_df["score"].to_numpy()[hi])


def test_save_load_roundtrip(snap_df, tmp_path, monkeypatch):
    import pool_snapshot as ps
    monkeypatch.setattr(ps, "SNAP_DIR", str(tmp_path))
    monkeypatch.setattr(ps, "data_version", lambda: "testver")
    scored = snap_df.to_dict("records")
    p = ps.save("2024-07-03", 10, scored,
                fml_raw=dict(zip(snap_df["canon"], snap_df["fml_raw"])),
                fml_z=dict(zip([c.split(".")[0] for c in snap_df["canon"]],
                               snap_df["fml_z"])))
    assert p and os.path.exists(p)
    assert "testver" in os.path.basename(p)            # 文件名带数据版本
    back = ps.load("2024-07-03", 10, ver="testver")
    assert back is not None and len(back) == len(snap_df)
    assert ps.available_days(10, ver="testver") == ["2024-07-03"]
    # fml_z 以 6 位代码对齐写回
    assert np.allclose(back["fml_z"].to_numpy(), snap_df["fml_z"].to_numpy(), equal_nan=True)


def test_load_missing_returns_none(tmp_path, monkeypatch):
    import pool_snapshot as ps
    monkeypatch.setattr(ps, "SNAP_DIR", str(tmp_path))
    assert ps.load("1999-01-01", 10, ver="x") is None
    assert ps.available_days(10, ver="x") == []


def test_ties_preserve_original_order():
    """同分标的必须保持原始顺序(实时路径用稳定的 list.sort).

    回归背景: pandas 默认快排不稳定, 曾导致"仅两两对调"的 Top-N 差异。
    """
    from pool_snapshot import top_from_snapshot
    df = pd.DataFrame({
        "canon": ["A.SH", "B.SH", "C.SH"], "name": ["A", "B", "C"],
        "price": [1.0] * 3, "signal": [0.0] * 3, "trend": [0.0] * 3,
        "vol": [0.0] * 3, "mom_rev": [0.0] * 3, "govern": [0.0] * 3,
        "liquidity": [0.0] * 3, "score": [0.5, 0.5, 0.1],
        "fml_raw": [0.0] * 3, "fml_z": [0.0] * 3,
    })
    assert [x["name"] for x in top_from_snapshot(df, n=3)] == ["A", "B", "C"]


def test_apply_fusion_rank_accepts_dict_and_is_not_silent(monkeypatch):
    """`_apply_fusion_rank(zs=<dict>)` 必须生效(回归: 传 dict 曾抛 TypeError 被静默吞掉).

    症状是"融合排序看似打开、实际按 score 排序", 从结果完全看不出来。
    """
    monkeypatch.setenv("RANK_BY_FUSION", "1")
    monkeypatch.setenv("FUSION_RANK_ALPHA", "1.0")
    from selector import RotationSelector
    n = 40
    scored = [{"canon": f"{600000 + i}.SH", "score": round(0.2 + i * 0.01, 4)}
              for i in range(n)]
    zmap = {f"{600000 + i}": float(i) for i in range(n)}   # 融合序与 score 序**相反**
    sel = RotationSelector.__new__(RotationSelector)       # 不连库
    sel._apply_fusion_rank(scored, "2024-07-03", zs=zmap)
    assert all("fusion_rank_key" in x for x in scored), "融合排序未生效(被静默跳过)"
    # 纯融合时, 融合分最高者(canon 尾号 6039)必须拿到最高 rank_key
    top = max(scored, key=lambda x: x["fusion_rank_key"])
    assert top["canon"].startswith("600039")
