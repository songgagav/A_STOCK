# -*- coding: utf-8 -*-
"""单元测试: 多尺度信号分解 + Hybrid-GRPO."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from wavelet_decomposition import (
    _db4_wavelet_decomp, extract_wavelet_features,
    smooth_reward_with_wavelet, compute_grpo_gae,
)


class TestWaveletDecomp(unittest.TestCase):
    def test_short_series_returns_zeros(self):
        result = _db4_wavelet_decomp(np.array([1.0]), 3)
        self.assertEqual(len(result["trend"]), 1)

    def test_normal_series_returns_all_keys(self):
        x = np.sin(np.linspace(0, 4 * np.pi, 100)) + np.random.randn(100) * 0.1
        result = _db4_wavelet_decomp(x, 3)
        for key in ["trend", "volatility", "detail", "energy", "trend_ratio", "vol_ratio"]:
            self.assertIn(key, result)
        self.assertEqual(len(result["trend"]), 100)
        self.assertGreater(result["energy"], 0)

    def test_trend_ratio_in_range(self):
        x = np.random.randn(64)
        result = _db4_wavelet_decomp(x, 3)
        self.assertGreaterEqual(result["trend_ratio"], 0.0)
        self.assertLessEqual(result["trend_ratio"], 1.0)

    def test_vol_ratio_in_range(self):
        x = np.random.randn(64)
        result = _db4_wavelet_decomp(x, 3)
        self.assertGreaterEqual(result["vol_ratio"], 0.0)
        self.assertLessEqual(result["vol_ratio"], 1.0)

    def test_energy_positive(self):
        x = np.ones(32)
        result = _db4_wavelet_decomp(x, 2)
        self.assertGreater(result["energy"], 0.0)


class TestExtractWaveletFeatures(unittest.TestCase):
    def test_output_shape(self):
        x = np.random.randn(100)
        features = extract_wavelet_features(x, level=3, window=30)
        self.assertEqual(features.shape, (100, 5))

    def test_short_series_adapts_window(self):
        x = np.random.randn(10)
        features = extract_wavelet_features(x, level=2, window=60)
        self.assertEqual(features.shape, (10, 5))

    def test_features_are_finite(self):
        x = np.random.randn(80)
        features = extract_wavelet_features(x, level=3, window=40)
        self.assertTrue(np.all(np.isfinite(features)))


class TestSmoothReward(unittest.TestCase):
    def test_short_series_unchanged(self):
        r = np.array([1.0, 2.0])
        smoothed = smooth_reward_with_wavelet(r, 2)
        np.testing.assert_array_equal(r, smoothed)

    def test_smoothed_length(self):
        r = np.random.randn(30)
        smoothed = smooth_reward_with_wavelet(r, 2)
        self.assertEqual(len(smoothed), 30)

    def test_smoothed_values_are_finite(self):
        r = np.random.randn(50)
        smoothed = smooth_reward_with_wavelet(r, 3)
        self.assertTrue(np.all(np.isfinite(smoothed)))


class TestGrpoGae(unittest.TestCase):
    def test_gae_basic(self):
        rewards = np.random.randn(20) * 0.1
        values = np.random.randn(21) * 0.1
        gae, group = compute_grpo_gae(rewards, values, gamma=0.99, lam=0.95, group_size=4)
        self.assertEqual(len(gae), 20)
        self.assertEqual(len(group), 20)

    def test_group_advantages_normalized(self):
        rewards = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        values = np.zeros(9)
        gae, group = compute_grpo_gae(rewards, values, group_size=4)
        self.assertTrue(np.all(np.isfinite(group)))
        # 每组内均值应为 0
        for g in range(2):
            start = g * 4
            end = start + 4
            self.assertAlmostEqual(group[start:end].mean(), 0.0, places=5)

    def test_gae_single_group(self):
        rewards = np.array([1.0, 2.0])
        values = np.zeros(3)
        gae, group = compute_grpo_gae(rewards, values, group_size=4)
        self.assertEqual(len(gae), 2)
        self.assertTrue(np.all(np.isfinite(group)))


if __name__ == "__main__":
    unittest.main()