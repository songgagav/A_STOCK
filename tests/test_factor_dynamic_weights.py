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
    DYNAMIC_FACTORS, apply_dynamic_weights, load_dynamic_weights,
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


if __name__ == "__main__":
    unittest.main()