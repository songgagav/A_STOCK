# -*- coding: utf-8 -*-
"""单元测试: IC 门控 + 单日亏损防御 + 自适应调仓节奏 (factor_gate).

默认参数来自 data/factor_gate_config.json (env 优先); 机制类用例用显式
cfg 钉死滞后/阈值, 不依赖外部文件.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

import factor_gate
from factor_gate import compute_plan, build_plan_from_cache, _Cfg, _default_hyst


def _cfg(hyst: int = 1, **kw) -> _Cfg:
    """构造测试 cfg: 默认无滞后(hyst=1), 便于直接测档位映射."""
    c = _Cfg()
    c.hyst_enter = c.hyst_exit = int(hyst)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class TestJointRiskCriteria(unittest.TestCase):
    def test_normal_ic_full_exposure(self):
        p = compute_plan(0.03, 0.2, daily_loss=0.0, base_interval=3,
                         cfg=_cfg())
        self.assertEqual(p["regime"], "normal")
        self.assertEqual(p["exposure_mult"], 1.0)
        self.assertFalse(p["freeze_new_buys"])
        self.assertEqual(p["interval_days"], 3)

    def test_joint_risk_requires_strong_neg(self):
        p = compute_plan(-0.02, 0.50, daily_loss=0.0, base_interval=3,
                         cfg=_cfg())
        self.assertNotEqual(p["regime"], "risk")

    def test_joint_risk_requires_low_mean(self):
        p = compute_plan(-0.002, 0.9, daily_loss=0.0, base_interval=3,
                         cfg=_cfg())
        self.assertNotEqual(p["regime"], "risk")
        self.assertEqual(p["regime"], "caution")

    def test_joint_risk_ic_ir_gate(self):
        p = compute_plan(-0.02, 0.9, daily_loss=0.0, base_interval=3,
                         ic_ir=-0.2, cfg=_cfg())
        self.assertNotEqual(p["regime"], "risk")

    def test_joint_risk_all_conditions(self):
        p = compute_plan(-0.02, 0.9, daily_loss=0.0, base_interval=3,
                         ic_ir=-1.0, cfg=_cfg())
        self.assertEqual(p["raw_regime"], "risk")
        # 无滞后时立即生效 risk 档
        self.assertEqual(p["regime"], "risk")

    def test_caution_mild(self):
        p = compute_plan(-0.003, 0.5, daily_loss=0.0, base_interval=3,
                         cfg=_cfg())
        self.assertEqual(p["regime"], "caution")
        self.assertEqual(p["exposure_mult"], _Cfg.exp_caution)


class TestRiskExposureFloor(unittest.TestCase):
    def test_risk_exposure_uses_configured_floor(self):
        """risk 暴露使用配置下限 (配置默认 0.90, 且 >=0.80)."""
        self.assertGreaterEqual(_Cfg.exp_risk, 0.8)
        p = compute_plan(-0.03, 0.9, daily_loss=0.0, base_interval=3,
                         ic_ir=-2.0, cfg=_cfg())
        self.assertEqual(p["exposure_mult"], _Cfg.exp_risk)
        self.assertLessEqual(_Cfg.exp_risk, _Cfg.exp_caution, "risk 应比 caution 保守")


class TestHysteresis(unittest.TestCase):
    def test_enter_risk_needs_n_days(self):
        c = _cfg(hyst=3)
        h = _default_hyst()
        regimes = []
        for _ in range(3):
            p = compute_plan(-0.03, 0.9, daily_loss=0.0, base_interval=3,
                             ic_ir=-2.0, hyst=h, cfg=c)
            regimes.append(p["regime"])
        self.assertEqual(regimes[:2], ["caution", "caution"])  # 未满3日按 caution 过渡
        self.assertEqual(regimes[2], "risk")                    # 第3日进入

    def test_exit_risk_needs_n_clean_days(self):
        c = _cfg(hyst=3)
        h = _default_hyst()
        for _ in range(3):
            compute_plan(-0.03, 0.9, daily_loss=0.0, base_interval=3,
                         ic_ir=-2.0, hyst=h, cfg=c)
        self.assertTrue(h["in_risk"])
        out = []
        for _ in range(3):
            p = compute_plan(0.02, 0.1, daily_loss=0.0, base_interval=3,
                             hyst=h, cfg=c)
            out.append(p["regime"])
        self.assertEqual(out[:2], ["risk", "risk"])
        self.assertEqual(out[2], "normal")

    def test_unknown_does_not_change_state(self):
        c = _cfg(hyst=3)
        h = _default_hyst()
        for _ in range(3):
            compute_plan(-0.03, 0.9, daily_loss=0.0, base_interval=3,
                         ic_ir=-2.0, hyst=h, cfg=c)
        p = compute_plan(None, None, daily_loss=0.0, base_interval=3, hyst=h, cfg=c)
        self.assertEqual(p["regime"], "unknown")
        self.assertTrue(h["in_risk"], "未知期不应清空 risk 状态")


class TestDailyLossDefense(unittest.TestCase):
    def test_heavy_daily_loss_freezes_even_normal(self):
        p = compute_plan(0.03, 0.2, daily_loss=-0.025, base_interval=3, cfg=_cfg())
        self.assertTrue(p["freeze_new_buys"])
        self.assertEqual(p["loss_flag"], "heavy")
        self.assertGreaterEqual(p["interval_days"], 3 + _Cfg.int_risk_step)

    def test_warn_daily_loss_scales_exposure(self):
        p = compute_plan(0.03, 0.2, daily_loss=-0.012, base_interval=3, cfg=_cfg())
        self.assertFalse(p["freeze_new_buys"])
        self.assertEqual(p["loss_flag"], "warn")
        self.assertLessEqual(p["exposure_mult"], _Cfg.exp_caution)


class TestBuildPlanFromCache(unittest.TestCase):
    def _write_cache(self, ics: list) -> str:
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ic.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"daily_ic": ics}, f)
        return p

    def test_recent_window_risk_requires_persistence(self):
        # 使用配置默认滞后(hyst N=5): 连续 5 日强负才进入 risk
        c = _Cfg()
        ics = [{"date": f"d{i:03d}", "fwd5_ic": 0.05} for i in range(60)]
        for i in range(20):
            ics[40 + i]["fwd5_ic"] = -0.02 - i * 0.002
        p = self._write_cache(ics)
        h = _default_hyst()
        regimes = []
        for _ in range(c.hyst_enter):
            plan = build_plan_from_cache(daily_loss=0.0, base_interval=3,
                                         cache_path=p, hyst=h)
            regimes.append(plan["regime"])
        self.assertEqual(regimes[-2], "caution")
        self.assertEqual(regimes[-1], "risk")       # 第 N 日进入
        self.assertEqual(plan["ic_as_of"], "d059")

    def test_no_cache_graceful(self):
        plan = build_plan_from_cache(
            daily_loss=0.0, base_interval=3,
            cache_path=os.path.join(tempfile.mkdtemp(), "missing.json"))
        self.assertEqual(plan["regime"], "unknown")
        self.assertEqual(plan["exposure_mult"], 1.0)

    def test_disabled_returns_noop(self):
        old = factor_gate._Cfg.enabled
        try:
            factor_gate._Cfg.enabled = False
            plan = build_plan_from_cache(daily_loss=-0.01, base_interval=3)
            self.assertEqual(plan["regime"], "disabled")
            self.assertEqual(plan["exposure_mult"], 1.0)
            self.assertFalse(plan["freeze_new_buys"])
        finally:
            factor_gate._Cfg.enabled = old


class TestConfigFile(unittest.TestCase):
    def test_default_config_loaded(self):
        """data/factor_gate_config.json 应被读取 (新默认: exp_risk .9 / neg .6 / hyst 5)."""
        p = os.path.join(_BASE, "data", "factor_gate_config.json")
        if not os.path.exists(p):
            self.skipTest("无默认配置文件")
        self.assertTrue(_Cfg.cfg_file_loaded)
        self.assertEqual(_Cfg.exp_risk, 0.90)
        self.assertEqual(_Cfg.exp_caution, 0.95)
        self.assertEqual(_Cfg.ic_neg_share_risk, 0.60)
        self.assertEqual(_Cfg.hyst_enter, 5)
        self.assertEqual(_Cfg.hyst_exit, 5)


class TestEngineSourceSmoke(unittest.TestCase):
    """接线语法/钩子冒烟: 不做整模块 import."""

    def _engine_src(self) -> str:
        p = os.path.join(_BASE, "src", "realtime_engine.py")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_engine_compiles(self):
        compile(self._engine_src(), "realtime_engine.py", "exec")

    def test_has_gate_hook_in_source(self):
        src = self._engine_src()
        self.assertIn("def _compute_gate", src)
        self.assertIn("freeze_new_buys", src)
        self.assertIn("_exp_mult", src)
        self.assertIn("_gate_hyst", src)


if __name__ == "__main__":
    unittest.main()
