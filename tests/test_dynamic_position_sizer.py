from __future__ import annotations

import numpy as np

from risk_first import DynamicPositionSizer, RiskFirstLayer


def test_volatility_target_reduces_exposure_and_keeps_cash():
    sizer = DynamicPositionSizer(target_vol=0.15)
    sized, info = sizer.apply([0.4, 0.6], annualized_vol=0.30, regime="sideways")
    assert np.isclose(info["scale"], 0.45)
    assert np.isclose(sized.sum(), 0.45)


def test_non_positive_expected_return_blocks_new_exposure():
    info = DynamicPositionSizer().size(0.20, expected_return=-0.01, regime="bull")
    assert info["scale"] == 0.0
    assert any("预期收益非正" in reason for reason in info["reasons"])


def test_risk_first_exposes_sizer_without_changing_existing_limits():
    layer = RiskFirstLayer()
    weights, info = layer.apply_dynamic_position_size(
        [0.5, 0.5], annualized_vol=0.15, regime="high_vol")
    assert np.isclose(weights.sum(), 0.5)
    assert info["regime"] == "high_vol"
    assert layer.state_dict()["position_sizer"]["target_vol"] == 0.15
