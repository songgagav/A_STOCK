# -*- coding: utf-8 -*-
"""因子健康处置测试 (2026-09-07): 失效因子(方向翻转/强度收敛)打分权重隔离."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


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


class TestFlipThresholdTightening(unittest.TestCase):
    """item 5 (2026-09-13): flipped 判据收紧 —— 短期正 IC 须有足够幅度才算方向翻转.

    原判据 |short| >= 0.01 过松: 长期 -0.20 的因子被 +0.015 的单期噪声判成
    "信号变反向"。收紧为 |short| >= max(绝对下限 0.02, 长期强度 x 0.25)。
    """

    def test_tiny_positive_blip_is_not_labelled_flipped(self):
        from factor_gate import factor_health_flags
        flags = factor_health_flags({"vol": {"long_mean": -0.20, "short_mean": 0.015}})
        d = flags["drift_detail"]["vol"]
        self.assertFalse(d["flipped"], "幅度仅 0.015 的正 IC 不应判为方向翻转")
        self.assertEqual(d["flip_threshold"], 0.05)   # max(0.02, 0.20*0.25)
        self.assertFalse(d["reason"].startswith("短期IC转正"))

    def test_flip_requires_ratio_of_long_strength(self):
        from factor_gate import factor_health_flags
        weak = factor_health_flags({"vol": {"long_mean": -0.20, "short_mean": 0.04}})
        self.assertFalse(weak["drift_detail"]["vol"]["flipped"], "0.04 < 0.05 门槛")
        strong = factor_health_flags({"vol": {"long_mean": -0.20, "short_mean": 0.06}})
        self.assertTrue(strong["drift_detail"]["vol"]["flipped"], "0.06 >= 0.05 门槛")

    def test_absolute_floor_applies_when_long_strength_is_small(self):
        from factor_gate import factor_health_flags
        # |long|=0.02 -> 阈值取绝对下限 0.02 (原判据 0.01 会误判为翻转)
        flags = factor_health_flags({"vol": {"long_mean": -0.02, "short_mean": 0.015}})
        d = flags["drift_detail"]["vol"]
        self.assertFalse(d["flipped"])
        self.assertEqual(d["flip_threshold"], 0.02)

    def test_thresholds_are_configurable(self):
        """把门槛调到 0 可复现旧行为(保证该判据可回退/可实验)."""
        from factor_gate import _Cfg, factor_health_flags

        class _Loose(_Cfg):
            flip_min_abs = 0.0
            flip_min_ratio = 0.0
            weaken_ratio = 0.0
            weaken_min_abs = 0.0

        fics = {"vol": {"long_mean": -0.20, "short_mean": 0.015}}
        self.assertFalse(factor_health_flags(fics)["drift_detail"]["vol"]["flipped"])
        self.assertTrue(
            factor_health_flags(fics, cfg=_Loose())["drift_detail"]["vol"]["flipped"],
            "门槛归零后应复现旧判据")

    def test_substantial_flip_still_isolated(self):
        """收紧后, 真正的方向翻转仍需被隔离(不能把门关死)."""
        from factor_gate import factor_health_flags
        flags = factor_health_flags({"vol": {"long_mean": -0.0757, "short_mean": 0.0794}})
        self.assertIn("vol", flags["isolated"])


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
