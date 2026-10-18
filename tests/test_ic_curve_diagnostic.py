from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from ic_curve_diagnostic import (
    DEFAULT_HORIZONS,
    FACTOR_NAMES,
    build_ic_curves,
    compute_factor_scores,
    compute_forward_labels,
)


def _calendar(start: str, count: int) -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _bars(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["day", "symbol", "close", "change_pct"])


def _factor_score(scores: pd.DataFrame, day: str, factor: str, symbol: str) -> float:
    row = scores.loc[
        (scores.day == day.replace("-", ""))
        & (scores.factor == factor)
        & (scores.symbol == symbol),
        "score",
    ]
    assert len(row) == 1
    return float(row.iloc[0])


def test_factor_window_never_reads_after_signal_day():
    days = _calendar("2026-05-01", 24)
    rows = [(day, "A", 10.0 + i * 0.03, 0.0) for i, day in enumerate(days[:21])]
    baseline, _ = compute_factor_scores(_bars(rows), [days[20]])
    changed_future = _bars(rows + [(days[21], "A", 1_000_000.0, 0.0)])
    changed, _ = compute_factor_scores(changed_future, [days[20]])

    pd.testing.assert_frame_equal(baseline, changed)


def test_present_day_universe_change_does_not_rewrite_historical_scores():
    days = _calendar("2026-05-01", 25)
    historical = [(day, "A", 12.0 + i * 0.02, 0.0) for i, day in enumerate(days[:21])]
    baseline, _ = compute_factor_scores(_bars(historical), [days[20]])
    present_only = [
        (day, "NEW_SYMBOL", 8.0 + i * 0.1, 0.0)
        for i, day in enumerate(days[21:])
    ]
    changed, _ = compute_factor_scores(_bars(historical + present_only), [days[20]])

    pd.testing.assert_frame_equal(baseline, changed)
    assert "NEW_SYMBOL" not in set(changed.symbol)


def test_invalid_or_short_history_is_counted_not_neutral():
    days = _calendar("2026-05-01", 21)
    rows: list[tuple[str, str, float, float]] = []
    rows.extend((day, "GOOD", 10.0 + i * 0.01, 0.0) for i, day in enumerate(days))
    rows.extend((day, "SHORT", 10.0 + i * 0.01, 0.0) for i, day in enumerate(days[1:], 1))
    rows.extend(
        (day, "BAD_HISTORY", (0.0 if i == 0 else 10.0 + i * 0.01), 0.0)
        for i, day in enumerate(days)
    )
    rows.extend((day, "BAD_SIGNAL", (0.0 if i == 20 else 10.0 + i * 0.01), 0.0)
                for i, day in enumerate(days))

    scores, missing = compute_factor_scores(_bars(rows), [days[-1]])

    assert set(scores.symbol) == {"GOOD"}
    assert missing[days[-1].replace("-", "")]["insufficient_history"] == 2
    assert missing[days[-1].replace("-", "")]["invalid_signal_close"] == 1
    assert not scores.score.eq(0.5).any()


def test_lower_vol_and_lower_momentum_map_to_higher_named_scores():
    days = _calendar("2026-05-01", 23)
    rows: list[tuple[str, str, float, float]] = []
    for i, day in enumerate(days):
        rows.extend(
            [
                (day, "LOW_VOL", 100.0 + i + (0.05 if i % 2 else -0.05), 0.0),
                (day, "HIGH_VOL", 100.0 + i + (4.0 if i % 2 else -4.0), 0.0),
                (day, "DOWN", 100.0 - i * 0.7, 0.0),
                (day, "UP", 100.0 + i * 0.7, 0.0),
            ]
        )

    scores, _ = compute_factor_scores(_bars(rows), [days[-1]])
    vol = "vol_selector_score_v1"
    mom = "mom_rev_selector_score_v1"

    assert _factor_score(scores, days[-1], vol, "LOW_VOL") > _factor_score(scores, days[-1], vol, "HIGH_VOL")
    assert _factor_score(scores, days[-1], mom, "DOWN") > _factor_score(scores, days[-1], mom, "UP")
    assert set(scores.factor) == set(FACTOR_NAMES)
    assert DEFAULT_HORIZONS == (1, 3, 5, 10, 20)


def test_forward_label_compounds_exactly_next_h_rows():
    days = _calendar("2026-06-01", 4)
    bars = _bars(
        [
            (days[0], "A", 10.0, 99.0),  # t's own return must not enter the label
            (days[1], "A", 11.0, 10.0),
            (days[2], "A", 9.9, -10.0),
            (days[3], "A", 10.395, 5.0),
        ]
    )

    labels, _ = compute_forward_labels(bars, days, [days[0]], days[3], horizons=(1, 3))
    h1 = labels.loc[labels.horizon == 1].iloc[0]
    h3 = labels.loc[labels.horizon == 3].iloc[0]

    assert h1.valid and h1.forward_return == pytest.approx(0.10)
    assert h3.valid and h3.forward_return == pytest.approx(1.10 * 0.90 * 1.05 - 1.0)


