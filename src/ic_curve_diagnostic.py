"""Pure point-in-time calculations for the isolated H5i IC diagnostic.

This module deliberately has no runtime hooks. Its only strategy dependency is
the existing technical-factor score function; H5i loading and bundle writing
are added by later tasks in this diagnostic branch.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

import numpy as np
import pandas as pd

from factor_library import score_factor


DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
FACTOR_NAMES = ("vol_selector_score_v1", "mom_rev_selector_score_v1")
_FACTOR_SOURCE_NAMES = {
    "vol_selector_score_v1": "vol",
    "mom_rev_selector_score_v1": "mom",
}
_BAR_COLUMNS = ("day", "symbol", "close", "change_pct")
_FACTOR_COLUMNS = ("day", "symbol", "factor", "score")
_LABEL_COLUMNS = ("day", "horizon", "symbol", "forward_return", "valid", "reason")
_CURVE_COLUMNS = (
    "day",
    "horizon",
    "ic",
    "status",
    "n_factor",
    "n_forward_valid",
    "n_pairs",
    "n_missing_factor",
    "n_missing_forward",
)


def _day_key(value: Any) -> str:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        parsed = pd.Timestamp(value)
    else:
        try:
            parsed = pd.Timestamp(str(value).strip())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid day: {value!r}") from exc
    if pd.isna(parsed):
        raise ValueError(f"invalid day: {value!r}")
    return parsed.strftime("%Y%m%d")


def _signal_days(values: Sequence[str]) -> list[str]:
    days = [_day_key(value) for value in values]
    if len(set(days)) != len(days):
        raise ValueError("signal days must be unique")
    return sorted(days)


def _horizons(values: Sequence[int]) -> tuple[int, ...]:
    horizons = tuple(values)
    if any(isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value <= 0
           for value in horizons):
        raise ValueError("horizons must be positive integers")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons must be unique")
    return tuple(sorted(int(value) for value in horizons))


def _normalize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    missing = set(_BAR_COLUMNS).difference(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    result = bars.loc[:, _BAR_COLUMNS].copy()
    result["day"] = [_day_key(value) for value in result["day"]]
    result["symbol"] = result["symbol"].astype("string").str.strip()
    if result["symbol"].isna().any() or result["symbol"].eq("").any():
        raise ValueError("bars contain an empty symbol")
    if result.duplicated(["day", "symbol"]).any():
        raise ValueError("duplicate day/symbol bar rows")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result["change_pct"] = pd.to_numeric(result["change_pct"], errors="coerce")
    return result.sort_values(["day", "symbol"], kind="mergesort").reset_index(drop=True)


def _calendar_days(values: Sequence[str]) -> list[str]:
    days = [_day_key(value) for value in values]
    if any(left >= right for left, right in zip(days, days[1:])):
        raise ValueError("calendar days must be strictly increasing and unique")
    return days


def _count(missing: dict[str, dict[str, int]], day: str, reason: str) -> None:
    by_reason = missing.setdefault(day, {})
    by_reason[reason] = by_reason.get(reason, 0) + 1


def compute_factor_scores(
    bars: pd.DataFrame, signal_days: Sequence[str]
) -> tuple[pd.DataFrame, dict[str, dict[str, int]]]:
    """Score each positive-close symbol using only its last 21 closes through t."""
    normalized = _normalize_bars(bars)
    days = _signal_days(signal_days)
    scores: list[dict[str, Any]] = []
    missing: dict[str, dict[str, int]] = {}

    finite_positive = np.isfinite(normalized["close"].to_numpy(dtype=float)) & (
        normalized["close"].to_numpy(dtype=float) > 0.0
    )
    for day in days:
        day_rows = normalized.loc[normalized["day"] == day]
        for row in day_rows.itertuples(index=False):
            if not np.isfinite(row.close) or row.close <= 0.0:
                _count(missing, day, "invalid_signal_close")
                continue

            symbol_rows = normalized.loc[
                (normalized["symbol"] == row.symbol)
                & (normalized["day"] <= day)
                & finite_positive
            ].tail(21)
            if len(symbol_rows) < 21:
                _count(missing, day, "insufficient_history")
                continue

            as_of_bars = symbol_rows.loc[:, ["close"]].reset_index(drop=True)
            for factor_name, source_name in _FACTOR_SOURCE_NAMES.items():
                value = float(score_factor(source_name, as_of_bars))
                if not np.isfinite(value):
                    _count(missing, day, "invalid_factor_score")
                    continue
                scores.append(
                    {"day": day, "symbol": row.symbol, "factor": factor_name, "score": value}
                )

    frame = pd.DataFrame(scores, columns=_FACTOR_COLUMNS)
    if not frame.empty:
        frame = frame.sort_values(["day", "factor", "symbol"], kind="mergesort").reset_index(drop=True)
    return frame, missing


def compute_forward_labels(
    bars: pd.DataFrame,
    calendar_days: Sequence[str],
    signal_days: Sequence[str],
    source_max_day: str,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> tuple[pd.DataFrame, dict[str, dict[str, int]]]:
    """Compound exact calendar-listed returns after t; never fill a missing row."""
    normalized = _normalize_bars(bars)
    calendar = _calendar_days(calendar_days)
    days = _signal_days(signal_days)
    source_max = _day_key(source_max_day)
    hs = _horizons(horizons)
    calendar_index = {day: position for position, day in enumerate(calendar)}
    for day in days:
        if day not in calendar_index:
            raise ValueError(f"signal day {day} is not in the supplied calendar")

    by_day_symbol = normalized.set_index(["day", "symbol"], drop=False)
    labels: list[dict[str, Any]] = []
    missing: dict[str, dict[str, int]] = {}

    for day in days:
        day_rows = normalized.loc[normalized["day"] == day]
        candidates = day_rows.loc[np.isfinite(day_rows["close"]) & (day_rows["close"] > 0.0)]
        position = calendar_index[day]
        for row in candidates.itertuples(index=False):
            for horizon in hs:
                due_days = calendar[position + 1 : position + horizon + 1]
                reason = ""
                forward_return: float | None = None
                if len(due_days) < horizon or due_days[-1] > source_max:
                    reason = "not_mature"
                else:
                    returns: list[float] = []
                    for due_day in due_days:
                        key = (due_day, row.symbol)
                        if key not in by_day_symbol.index:
                            reason = "missing_future_bar"
                            break
                        pct = float(by_day_symbol.loc[key, "change_pct"])
                        if not np.isfinite(pct) or pct <= -100.0:
                            reason = "invalid_change_pct"
                            break
                        returns.append(pct)
                    if not reason:
                        compounded = float(np.prod(1.0 + np.asarray(returns, dtype=float) / 100.0) - 1.0)
                        if np.isfinite(compounded):
                            forward_return = compounded
                        else:
                            reason = "invalid_forward_return"

                valid = not reason
                labels.append(
                    {
                        "day": day,
                        "horizon": horizon,
                        "symbol": row.symbol,
                        "forward_return": forward_return,
                        "valid": valid,
                        "reason": reason,
                    }
                )
                if reason:
                    _count(missing, day, f"h{horizon}:{reason}")

    frame = pd.DataFrame(labels, columns=_LABEL_COLUMNS)
    if not frame.empty:
        frame = frame.sort_values(["day", "horizon", "symbol"], kind="mergesort").reset_index(drop=True)
    return frame, missing


def _required_frame(frame: pd.DataFrame, columns: Sequence[str], name: str) -> pd.DataFrame:
    missing = set(columns).difference(frame.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {sorted(missing)}")
    return frame.loc[:, columns].copy()


def build_ic_curves(
    factor_scores: pd.DataFrame,
    forward_labels: pd.DataFrame,
    signal_days: Sequence[str],
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> dict[str, pd.DataFrame]:
    """Build long-form daily Spearman IC curves for the two named factors."""
    scores = _required_frame(factor_scores, _FACTOR_COLUMNS, "factor_scores")
    labels = _required_frame(forward_labels, _LABEL_COLUMNS, "forward_labels")
    scores["day"] = [_day_key(value) for value in scores["day"]]
    labels["day"] = [_day_key(value) for value in labels["day"]]
    scores["symbol"] = scores["symbol"].astype("string").str.strip()
    labels["symbol"] = labels["symbol"].astype("string").str.strip()
    scores["score"] = pd.to_numeric(scores["score"], errors="coerce")
    labels["forward_return"] = pd.to_numeric(labels["forward_return"], errors="coerce")
    labels["valid"] = labels["valid"].fillna(False).astype(bool)
    labels["reason"] = labels["reason"].fillna("").astype(str)
    labels["horizon"] = pd.to_numeric(labels["horizon"], errors="raise").astype(int)
    if scores.duplicated(["day", "symbol", "factor"]).any():
        raise ValueError("duplicate day/symbol/factor score rows")
    if labels.duplicated(["day", "horizon", "symbol"]).any():
        raise ValueError("duplicate day/horizon/symbol label rows")
    days = _signal_days(signal_days)
    hs = _horizons(horizons)
    curves: dict[str, pd.DataFrame] = {}

    for factor in FACTOR_NAMES:
        rows: list[dict[str, Any]] = []
        for day in days:
            factor_rows = scores.loc[
                (scores["day"] == day)
                & (scores["factor"] == factor)
                & np.isfinite(scores["score"])
            ]
            factor_values = factor_rows.set_index("symbol")["score"]
            for horizon in hs:
                label_rows = labels.loc[(labels["day"] == day) & (labels["horizon"] == horizon)]
                not_mature = bool(label_rows["reason"].eq("not_mature").any())
                valid_mask = (
                    label_rows["valid"]
                    & label_rows["reason"].eq("")
                    & np.isfinite(label_rows["forward_return"])
                )
                valid_labels = label_rows.loc[valid_mask]
                valid_symbols = set(valid_labels["symbol"].astype(str))
                label_symbols = set(label_rows["symbol"].astype(str))
                factor_symbols = set(factor_values.index.astype(str))
                paired_symbols = sorted(valid_symbols & factor_symbols)
                n_forward_valid = len(valid_symbols)
                n_factor = len(factor_values)
                n_missing_factor = max(0, len(label_symbols) - len(factor_symbols & label_symbols))
                n_missing_forward = int(
                    (~label_rows["valid"] & label_rows["reason"].ne("not_mature")).sum()
                )
                ic: float | None = None

                if not_mature:
                    status = "not_mature"
                elif len(paired_symbols) < 5:
                    status = "insufficient_pairs"
                else:
                    forward_values = valid_labels.set_index("symbol")["forward_return"]
                    x = factor_values.loc[paired_symbols].rank(method="average").to_numpy(dtype=float)
                    y = forward_values.loc[paired_symbols].rank(method="average").to_numpy(dtype=float)
                    if np.ptp(x) == 0.0 or np.ptp(y) == 0.0:
                        status = "constant_rank"
                    else:
                        correlation = float(np.corrcoef(x, y)[0, 1])
                        if np.isfinite(correlation):
                            ic = correlation
                            status = "settled"
                        else:
                            status = "constant_rank"

                rows.append(
                    {
                        "day": day,
                        "horizon": horizon,
                        "ic": ic,
                        "status": status,
                        "n_factor": n_factor,
                        "n_forward_valid": n_forward_valid,
                        "n_pairs": len(paired_symbols),
                        "n_missing_factor": n_missing_factor,
                        "n_missing_forward": n_missing_forward,
                    }
                )
        curves[factor] = pd.DataFrame(rows, columns=_CURVE_COLUMNS)
    return curves
