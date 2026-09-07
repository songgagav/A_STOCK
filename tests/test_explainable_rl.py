# -*- coding: utf-8 -*-
"""单元测试: 可解释强化学习 + 自适应特征选择."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from explainable_rl import (
    FeatureImportanceTracker, AdaptiveFeatureSelector,
    DecisionTrace, compute_explainability_penalty,
)


class TestFeatureImportanceTracker(unittest.TestCase):
    def setUp(self):
        self.ft = FeatureImportanceTracker(["f1", "f2", "f3"])

    def test_initial_importance_zero(self):
        imp = self.ft.get_importance()
        self.assertEqual(len(imp), 3)
        for v in imp.values():
            self.assertEqual(v, 0.0)

    def test_record_updates_importance(self):
        self.ft.record(np.array([1.0, 0.5, 0.0]), np.array([0.1, 0.2]), 0.5)
        imp = self.ft.get_importance()
        self.assertGreater(imp["f1"], 0.0)

    def test_top_features(self):
        for _ in range(5):
            self.ft.record(
                np.random.randn(3), np.random.randn(2), float(np.random.randn() * 0.1))
        top = self.ft.get_top_features(2)
        self.assertEqual(len(top), 2)
        self.assertIsInstance(top[0], str)

    def test_reset_clears(self):
        self.ft.record(np.array([1.0, 0.5, 0.0]), np.array([0.1]), 0.5)
        self.ft.reset()
        self.assertEqual(self.ft._total_steps, 0)


class TestAdaptiveFeatureSelector(unittest.TestCase):
    def setUp(self):
        self.fs = AdaptiveFeatureSelector(
            ["f1", "f2", "f3", "f4"],
            ic_threshold=0.02,
            min_features=2,
            eval_interval=10,
        )

    def test_initial_active_all(self):
        self.assertEqual(len(self.fs.active_features), 4)

    def test_update_returns_ic_estimates(self):
        for _ in range(15):
            fv = {f"f{i}": np.random.randn() for i in range(1, 5)}
            self.fs.update(fv, 0.01)
        ic = self.fs.update({"f1": 0.5, "f2": -0.3, "f3": 0.1, "f4": 0.0}, 0.02)
        for name in self.fs.active_features:
            self.assertIn(name, ic)

    def test_reset_restores_all_features(self):
        self.fs._active_features = ["f1"]
        self.fs.reset()
        self.assertEqual(len(self.fs.active_features), 4)


class TestDecisionTrace(unittest.TestCase):
    def setUp(self):
        self.dt = DecisionTrace(["f1", "f2", "f3", "f4", "f5"])

    def test_record_creates_entry(self):
        self.dt.record(np.random.randn(5), np.random.randn(3), 0.5)
        self.assertEqual(len(self.dt._records), 1)

    def test_get_recent(self):
        for _ in range(5):
            self.dt.record(np.random.randn(5), np.random.randn(3), float(np.random.randn() * 0.1))
        recent = self.dt.get_recent(3)
        self.assertEqual(len(recent), 3)

    def test_get_extreme_decisions(self):
        self.dt.record(np.random.randn(5), np.random.randn(3), 4.0)
        self.dt.record(np.random.randn(5), np.random.randn(3), 0.1)
        extreme = self.dt.get_extreme_decisions(3.0)
        self.assertEqual(len(extreme), 1)

    def test_get_explanation(self):
        self.dt.record(np.random.randn(5), np.random.randn(3), 0.5)
        explanation = self.dt.get_explanation()
        self.assertIn("决策", explanation)

    def test_max_records_enforced(self):
        dt = DecisionTrace(["f1"], max_records=3)
        for _ in range(10):
            dt.record(np.random.randn(1), np.random.randn(1), 0.1)
        self.assertLessEqual(len(dt._records), 3)

    def test_save_and_empty(self):
        dt = DecisionTrace(["f1"])
        explanation = dt.get_explanation()
        self.assertEqual(explanation, "无决策记录")

    def test_reset(self):
        self.dt.record(np.random.randn(5), np.random.randn(3), 0.5)
        self.dt.reset()
        self.assertEqual(len(self.dt._records), 0)


class TestExplainabilityPenalty(unittest.TestCase):
    def test_high_concentration_penalized(self):
        weights = np.array([0.8, 0.1, 0.05, 0.05])
        penalty = compute_explainability_penalty(weights, ["a", "b", "c", "d"], 0.5)
        self.assertGreater(penalty, 0.0)

    def test_low_concentration_no_penalty(self):
        weights = np.array([0.25, 0.25, 0.25, 0.25])
        penalty = compute_explainability_penalty(weights, ["a", "b", "c", "d"], 0.5)
        self.assertEqual(penalty, 0.0)

    def test_empty_weights(self):
        penalty = compute_explainability_penalty(np.array([]), [])
        self.assertEqual(penalty, 0.0)


if __name__ == "__main__":
    unittest.main()