# -*- coding: utf-8 -*-
"""Deterministic market-regime detection and factor routing.

This is the first, rule-based P0 step of the regime-aware evolution.  It is
deliberately pure and dependency-light so it can be used in shadow research
before being enabled in live selection.
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np


REGIMES = ("bull", "bear", "sideways", "high_vol", "unknown")

# Conservative, inspectable priors.  A missing factor keeps multiplier 1.0.
DEFAULT_FACTOR_MULTIPLIERS: dict[str, dict[str, float]] = {
    "bull": {
        "pb_inv": 0.90, "ep": 0.95, "ocf_ps": 1.05,
        "roe_yy_chg": 1.10, "gp4": 1.25,
    },
    "bear": {
        "pb_inv": 1.20, "ep": 1.15, "ocf_ps": 1.10,
        "roe_yy_chg": 1.05, "gp4": 0.80,
    },
    "sideways": {},
    "high_vol": {
        "pb_inv": 1.05, "ep": 1.00, "ocf_ps": 1.20,
        "roe_yy_chg": 1.10, "gp4": 0.75,
    },
}


def _finite_returns(returns: Iterable[float] | np.ndarray) -> np.ndarray:
    arr = np.asarray(list(returns) if not isinstance(returns, np.ndarray) else returns,
                     dtype=float).reshape(-1)
    return arr[np.isfinite(arr)]


def classify_market_regime(
    returns: Iterable[float] | np.ndarray,
    *,
    trend_window: int = 20,
    vol_window: int = 20,
    bull_threshold: float = 0.02,
    bear_threshold: float = -0.02,
    high_vol_threshold: float = 0.30,
    min_observations: int = 20,
) -> dict:
    """Classify recent decimal daily returns into four market states.

    High volatility takes precedence over trend so a fast sell-off is not
    mislabeled as an ordinary bear regime.  Insufficient or unusable data is
    explicit ``unknown`` and never silently treated as a healthy regime.
    """
    trend_window = max(2, int(trend_window))
    vol_window = max(2, int(vol_window))
    min_observations = max(2, int(min_observations))
    arr = _finite_returns(returns)
    if len(arr) < min_observations:
        return {
            "ok": False,
            "regime": "unknown",
            "n_observations": int(len(arr)),
            "trend_return": None,
            "annualized_vol": None,
            "reason": f"有效收益观测不足 ({len(arr)} < {min_observations})",
        }

    trend = arr[-trend_window:]
    vol = arr[-vol_window:]
    growth = np.prod(1.0 + trend)
    trend_return = float(growth - 1.0) if np.isfinite(growth) else 0.0
    annualized_vol = float(np.std(vol, ddof=1) * math.sqrt(252.0))
    if not np.isfinite(annualized_vol):
        annualized_vol = 0.0

    if annualized_vol >= float(high_vol_threshold):
        regime = "high_vol"
    elif trend_return >= float(bull_threshold):
        regime = "bull"
    elif trend_return <= float(bear_threshold):
        regime = "bear"
    else:
        regime = "sideways"
    return {
        "ok": True,
        "regime": regime,
        "n_observations": int(len(arr)),
        "trend_return": trend_return,
        "annualized_vol": annualized_vol,
        "trend_window": trend_window,
        "vol_window": vol_window,
    }


def route_factor_weights(
    weights: dict[str, float],
    regime: str,
    *,
    multipliers: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    """Apply a transparent regime prior and renormalize factor weights.

    Unknown regimes are a no-op after safe normalization.  Inputs are not
    mutated, negative/non-finite weights are treated as unavailable, and an
    all-invalid input falls back to equal weights.
    """
    clean: dict[str, float] = {}
    for name, value in (weights or {}).items():
        try:
            val = float(value)
        except (TypeError, ValueError):
            val = 0.0
        clean[str(name)] = val if np.isfinite(val) and val > 0 else 0.0
    if not clean:
        return {}
    total = sum(clean.values())
    if total <= 0:
        equal = 1.0 / len(clean)
        clean = {name: equal for name in clean}
    else:
        clean = {name: value / total for name, value in clean.items()}

    profile = (multipliers or DEFAULT_FACTOR_MULTIPLIERS).get(regime, {})
    routed = {name: value * max(0.0, float(profile.get(name, 1.0)))
              for name, value in clean.items()}
    routed_total = sum(routed.values())
    if routed_total <= 0:
        return clean
    return {name: value / routed_total for name, value in routed.items()}


__all__ = [
    "DEFAULT_FACTOR_MULTIPLIERS",
    "REGIMES",
    "classify_market_regime",
    "route_factor_weights",
]
