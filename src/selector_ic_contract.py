"""Pure, diagnostic-only PIT selector-factor IC contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import math
import statistics
from types import MappingProxyType
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
HORIZONS = (1, 3, 5, 10, 20)
LABEL_VERSION = "0925_open_to_close_v1"


@dataclass(frozen=True)
class FactorContract:
    key: str
    expected_direction: int
    stored_orientation: str
    source_status: str
    unsupported_reason: str | None = None


FACTOR_CONTRACTS = MappingProxyType({
    "vol": FactorContract("vol", 1, "selector_low_vol_score", "candidate"),
    "mom_rev": FactorContract("mom_rev", -1, "raw_mom_20", "candidate"),
    "pb_rev": FactorContract("pb_rev", 1, "selector_low_pb_score", "candidate"),
    "roe": FactorContract("roe", 1, "raw_roe", "unsupported",
                           "announcement_time_pit_unproven"),
    "mf_net": FactorContract("mf_net", 1, "raw_net_flow_ratio", "unsupported",
                              "stock_level_pit_source_absent"),
})


def signal_cutoff(day: str | date) -> datetime:
    """Return the approved 09:25 Shanghai decision cutoff for a signal day."""
    if isinstance(day, str):
        try:
            parsed = date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError("day must be an ISO date (YYYY-MM-DD)") from exc
    elif isinstance(day, date):
        parsed = day
    else:
        raise TypeError("day must be an ISO date string or datetime.date")
    return datetime.combine(parsed, time(9, 25), tzinfo=SHANGHAI)


def _iso_day(value: str | date) -> str:
    if isinstance(value, datetime):
        raise TypeError("session day cannot include a time")
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("session days must be ISO dates (YYYY-MM-DD)") from exc


def _ordered_sessions(sessions: Sequence[str | date]) -> list[str]:
    normalized = [_iso_day(day) for day in sessions]
    if len(normalized) != len(set(normalized)):
        raise ValueError("calendar sessions must be unique")
    return sorted(normalized)


def previous_session(day: str | date, sessions: Sequence[str | date]) -> str:
    """Return the latest calendar session strictly before ``day``."""
    signal_day = _iso_day(day)
    prior = [session for session in _ordered_sessions(sessions) if session < signal_day]
    if not prior:
        raise ValueError(f"no prior session for {signal_day}")
    return prior[-1]


def horizon_sessions(day: str | date, horizon: int,
                     sessions: Sequence[str | date]) -> list[str] | None:
    """Return [signal day, ..., exit day] or None until all sessions mature."""
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise ValueError("horizon must be a positive integer")
    signal_day = _iso_day(day)
    ordered = _ordered_sessions(sessions)
    try:
        start = ordered.index(signal_day)
    except ValueError as exc:
        raise ValueError(f"signal day is not a calendar session: {signal_day}") from exc
    end = start + horizon
    if end > len(ordered):
        return None
    return ordered[start:end]


@dataclass(frozen=True)
class LabelResult:
    value: float | None
    status: str
    reason: str | None
    settlement_date: str | None


@dataclass(frozen=True)
class FactorValue:
    value: float | None
    status: str
    reason: str | None = None
    raw_value: float | None = None
    audit_value: float | None = None


@dataclass(frozen=True)
class ICResult:
    ic: float | None
    status: str
    pair_count: int


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip_score(value: float, low: float, high: float, invert: bool = False) -> float:
    normalized = min(1.0, max(0.0, (value - low) / (high - low)))
    return 1.0 - normalized if invert else normalized


def _recent_closes(closes: Sequence[object]) -> tuple[list[float] | None, str | None]:
    if len(closes) < 21:
        return None, "insufficient_history"
    recent = []
    for raw in closes[-21:]:
        value = _finite_number(raw)
        if value is None or value <= 0:
            return None, "invalid_close"
        recent.append(value)
    return recent, None


def volatility_signal(closes: Sequence[object]) -> FactorValue:
    """Return selector-facing low-vol score and raw annualized volatility."""
    recent, error = _recent_closes(closes)
    if error:
        return FactorValue(None, error, error)
    assert recent is not None
    returns = [current / previous - 1.0 for previous, current in zip(recent, recent[1:])]
    annualized = statistics.stdev(returns) * math.sqrt(252.0)
    return FactorValue(
        _clip_score(annualized, 0.10, 0.60, invert=True),
        "ok",
        raw_value=annualized,
    )


def momentum_signal(closes: Sequence[object]) -> FactorValue:
    """Return raw 20-session return for the optimizer's explicit -1 direction."""
    recent, error = _recent_closes(closes)
    if error:
        return FactorValue(None, error, error)
    assert recent is not None
    raw_mom20 = recent[-1] / recent[0] - 1.0
    reverse_score = _clip_score(raw_mom20, -0.20, 0.20, invert=True)
    return FactorValue(raw_mom20, "ok", raw_value=raw_mom20, audit_value=reverse_score)


