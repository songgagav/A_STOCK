# -*- coding: utf-8 -*-
"""h5i-first IC curve input contracts."""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import h5i_bar_store  # noqa: E402
import ic_backtest  # noqa: E402
import ic_curve_refresh  # noqa: E402


class _FakeH5i:
    def __init__(self, frame=None, days=None):
        self.frame = frame
        self.days = days or []

    def closes_window(self, start, end, positive_close_only=True):
        assert (start, end) == ("2026-09-29", "2026-09-30")
        assert positive_close_only is True
        return self.frame.copy()

    def trading_days(self):
        return list(self.days)

    def close(self):
        pass


def test_unadjusted_ic_input_prefers_h5i_and_normalizes_percent_change(monkeypatch):
    frame = pd.DataFrame(
        {
            "d": ["2026-09-29", "2026-09-30"],
            "symbol": ["000001", "000001"],
            "close": [10.0, 10.2],
            "change_pct": [1.5, -2.0],
        }
    )
    monkeypatch.setenv("BAR_STORE", "h5i")
    monkeypatch.setattr(h5i_bar_store, "H5iBarStore", lambda: _FakeH5i(frame=frame))

    pct = ic_backtest.load_pct("2026-09-29", "2026-09-30", use_adj=False)

    assert isinstance(pct.index, pd.DatetimeIndex)
    assert list(pct.index) == [pd.Timestamp("2026-09-29"), pd.Timestamp("2026-09-30")]
    assert list(pct.columns) == ["000001"]
    assert pct.loc[pd.Timestamp("2026-09-29"), "000001"] == 0.015
    assert pct.loc[pd.Timestamp("2026-09-30"), "000001"] == -0.02


def test_refresh_window_discovery_prefers_h5i_trade_days(monkeypatch):
    days = ["2026-09-26", "2026-09-29", "2026-09-30"]
    monkeypatch.setenv("BAR_STORE", "h5i")
    monkeypatch.setattr(h5i_bar_store, "H5iBarStore", lambda: _FakeH5i(days=days))

    assert ic_curve_refresh._latest_settled_day() == "2026-09-30"
    assert ic_curve_refresh._window_start("2026-09-30", days=2) == "2026-09-29"


def test_build_factor_uses_current_wide_return_contract():
    returns = pd.DataFrame(
        {"000001": [float(i) / 100.0 for i in range(1, 22)]},
        index=[f"2026-09-{i:02d}" for i in range(1, 22)],
    )

    momentum = ic_backtest.build_factor(returns, 20, "mom_20")
    reversal = ic_backtest.build_factor(returns, 20, "reversal")
    volatility = ic_backtest.build_factor(returns, 20, "vol")

    expected_mean = sum(float(i) / 100.0 for i in range(2, 22)) / 20.0
    assert momentum.loc["2026-09-21", "000001"] == pytest.approx(expected_mean)
    assert reversal.loc["2026-09-21", "000001"] == pytest.approx(-expected_mean)
    assert volatility.loc["2026-09-21", "000001"] > 0


def test_full_refresh_accepts_h5i_string_latest_day(monkeypatch, tmp_path):
    calls = []

    def fake_run(start, end, k, holds, factor, use_adj):
        calls.append((start, end, k, tuple(holds), factor, use_adj))
        return ({hold: pd.Series(dtype=float) for hold in holds}, "unused.csv")

    monkeypatch.setattr(ic_curve_refresh, "_latest_settled_day", lambda: "2026-09-30")
    monkeypatch.setattr(ic_curve_refresh.ic_backtest, "run", fake_run)
    monkeypatch.setattr(ic_curve_refresh, "IC_DIR", str(tmp_path))

    result = ic_curve_refresh.refresh(factor_names=["vol"], full=True)

    assert result["ok"] is True
    assert calls == [("2013-01-01", "2026-09-30", 20, (1, 3, 5, 10, 20), "vol", False)]
