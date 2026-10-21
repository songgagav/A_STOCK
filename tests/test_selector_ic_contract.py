from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest


def _api():
    """Fail as an assertion, not a collection error, until the module exists."""
    import importlib.util

    spec = importlib.util.find_spec("selector_ic_contract")
    assert spec is not None, "diagnostic PIT contract module is not implemented"
    import selector_ic_contract

    return selector_ic_contract


def _require(name):
    api = _api()
    assert hasattr(api, name), f"diagnostic PIT contract is missing {name}"
    return getattr(api, name)


def test_signal_cutoff_is_0925_shanghai_and_keeps_calendar_day():
    api = _api()

    got = api.signal_cutoff("2026-10-21")

    assert got == datetime(2026, 10, 21, 9, 25, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert api.signal_cutoff(date(2026, 10, 21)).time() == time(9, 25)


def _weekdays(start: date, count: int) -> list[str]:
    out = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def test_previous_session_is_strictly_before_signal_date():
    previous_session = _require("previous_session")
    sessions = ["2026-10-19", "2026-10-20", "2026-10-21"]

    assert previous_session("2026-10-21", sessions) == "2026-10-20"


@pytest.mark.parametrize("horizon", [1, 3, 5, 20])
def test_forward_return_uses_signal_day_open_and_horizon_close(horizon):
    forward = _require("forward_return_for_symbol")
    sessions = _weekdays(date(2026, 1, 5), horizon)
    bars = {
        (day, "600000.SH"): {
            "open": 10.0,
            "close": 11.0,
            "change_pct": 10.0,
        }
        for day in sessions
    }

    result = forward(bars, "600000.SH", sessions[0], horizon, sessions)

    assert result.status == "ok"
    assert result.settlement_date == sessions[-1]
    assert result.value == pytest.approx(1.1**horizon - 1.0)


def test_forward_return_fails_closed_when_an_intermediate_session_bar_is_missing():
    forward = _require("forward_return_for_symbol")
    sessions = ["2026-10-19", "2026-10-20", "2026-10-21"]
    bars = {
        ("2026-10-19", "600000.SH"): {"open": 10.0, "close": 11.0},
        ("2026-10-21", "600000.SH"): {"open": 12.0, "close": 13.0},
    }

    result = forward(bars, "600000.SH", "2026-10-19", 3, sessions)

    assert result.status == "incomplete"
    assert result.value is None
    assert result.reason == "missing_bar:2026-10-20"


def test_forward_return_reports_immature_horizon_without_shortening_it():
    forward = _require("forward_return_for_symbol")
    sessions = ["2026-10-19", "2026-10-20"]
    bars = {("2026-10-19", "600000.SH"): {"open": 10.0, "close": 11.0}}

    result = forward(bars, "600000.SH", "2026-10-19", 3, sessions)

    assert result.status == "not_mature"
    assert result.value is None


def test_volatility_signal_uses_twenty_returns_sample_std_and_selector_clip():
    volatility_signal = _require("volatility_signal")
    returns = [0.02] * 19 + [0.08]
    closes = [100.0]
    for ret in returns:
        closes.append(closes[-1] * (1.0 + ret))
    mean = sum(returns) / 20
    expected_ann = (sum((ret - mean) ** 2 for ret in returns) / 19) ** 0.5 * 252**0.5
    expected_score = 1.0 - (expected_ann - 0.10) / (0.60 - 0.10)

    result = volatility_signal(closes)

    assert result.status == "ok"
    assert result.raw_value == pytest.approx(expected_ann)
    assert result.value == pytest.approx(expected_score)


def test_momentum_signal_stores_raw_mom20_and_keeps_reverse_score_separate():
    momentum_signal = _require("momentum_signal")
    closes = [100.0] * 20 + [110.0]

    result = momentum_signal(closes)

    assert result.status == "ok"
    assert result.value == pytest.approx(0.10)
    assert result.audit_value == pytest.approx(0.25)


def test_pb_reverse_score_is_inverse_clipped_and_missing_is_not_neutral():
    pb_rev_signal = _require("pb_rev_signal")

    assert pb_rev_signal(0.5).value == 1.0
    assert pb_rev_signal(3.0).value == 0.0
    assert pb_rev_signal(-0.1).value == 1.0
    missing = pb_rev_signal(None)
    assert missing.status == "missing"
    assert missing.value is None


def test_rank_ic_uses_average_ranks_for_ties():
    rank_ic = _require("rank_ic")

    result = rank_ic(
        {"a": 1, "b": 1, "c": 2, "d": 3, "e": 4},
        {"a": 5, "b": 4, "c": 3, "d": 2, "e": 1},
    )

    assert result.status == "ok"
    assert result.pair_count == 5
    assert result.ic == pytest.approx(-0.9746794344808963)


def test_rank_ic_requires_five_finite_pairs_and_excludes_nonfinite_inputs():
    rank_ic = _require("rank_ic")

    too_small = rank_ic({"a": 1, "b": 2, "c": 3, "d": 4},
                        {"a": 4, "b": 3, "c": 2, "d": 1})
    assert too_small.status == "insufficient_pairs"
    assert too_small.ic is None
    assert too_small.pair_count == 4

    enough = rank_ic(
        {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "bad": float("inf")},
        {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "bad": 6},
    )
    assert enough.status == "ok"
    assert enough.pair_count == 5
    assert enough.ic == pytest.approx(1.0)


def test_rank_ic_marks_constant_rank_null():
    rank_ic = _require("rank_ic")

    result = rank_ic({symbol: 1.0 for symbol in "abcde"},
                     {symbol: float(i) for i, symbol in enumerate("abcde")})

    assert result.status == "constant_rank"
    assert result.ic is None


def test_mom_rev_direction_is_applied_once_to_raw_mom20_ic():
    rank_ic = _require("rank_ic")
    apply_expected_direction = _require("apply_expected_direction")
    raw = rank_ic({"a": 1, "b": 2, "c": 3, "d": 4, "e": 5},
                  {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5})

    assert raw.ic == pytest.approx(1.0)
    assert apply_expected_direction("mom_rev", raw.ic) == pytest.approx(-1.0)
    assert apply_expected_direction("vol", raw.ic) == pytest.approx(1.0)