def test_missing_or_invalid_future_row_is_not_filled():
    days = _calendar("2026-06-01", 4)
    bars = _bars(
        [
            (days[0], "MISSING", 10.0, 0.0),
            (days[1], "MISSING", 11.0, 1.0),
            (days[3], "MISSING", 12.0, 1.0),  # missing the exact middle due day
            (days[0], "INVALID", 10.0, 0.0),
            (days[1], "INVALID", 11.0, 1.0),
            (days[2], "INVALID", 11.0, -100.0),
            (days[3], "INVALID", 12.0, 1.0),
            (days[0], "NONFINITE", 10.0, 0.0),
            (days[1], "NONFINITE", 11.0, 1.0),
            (days[2], "NONFINITE", 11.0, np.nan),
            (days[3], "NONFINITE", 12.0, 1.0),
        ]
    )

    labels, missing = compute_forward_labels(bars, days, [days[0]], days[3], horizons=(3,))

    assert set(labels.reason) == {"missing_future_bar", "invalid_change_pct"}
    assert labels.forward_return.isna().all()
    assert missing[days[0].replace("-", "")]["h3:missing_future_bar"] == 1
    assert missing[days[0].replace("-", "")]["h3:invalid_change_pct"] == 2


def test_horizon_maturity_requires_source_watermark():
    days = _calendar("2026-06-01", 5)
    bars = _bars(
        [
            (days[0], "A", 10.0, 0.0),
            (days[1], "A", 11.0, 1.0),
            (days[2], "A", 12.0, 1.0),
        ]
    )

    labels, _ = compute_forward_labels(bars, days, [days[0]], days[2], horizons=(1, 3))
    h1 = labels.loc[labels.horizon == 1].iloc[0]
    h3 = labels.loc[labels.horizon == 3].iloc[0]

    assert h1.valid and h1.reason == ""
    assert not h3.valid and pd.isna(h3.forward_return) and h3.reason == "not_mature"


def test_n_forward_valid_counts_only_complete_labels():
    days = _calendar("2026-06-01", 2)
    rows: list[tuple[str, str, float, float]] = []
    for index in range(5):
        symbol = f"S{index}"
        rows.append((days[0], symbol, 10.0 + index, 0.0))
        if index < 4:
            rows.append((days[1], symbol, 11.0 + index, 1.0))
    bars = _bars(rows)
    labels, _ = compute_forward_labels(bars, days, [days[0]], days[1], horizons=(1,))
    scores = pd.DataFrame(
        [
            {"day": days[0].replace("-", ""), "symbol": f"S{i}", "factor": FACTOR_NAMES[0], "score": float(i)}
            for i in range(5)
        ]
    )

    curve = build_ic_curves(scores, labels, [days[0]], horizons=(1,))[FACTOR_NAMES[0]].iloc[0]

    assert int(curve.n_forward_valid) == 4
    assert int(curve.n_missing_forward) == 1
    assert int(curve.n_factor) == 5
    assert int(curve.n_pairs) == 4


def test_spearman_uses_average_tie_ranks():
    day = "20260601"
    symbols = [f"S{i}" for i in range(5)]
    scores = pd.DataFrame(
        [
            {"day": day, "symbol": symbol, "factor": FACTOR_NAMES[0], "score": value}
            for symbol, value in zip(symbols, [1.0, 1.0, 2.0, 3.0, 4.0])
        ]
    )
    labels = pd.DataFrame(
        [
            {"day": day, "horizon": 1, "symbol": symbol, "forward_return": i / 100,
             "valid": True, "reason": ""}
            for i, symbol in enumerate(symbols)
        ]
    )

    ic = build_ic_curves(scores, labels, [day], horizons=(1,))[FACTOR_NAMES[0]].iloc[0].ic

    assert ic == pytest.approx(np.sqrt(0.95))


def test_insufficient_pairs_or_constant_rank_has_null_ic():
    day = "20260601"
    symbols = [f"S{i}" for i in range(5)]
    scores = pd.DataFrame(
        [
            {"day": day, "symbol": f"S{i}", "factor": FACTOR_NAMES[0], "score": float(i)}
            for i in range(4)
        ]
        + [
            {"day": day, "symbol": symbol, "factor": FACTOR_NAMES[1], "score": 0.7}
            for symbol in symbols
        ]
    )
    labels = pd.DataFrame(
        [
            {"day": day, "horizon": 1, "symbol": symbol, "forward_return": i / 100,
             "valid": True, "reason": ""}
            for i, symbol in enumerate(symbols)
        ]
    )

    curves = build_ic_curves(scores, labels, [day], horizons=(1,))

    assert curves[FACTOR_NAMES[0]].iloc[0].status == "insufficient_pairs"
    assert pd.isna(curves[FACTOR_NAMES[0]].iloc[0].ic)
    assert curves[FACTOR_NAMES[1]].iloc[0].status == "constant_rank"
    assert pd.isna(curves[FACTOR_NAMES[1]].iloc[0].ic)
