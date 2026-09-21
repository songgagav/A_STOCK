# -*- coding: utf-8 -*-
"""regime_costs 回归测试 (路线图 #9).

最关键的断言是 **normal 必须是恒等变换**: 一旦 normal 的倍率/延迟不是中性,
既有的单场景回测产物就会与今天的结果不可比, 而这件事不会报错 —— 只会让
"回测变好了"变成一句无法归因的话。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import regime_costs as RC  # noqa: E402


BASE_BUY = 0.00025 + 0.00001 + 0.0005 + 0.0002      # comm + transfer + slippage + impact
BASE_SELL = 0.00025 + 0.0005 + 0.00001 + 0.0005 + 0.0002


class TestScenarioResolution:
    def test_default_is_normal(self, monkeypatch):
        monkeypatch.delenv(RC.ENV_VAR, raising=False)
        assert RC.resolve_scenario()["name"] == "normal"
        assert RC.DEFAULT_SCENARIO == "normal"

    def test_env_var_selects_scenario(self, monkeypatch):
        monkeypatch.setenv(RC.ENV_VAR, "stress")
        assert RC.resolve_scenario()["name"] == "stress"

    def test_explicit_name_beats_env(self, monkeypatch):
        monkeypatch.setenv(RC.ENV_VAR, "stress")
        assert RC.resolve_scenario("normal")["name"] == "normal"

    def test_unknown_scenario_raises_not_falls_back(self):
        """未知场景名必须抛错。拼错场景名却拿到 normal 结果 = "压力测试通过了"
        其实根本没跑压力 —— 这是本类工具最危险的失效模式。"""
        with pytest.raises(KeyError):
            RC.resolve_scenario("stres")
        with pytest.raises(KeyError):
            RC.resolve_scenario("")

    def test_env_var_unknown_also_raises(self, monkeypatch):
        monkeypatch.setenv(RC.ENV_VAR, "nope")
        with pytest.raises(KeyError):
            RC.resolve_scenario()

    def test_case_and_space_insensitive(self):
        assert RC.resolve_scenario("  STRESS ")["name"] == "stress"

    def test_scenario_names_stable_and_sorted(self):
        assert RC.scenario_names() == ["normal", "stress"]

    def test_returned_scenario_carries_name(self):
        assert RC.resolve_scenario("stress")["name"] == "stress"

    def test_scenarios_dict_not_mutated_by_resolve(self):
        sc = RC.resolve_scenario("normal")
        sc["slippage_mult"] = 999
        assert RC.SCENARIOS["normal"]["slippage_mult"] == 1.0


class TestNormalIsIdentity:
    def test_normal_rates_unchanged(self):
        r = RC.project_rates(BASE_BUY, BASE_SELL, "normal")
        assert r["buy_rate"] == pytest.approx(BASE_BUY, abs=1e-15)
        assert r["sell_rate"] == pytest.approx(BASE_SELL, abs=1e-15)

    def test_normal_identity_holds_even_with_slippage_share(self):
        r = RC.project_rates(BASE_BUY, BASE_SELL, "normal", slippage_share=0.4)
        assert r["buy_rate"] == pytest.approx(BASE_BUY, abs=1e-15)

    def test_default_resolution_is_identity(self, monkeypatch):
        """不传场景 + 无环境变量 => 与基准逐位相同(历史产物可比)。"""
        monkeypatch.delenv(RC.ENV_VAR, raising=False)
        r = RC.project_rates(BASE_BUY, BASE_SELL, None)
        assert r["buy_rate"] == pytest.approx(BASE_BUY, abs=1e-15)
        assert r["latency_bars"] == 0

    def test_normal_latency_zero(self):
        assert RC.project_rates(BASE_BUY, BASE_SELL, "normal")["latency_bars"] == 0


class TestStressProjection:
    def test_stress_increases_both_rates(self):
        r = RC.project_rates(BASE_BUY, BASE_SELL, "stress")
        assert r["buy_rate"] > BASE_BUY
        assert r["sell_rate"] > BASE_SELL

    def test_stress_latency_is_one_bar(self):
        assert RC.project_rates(BASE_BUY, BASE_SELL, "stress")["latency_bars"] == 1

    def test_stress_multipliers_recorded(self):
        r = RC.project_rates(BASE_BUY, BASE_SELL, "stress")
        assert r["slippage_mult"] == 4.0
        assert r["fee_mult"] == 2.0

    def test_conservative_formula_matches_hand_calc(self):
        """无 slippage_share 时的保守近似: r*smul + r*(fmul-1) = r*(smul+fmul-1)。"""
        r = RC.project_rates(BASE_BUY, BASE_SELL, "stress")
        assert r["buy_rate"] == pytest.approx(BASE_BUY * (4.0 + 2.0 - 1.0), rel=1e-12)

    def test_slippage_share_only_scales_slippage_part(self):
        """给了 slippage_share 后, 费率 = r*(s*smul + (1-s)*fmul) —— 归因可分离。"""
        s = 0.5
        r = RC.project_rates(BASE_BUY, BASE_SELL, "stress", slippage_share=s)
        expect = BASE_BUY * (s * 4.0 + (1 - s) * 2.0)
        assert r["buy_rate"] == pytest.approx(expect, rel=1e-12)

    def test_slippage_share_one_gives_pure_slippage_mult(self):
        r = RC.project_rates(BASE_BUY, BASE_SELL, "stress", slippage_share=1.0)
        assert r["buy_rate"] == pytest.approx(BASE_BUY * 4.0, rel=1e-12)

    def test_slippage_share_zero_gives_pure_fee_mult(self):
        r = RC.project_rates(BASE_BUY, BASE_SELL, "stress", slippage_share=0.0)
        assert r["buy_rate"] == pytest.approx(BASE_BUY * 2.0, rel=1e-12)

    def test_slippage_share_clamped(self):
        over = RC.project_rates(BASE_BUY, BASE_SELL, "stress", slippage_share=5.0)
        at1 = RC.project_rates(BASE_BUY, BASE_SELL, "stress", slippage_share=1.0)
        assert over["buy_rate"] == pytest.approx(at1["buy_rate"], rel=1e-12)

    def test_stress_magnitude_is_physically_plausible(self):
        """压力滑点必须显著小于一个 A 股主板跌停板(10%), 否则这套假设已脱离
        物理可能 —— 那时"压力场景"变成"不可能场景", 数字再难看也没有信息量。

        量级核算: 基准滑点万5 = 0.05%, ×4 = 0.20% = 跌停板的 1/50。
        """
        slip = 0.0005                       # PAPER.slippage 基准(万5)
        proj = RC.project_rates(slip, slip, "stress", slippage_share=1.0)["buy_rate"]
        assert proj == pytest.approx(slip * 4.0, rel=1e-12)
        assert proj < 0.10 / 20.0, f"压力滑点 {proj:.4f} 已达跌停板 1/20 以上"


class TestDeltaAndDescribe:
    def test_cost_delta_bps_sign_and_value(self):
        r = RC.project_rates(BASE_BUY, BASE_SELL, "stress")
        assert RC.cost_delta_bps(BASE_BUY, r["buy_rate"]) > 0
        assert RC.cost_delta_bps(BASE_BUY, BASE_BUY) == 0.0

    def test_cost_delta_bps_known_value(self):
        assert RC.cost_delta_bps(0.001, 0.002) == pytest.approx(10.0, abs=1e-9)

    def test_describe_contains_name_and_numbers(self):
        d = RC.describe("stress")
        assert "stress" in d and "4.0" in d and "1bar" in d

    def test_describe_accepts_dict(self):
        assert "normal" in RC.describe(RC.resolve_scenario("normal"))


class TestEdgeInputs:
    def test_zero_rates_stay_zero(self):
        r = RC.project_rates(0.0, 0.0, "stress")
        assert r["buy_rate"] == 0.0 and r["sell_rate"] == 0.0

    def test_negative_rate_scales_linearly(self):
        r = RC.project_rates(-0.001, -0.001, "stress")
        assert r["buy_rate"] < 0

    def test_custom_dict_scenario_accepted(self):
        sc = {"name": "custom", "slippage_mult": 3.0, "fee_mult": 1.0, "latency_bars": 2}
        r = RC.project_rates(BASE_BUY, BASE_SELL, sc)
        assert r["scenario"] == "custom" and r["latency_bars"] == 2

    def test_long_short_aliases_equal_buy_sell(self):
        r = RC.project_rates(BASE_BUY, BASE_SELL, "stress")
        assert r["long_rate"] == r["buy_rate"]
        assert r["short_rate"] == r["sell_rate"]

    def test_none_scenario_with_env(self, monkeypatch):
        monkeypatch.setenv(RC.ENV_VAR, "stress")
        r = RC.project_rates(BASE_BUY, BASE_SELL, None)
        assert r["buy_rate"] > BASE_BUY
