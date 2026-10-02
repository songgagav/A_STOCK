from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np


_SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "regime_route_report.py"
_SPEC = importlib.util.spec_from_file_location("regime_route_report", _SCRIPT)
assert _SPEC and _SPEC.loader
REPORT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(REPORT)


def test_spearman_and_decile_spread_are_positive_for_aligned_signal():
    values = np.arange(20, dtype=float)
    returns = values / 100.0
    assert np.isclose(REPORT.spearman_ic(values, returns), 1.0)
    assert REPORT.decile_spread(values, returns) > 0


def test_factor_day_metrics_excludes_non_finite_pairs():
    metric = REPORT.factor_day_metrics(
        {"a": 1.0, "b": np.nan, "c": 3.0},
        {"a": 0.1, "b": 0.2, "c": np.nan},
    )
    assert metric["n"] == 1
    assert metric["ic"] is None


def test_aggregate_metrics_does_not_treat_missing_ic_as_zero():
    out = REPORT.aggregate_metrics([
        {"n": 10, "ic": 0.2, "decile_spread": 0.1},
        {"n": 10, "ic": None, "decile_spread": None},
    ])
    assert out["n_days"] == 2
    assert out["n_ic"] == 1
    assert out["mean_ic"] == 0.2
    assert out["negative_ic_share"] == 0.0


def test_regime_series_uses_history_before_replay_window():
    returns = [(f"2026-08-{day:02d}", 0.002) for day in range(1, 22)]
    out = REPORT._regime_series(
        ["2026-08-21"], returns, trend_window=20, vol_window=20
    )
    assert out["2026-08-21"]["ok"] is True
    assert out["2026-08-21"]["regime"] == "bull"
