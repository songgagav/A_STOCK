from __future__ import annotations

import numpy as np

from regime_detector import classify_market_regime, route_factor_weights


def test_insufficient_returns_are_explicitly_unknown():
    out = classify_market_regime([0.01] * 5)
    assert out["ok"] is False
    assert out["regime"] == "unknown"


def test_bull_and_bear_states_follow_trend():
    bull = classify_market_regime([0.003] * 30, high_vol_threshold=1.0)
    bear = classify_market_regime([-0.003] * 30, high_vol_threshold=1.0)
    assert bull["regime"] == "bull"
    assert bear["regime"] == "bear"


def test_high_vol_takes_precedence_over_trend():
    returns = [0.04, -0.04] * 15
    out = classify_market_regime(returns, high_vol_threshold=0.20)
    assert out["regime"] == "high_vol"
    assert out["annualized_vol"] > 0.20


def test_factor_route_is_normalized_and_does_not_mutate_input():
    weights = {"pb_inv": 0.25, "ep": 0.25, "ocf_ps": 0.25, "gp4": 0.25}
    routed = route_factor_weights(weights, "bull")
    assert weights["gp4"] == 0.25
    assert np.isclose(sum(routed.values()), 1.0)
    assert routed["gp4"] > routed["pb_inv"]


def test_unknown_regime_is_a_safe_normalized_noop():
    routed = route_factor_weights({"a": 2.0, "b": 1.0}, "unknown")
    assert np.isclose(routed["a"], 2.0 / 3.0)
    assert np.isclose(routed["b"], 1.0 / 3.0)