def pb_rev_signal(pb: object) -> FactorValue:
    """Return the live inverse-clipped PB score; missing data is never neutralized."""
    value = _finite_number(pb)
    if pb is None:
        return FactorValue(None, "missing", "missing_pb")
    if value is None:
        return FactorValue(None, "invalid", "non_finite_or_non_numeric_pb")
    score = _clip_score(value, 0.5, 3.0, invert=True)
    return FactorValue(score, "ok", raw_value=value)


def _average_ranks(values: Mapping[str, float]) -> dict[str, float]:
    ordered = sorted(values, key=lambda key: (values[key], key))
    ranks: dict[str, float] = {}
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        average = ((start + 1) + end) / 2.0
        for key in ordered[start:end]:
            ranks[key] = average
        start = end
    return ranks


def rank_ic(signal_values: Mapping[str, object], forward_returns: Mapping[str, object],
            min_pairs: int = 5) -> ICResult:
    """Calculate finite-pair Spearman IC using average ranks for ties."""
    if isinstance(min_pairs, bool) or not isinstance(min_pairs, int) or min_pairs < 5:
        raise ValueError("min_pairs must be an integer >= 5")
    x: dict[str, float] = {}
    y: dict[str, float] = {}
    for symbol in signal_values.keys() & forward_returns.keys():
        xv = _finite_number(signal_values[symbol])
        yv = _finite_number(forward_returns[symbol])
        if xv is not None and yv is not None:
            x[symbol] = xv
            y[symbol] = yv
    pairs = len(x)
    if pairs < min_pairs:
        return ICResult(None, "insufficient_pairs", pairs)

    rx = _average_ranks(x)
    ry = _average_ranks(y)
    mean_x = sum(rx.values()) / pairs
    mean_y = sum(ry.values()) / pairs
    centered_x = [rx[symbol] - mean_x for symbol in rx]
    centered_y = [ry[symbol] - mean_y for symbol in rx]
    sum_x = sum(value * value for value in centered_x)
    sum_y = sum(value * value for value in centered_y)
    if sum_x == 0.0 or sum_y == 0.0:
        return ICResult(None, "constant_rank", pairs)
    covariance = sum(a * b for a, b in zip(centered_x, centered_y))
    ic = covariance / math.sqrt(sum_x * sum_y)
    return ICResult(ic, "ok", pairs)


def apply_expected_direction(factor_key: str, ic: float | None) -> float | None:
    """Apply the declared consumer direction once to a raw/stored-orientation IC."""
    if factor_key not in FACTOR_CONTRACTS:
        raise KeyError(f"unknown factor: {factor_key}")
    if ic is None:
        return None
    value = _finite_number(ic)
    if value is None:
        return None
    return FACTOR_CONTRACTS[factor_key].expected_direction * value


def forward_return_for_symbol(
    bars_by_session: Mapping[tuple[str, str], Mapping[str, object]],
    symbol: str,
    day: str | date,
    horizon: int,
    sessions: Sequence[str | date],
) -> LabelResult:
    """Compute the diagnostic 09:25-open proxy return without shortening labels."""
    path = horizon_sessions(day, horizon, sessions)
    if path is None:
        return LabelResult(None, "not_mature", "horizon_not_settled", None)

    first = bars_by_session.get((path[0], symbol))
    if first is None:
        return LabelResult(None, "incomplete", f"missing_bar:{path[0]}", path[-1])
    try:
        open_price = float(first["open"])
        close_price = float(first["close"])
    except (KeyError, TypeError, ValueError):
        return LabelResult(None, "incomplete", f"missing_open_or_close:{path[0]}", path[-1])
    if not (math.isfinite(open_price) and math.isfinite(close_price)
            and open_price > 0 and close_price > 0):
        return LabelResult(None, "incomplete", f"invalid_open_or_close:{path[0]}", path[-1])

    gross = close_price / open_price
    for session in path[1:]:
        row = bars_by_session.get((session, symbol))
        if row is None:
            return LabelResult(None, "incomplete", f"missing_bar:{session}", path[-1])
        try:
            change_pct = float(row["change_pct"])
        except (KeyError, TypeError, ValueError):
            return LabelResult(None, "incomplete", f"missing_change_pct:{session}", path[-1])
        if not math.isfinite(change_pct) or change_pct < -100.0:
            return LabelResult(None, "incomplete", f"invalid_change_pct:{session}", path[-1])
        gross *= 1.0 + change_pct / 100.0
    value = gross - 1.0
    if not math.isfinite(value):
        return LabelResult(None, "incomplete", "non_finite_return", path[-1])
    return LabelResult(value, "ok", None, path[-1])
