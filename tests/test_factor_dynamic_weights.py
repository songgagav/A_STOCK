# -*- coding: utf-8 -*-
"""单元测试: PPO 动态因子权重分配."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from factor_dynamic_weights import (
    DYNAMIC_FACTORS, apply_dynamic_weights, compute_factor_day_metrics,
    load_dynamic_weights,
)


class TestApplyDynamicWeights(unittest.TestCase):
    def setUp(self):
        self.static = {"pb_inv": 0.25, "ep": 0.25, "ocf_ps": 0.25, "roe_yy_chg": 0.25}

    def test_no_dynamic_returns_static(self):
        result = apply_dynamic_weights(self.static, None)
        for k, v in self.static.items():
            self.assertAlmostEqual(result[k], v)

    def test_blend_with_dynamic(self):
        dynamic = {"pb_inv": 0.4, "ep": 0.3, "ocf_ps": 0.2, "roe_yy_chg": 0.1}
        result = apply_dynamic_weights(self.static, dynamic, blend_ratio=0.5)
        self.assertAlmostEqual(sum(result.values()), 1.0, places=5)
        for k in self.static:
            expected = self.static[k] * 0.5 + dynamic[k] * 0.5
            total = sum(self.static[k] * 0.5 + dynamic[k] * 0.5 for k in self.static)
            expected /= total
            self.assertAlmostEqual(result[k], expected, places=5)

    def test_blend_ratio_zero(self):
        dynamic = {"pb_inv": 0.9, "ep": 0.1, "ocf_ps": 0.0, "roe_yy_chg": 0.0}
        result = apply_dynamic_weights(self.static, dynamic, blend_ratio=0.0)
        for k in self.static:
            self.assertAlmostEqual(result[k], 0.25)

    def test_blend_ratio_one(self):
        dynamic = {"pb_inv": 1.0, "ep": 0.0, "ocf_ps": 0.0, "roe_yy_chg": 0.0}
        result = apply_dynamic_weights(self.static, dynamic, blend_ratio=1.0)
        self.assertAlmostEqual(result["pb_inv"], 1.0)
        self.assertAlmostEqual(result["ep"], 0.0)

    def test_partial_dynamic_weights(self):
        """动态权重只覆盖部分因子时, 缺失的用静态权重."""
        dynamic = {"pb_inv": 1.0}
        result = apply_dynamic_weights(self.static, dynamic, blend_ratio=0.5)
        self.assertAlmostEqual(sum(result.values()), 1.0, places=5)

    def test_dynamic_factor_names(self):
        self.assertEqual(len(DYNAMIC_FACTORS), 5)
        self.assertIn("pb_inv", DYNAMIC_FACTORS)
        self.assertIn("gp4", DYNAMIC_FACTORS)

    def test_load_dynamic_weights_nonexistent(self):
        result = load_dynamic_weights("99999999")
        self.assertIsNone(result)


class TestFactorDayMetrics(unittest.TestCase):
    def test_metrics_are_cross_sectional_not_mean_scores(self):
        scores = {
            "f": {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0},
        }
        labels = {"a": -0.05, "b": -0.02, "c": 0.0, "d": 0.03, "e": 0.06}
        out = compute_factor_day_metrics(scores, labels, min_samples=5, quantile=0.2)
        self.assertAlmostEqual(out["f"]["rank_ic"], 1.0)
        self.assertAlmostEqual(out["f"]["long_short_return"], 0.11)
        self.assertEqual(out["f"]["n_labeled"], 5)

    def test_unmatured_labels_are_not_zero_filled(self):
        scores = {"f": {"a": 1.0, "b": 2.0, "c": 3.0}}
        labels = {"a": 0.1, "b": float("nan"), "c": float("nan")}
        out = compute_factor_day_metrics(scores, labels, min_samples=3)
        self.assertTrue(np.isnan(out["f"]["rank_ic"]))
        self.assertTrue(np.isnan(out["f"]["long_short_return"]))


if __name__ == "__main__":
    unittest.main()
