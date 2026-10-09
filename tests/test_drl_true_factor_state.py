# -*- coding: utf-8 -*-
"""Historical fixture validation for the production PIT DRL state loader."""

from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np
import pandas as pd

import drl_train


class _FixtureStore:
    frame = None

    def __init__(self, *args, **kwargs):
        pass

    def closes_window(self, start, end, **kwargs):
        return type(self).frame.copy()

    def close(self):
        return None


def test_true_factor_state_uses_pit_snapshots_and_excludes_immature_labels(
    tmp_path, monkeypatch
):
    cutoff = dt.date(2026, 2, 1)
    days = [cutoff - dt.timedelta(days=29 - i) for i in range(30)]
    symbols = [f"{i:06d}" for i in range(1, 11)]

    rows = []
    for i, day in enumerate(days):
        for j, symbol in enumerate(symbols):
            # Higher j has a higher five-day forward return, creating a known
            # positive cross-sectional Rank IC for every factor.
            close = 100.0 + j + (i * 0.01 * (j + 1))
            rows.append((day.isoformat(), symbol, close))
    _FixtureStore.frame = pd.DataFrame(rows, columns=["d", "symbol", "close"])
    monkeypatch.setattr("h5i_bar_store.H5iBarStore", _FixtureStore)
    monkeypatch.setattr(drl_train, "DATA_DIR", str(tmp_path))

    for i, day in enumerate(days):
        day_dir = tmp_path / "daily" / day.strftime("%Y%m%d")
        day_dir.mkdir(parents=True)
        pool = [
            {
                "canon": symbol,
                **{
                    name: float(j + i * 0.001)
                    for name in drl_train.SCORE_FACTORS
                },
            }
            for j, symbol in enumerate(symbols)
        ]
        (day_dir / "selection.json").write_text(
            json.dumps({"pool_snapshot": pool}), encoding="utf-8"
        )

    ic, returns, dates = drl_train._load_true_factor_state(cutoff, lookback_days=60)

    # The last five snapshot rows have no fifth subsequent bar and are
    # excluded; five more rows are consumed by state maturity alignment.
    assert ic.shape == (20, len(drl_train.SCORE_FACTORS))
    assert len(returns) == len(dates) == 20
    assert dates[0] == days[5].isoformat()
    assert np.all(np.isfinite(ic))
    assert np.all(ic > 0.99)
    assert np.all(np.isfinite(returns))
