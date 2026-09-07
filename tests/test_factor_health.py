# -*- coding: utf-8 -*-
"""因子健康处置测试 (2026-09-07): 失效因子(方向翻转/强度收敛)打分权重隔离."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestFactorHealthFlags(unittest.TestCase):
    def _cfg(self):
        from factor_gate import _Cfg
        return _Cfg()

    def test_flipped_reversal_factor_isolated(self):
        from factor_gate import factor_health_flags
        # vol 为反转义因子, 长期负 IC, 短期转正 -> 隔离
        fics = {
            "vol": {"long_mean": -0.0757, "short_mean": 0.0794},
            "mom_20": {"long_mean": -0.0652, "short_mean": -0.3480},
        }
        flags = factor_health_flags(fics)
        self.assertIn("vol", flags["isolated"])
        self.assertEqual(flags["isolated"]["vol"]["action"], "isolate")
        # mom_rev(mom_20) 负 IC 加深 = 更有效 -> 不隔离
        self.assertNotIn("mom_rev", flags["isolated"])
        self.assertNotIn("mom_20", flags["isolated"])

    def test_weakened_reversal_factor_isolated(self):
        from factor_gate import factor_health_flags
        fics = {
            "vol": {"long_mean": -0.10, "short_mean": -0.015},  # 收敛到 <70%
        }
        flags = factor_health_flags(fics)
        self.assertIn("vol", flags["isolated"])

    def test_healthy_factors_kept(self):
        from factor_gate import factor_health_flags
        fics = {
            "vol": {"long_mean": -0.08, "short_mean": -0.10},   # 负 IC 保持
            "mom_20": {"long_mean": -0.06, "short_mean": -0.20},
        }
        flags = factor_health_flags(fics)
        self.assertEqual(flags["isolated"], {})

    def test_no_curve_data_graceful(self):
        from factor_gate import factor_health_flags
        flags = factor_health_flags({})
        self.assertEqual(flags["isolated"], {})
        self.assertEqual(flags["unstable"], [])


class TestSelectorHealthOverride(unittest.TestCase):
    def test_isolated_factor_weight_zeroed(self):
        from factor_gate import factor_health_flags
        import factor_library as fl

        orig = factor_health_flags
        try:
            # 模拟: vol 失效被隔离
            factor_gate = __import__("factor_gate")
            factor_gate.factor_health_flags = lambda *a, **k: {
                "enabled": True,
                "isolated": {"vol": {"action": "isolate", "reason": "flip"}},
                "unstable": ["vol"], "drift_detail": {},
            }
            fl._health_logged.clear()
            W = fl.selector_weights()
            self.assertEqual(W.get("vol"), 0.0)
            self.assertEqual(W.get("mom_rev"), 0.0)  # 静态默认 mom_rev=0
        finally:
            factor_gate.factor_health_flags = orig

    def test_health_disabled_by_env(self):
        import factor_library as fl
        os.environ["FACTOR_HEALTH_ENABLED"] = "0"
        try:
            fl._health_logged.clear()
            W = fl.selector_weights()
            self.assertGreaterEqual(W.get("vol", 0.0), 0.0)  # 不因健康处置改动
            self.assertIn("vol", W)
        finally:
            os.environ.pop("FACTOR_HEALTH_ENABLED", None)


if __name__ == "__main__":
    unittest.main()
