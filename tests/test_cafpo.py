# -*- coding: utf-8 -*-
"""单元测试: CAFPO 条件自编码因子投资 (cafpo)."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from cafpo import (
    ConditionalAutoEncoder, extract_latent_factors, latent_rank_ic,
)


def _synthetic_cafpo_data(
    T: int = 120,
    n_features: int = 6,
    n_conditions: int = 3,
    latent: int = 2,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """生成由潜在驱动 + 公司特征 + 噪声构成的因子矩阵."""
    rng = np.random.default_rng(seed)
    drivers = rng.normal(0, 1, (T, latent))          # 潜在收益驱动
    loadings = rng.normal(0, 1, (latent, n_features)) * 1.5
    cond = rng.normal(0, 1, (T, n_conditions))
    cond_g = rng.normal(0, 1, (n_conditions, n_features)) * 0.5
    noise = rng.normal(0, 0.08, (T, n_features))
    features = drivers @ loadings + cond @ cond_g + noise
    return features, cond, drivers


class TestConditionalAutoEncoder(unittest.TestCase):
    def setUp(self):
        self.features, self.cond, self.drivers = _synthetic_cafpo_data(seed=0)

    def test_fit_reduces_reconstruction_loss(self):
        m = ConditionalAutoEncoder(
            self.features.shape[1], self.cond.shape[1], latent_dim=2, seed=0)
        m.fit(self.features, self.cond, steps=500, lr=0.02, holdout_frac=0.2)
        self.assertTrue(len(m.train_losses) == 500)
        # 收敛: 尾部损失显著低于初始
        self.assertLess(m.train_losses[-1], m.train_losses[0] * 0.6)

    def test_reconstruction_r2(self):
        m = ConditionalAutoEncoder(
            self.features.shape[1], self.cond.shape[1], latent_dim=2, seed=0)
        m.fit(self.features, self.cond, steps=500, lr=0.02, holdout_frac=0.2)
        r2 = m.reconstruction_r2(self.features, self.cond)
        self.assertGreater(r2, 0.5, f"训练集重建解释度应较高, 实际 {r2:.3f}")

    def test_encode_shape_and_latent_correlates_with_driver(self):
        m = ConditionalAutoEncoder(
            self.features.shape[1], self.cond.shape[1], latent_dim=2, seed=0)
        m.fit(self.features, self.cond, steps=500, lr=0.02, holdout_frac=0.2)
        z = m.encode(self.features, self.cond)
        self.assertEqual(z.shape, (len(self.features), 2))
        # 任一潜在因子与某个收益驱动显著相关 (旋转自由度允许 |corr|>=阈值)
        best = max(
            abs(float(np.corrcoef(z[:, i], self.drivers[:, j])[0, 1]))
            for i in range(2) for j in range(2)
        )
        self.assertGreater(best, 0.4, f"潜在因子应捕捉收益驱动, 最优相关 {best:.3f}")

    def test_holds_out_val_split_finite(self):
        m = ConditionalAutoEncoder(
            self.features.shape[1], self.cond.shape[1], latent_dim=2, seed=0)
        m.fit(self.features, self.cond, steps=300, lr=0.02, holdout_frac=0.25)
        self.assertTrue(np.isfinite(m.val_losses[-1]))
        # 样本外重建可用且不劣于随机噪声水平
        n_val = int(round(len(self.features) * 0.25))
        v_r2 = m.reconstruction_r2(self.features[-n_val:], self.cond[-n_val:])
        self.assertGreater(v_r2, 0.0)


class TestExtractLatentFactors(unittest.TestCase):
    def test_end_to_end(self):
        features, cond, _ = _synthetic_cafpo_data(T=100, seed=1)
        res = extract_latent_factors(
            features, cond, latent_dim=2, steps=300, lr=0.02, seed=1)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["latent_factors"].shape, (100, 2))
        self.assertGreater(res["recon_r2"], 0.4)
        self.assertGreater(res["val_recon_r2"], 0.0)
        self.assertGreater(res["n_params"], 0)

    def test_bad_inputs(self):
        res = extract_latent_factors(np.ones((5, 2)), np.ones((4, 2)))
        self.assertFalse(res["ok"])


class TestLatentRankIC(unittest.TestCase):
    def test_correlated_latent_gives_high_ic(self):
        rng = np.random.default_rng(2)
        driver = rng.normal(size=100)
        y = driver * 0.5 + rng.normal(0, 0.1, 100)
        latent = np.column_stack([driver, rng.normal(size=100)])
        r = latent_rank_ic(latent, y)
        self.assertEqual(len(r["per_factor_ic"]), 2)
        self.assertGreater(r["per_factor_ic"][0], 0.6)
        self.assertLess(abs(r["per_factor_ic"][1]), 0.3)

    def test_too_short_returns_zero(self):
        r = latent_rank_ic(np.ones((4, 2)), np.ones(4))
        self.assertEqual(r["mean_ic"], 0.0)


if __name__ == "__main__":
    unittest.main()
