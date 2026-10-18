"""Pure point-in-time calculations for the isolated H5i IC diagnostic.

This module deliberately has no runtime hooks. Its only strategy dependency is
the existing technical-factor score function; H5i loading and bundle writing
are added by later tasks in this diagnostic branch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
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
_CALENDAR_PROVENANCE = "local_calendar_unknown_upstream"


@dataclass(frozen=True)
class DiagnosticInputs:
    """Bounded, read-only snapshot and the identities needed to revalidate it."""

    bars: pd.DataFrame
    calendar_days: tuple[str, ...]
    signal_days: tuple[str, ...]
    source_max_day: str
    calendar_sha256: str
    schema_fingerprint: str
    queried_rows_sha256: str
    source_path: Path
    calendar_path: Path
    start_day: str
    end_day: str
    max_future_due_day: str
    query_bounds: dict[str, str]
    calendar_provenance: str = _CALENDAR_PROVENANCE


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


def _schema_fingerprint(schema: Any) -> str:
    description = [
        {"name": field.name, "type": str(field.type), "nullable": bool(field.nullable)}
        for field in schema
    ]
    payload = json.dumps(description, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_digest(bars: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in bars.sort_values(["day", "symbol"], kind="mergesort").itertuples(index=False):
        values: list[Any] = [str(row.day), str(row.symbol)]
        for value in (row.close, row.change_pct):
            if pd.isna(value):
                values.append(None)
            else:
                values.append(float(value).hex())
        digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _calendar_from_bytes(payload: bytes) -> list[str]:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("calendar file is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        raise ValueError("calendar JSON must contain a days array")
    return _calendar_days(data["days"])


def _is_weekday(day: str) -> bool:
    return datetime.strptime(day, "%Y%m%d").weekday() < 5


def _validate_daily_bars_schema(schema: Any) -> None:
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover - h5i runtime includes pyarrow
        raise RuntimeError("pyarrow is required to validate the H5i schema") from exc

    fields = {field.name: field for field in schema}
    required = {"ts", "symbol", "close", "change_pct"}
    if not required.issubset(fields):
        raise ValueError(f"daily_bars schema missing columns: {sorted(required - set(fields))}")
    symbol_type = fields["symbol"].type
    if not (pa.types.is_string(symbol_type) or pa.types.is_large_string(symbol_type)):
        raise ValueError("daily_bars schema has incompatible symbol type")
    if not (pa.types.is_timestamp(fields["ts"].type) or pa.types.is_date(fields["ts"].type)):
        raise ValueError("daily_bars schema has incompatible ts type")
    for name in ("close", "change_pct"):
        value_type = fields[name].type
        if not (pa.types.is_integer(value_type) or pa.types.is_floating(value_type) or pa.types.is_decimal(value_type)):
            raise ValueError(f"daily_bars schema has incompatible {name} type")


def _query_h5i_window(
    source_path: Path, start_day: str, end_day: str, max_future_due_day: str
) -> tuple[pd.DataFrame, str, str, str]:
    try:
        import h5i_db
    except ImportError as exc:
        raise RuntimeError("h5i_db is required; no alternate data source is allowed") from exc

    database = h5i_db.Database(str(source_path), read_only=True)
    try:
        try:
            schema = database.schema("daily_bars")
        except Exception as exc:
            raise ValueError("daily_bars schema is unavailable") from exc
        _validate_daily_bars_schema(schema)
        fingerprint = _schema_fingerprint(schema)

        watermark = database.sql(
            "SELECT MAX(CAST(ts AS DATE)) AS source_max_day FROM daily_bars"
        ).to_pandas()
        if watermark.empty or pd.isna(watermark.iloc[0]["source_max_day"]):
            raise ValueError("daily_bars has no source watermark")
        source_max_day = _day_key(watermark.iloc[0]["source_max_day"])
        if end_day > source_max_day:
            raise ValueError(f"end {end_day} is later than source max {source_max_day}")
        query_end_day = min(max_future_due_day, source_max_day)

        query = f"""
            WITH pre_start_ranked AS (
                SELECT CAST(ts AS DATE) AS day,
                       TRIM(CAST(symbol AS VARCHAR)) AS symbol,
                       close,
                       change_pct,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY CAST(ts AS DATE) DESC
                       ) AS row_number
                FROM daily_bars
                WHERE CAST(ts AS DATE) < DATE '{start_day[:4]}-{start_day[4:6]}-{start_day[6:]}'
                  AND close IS NOT NULL
                  AND close > 0
                  AND NOT isnan(close)
                  AND close < 1e308
            ), bounded_rows AS (
                SELECT day, symbol, close, change_pct
                FROM pre_start_ranked
                WHERE row_number <= 20
                UNION ALL
                SELECT CAST(ts AS DATE) AS day,
                       TRIM(CAST(symbol AS VARCHAR)) AS symbol,
                       close,
                       change_pct
                FROM daily_bars
                WHERE CAST(ts AS DATE) BETWEEN
                      DATE '{start_day[:4]}-{start_day[4:6]}-{start_day[6:]}'
                      AND DATE '{query_end_day[:4]}-{query_end_day[4:6]}-{query_end_day[6:]}'
            )
            SELECT day, symbol, close, change_pct
            FROM bounded_rows
            ORDER BY day, symbol
        """
        frame = database.sql(query).to_pandas()
    finally:
        database.close()

    frame = frame.rename(columns={"day": "day", "symbol": "symbol"})
    frame = _normalize_bars(frame)
    return frame, source_max_day, fingerprint, query_end_day


def read_diagnostic_inputs(
    h5i_path: Path, calendar_path: Path, *, start: str, end: str
) -> DiagnosticInputs:
    """Read only a 20-close lookback and bounded requested/future H5i window."""
    source_path = Path(h5i_path).expanduser().resolve(strict=True)
    resolved_calendar_path = Path(calendar_path).expanduser().resolve(strict=True)
    start_day, end_day = _day_key(start), _day_key(end)
    if start_day > end_day:
        raise ValueError("start must be on or before end")

    calendar_bytes = resolved_calendar_path.read_bytes()
    full_calendar = _calendar_from_bytes(calendar_bytes)
    weekday_days = [day for day in full_calendar if _is_weekday(day)]
    signal_days = [day for day in weekday_days if start_day <= day <= end_day]
    if not signal_days:
        raise ValueError("calendar has no weekdays in the requested interval")
    future_days = [day for day in weekday_days if day > end_day]
    if len(future_days) < 20:
        raise ValueError("calendar must cover 20 future weekdays after end")
    max_future_due_day = future_days[19]

    # The single read-only connection obtains the watermark with an aggregate,
    # then caps the bounded row query before reading any data rows.
    bars, source_max_day, schema_fingerprint, query_end_day = _query_h5i_window(
        source_path, start_day, end_day, max_future_due_day
    )

    calendar_window = tuple(
        day for day in weekday_days if start_day <= day <= max_future_due_day
    )
    window_days = set(calendar_window)
    queried_days = set(bars.loc[bars["day"] >= start_day, "day"].astype(str))
    off_calendar = sorted(queried_days - window_days)
    if off_calendar:
        raise ValueError(f"H5i rows fall on dates absent from the supplied calendar: {off_calendar[:5]}")

    expected_past_days = [day for day in calendar_window if day <= source_max_day]
    missing_days = sorted(set(expected_past_days) - queried_days)
    if missing_days:
        raise ValueError(f"past calendar days without H5i bars: {missing_days[:10]}")

    query_bounds = {
        "data_start": start_day,
        "data_end": query_end_day,
        "pre_start_lookback": "last_20_finite_positive_closes_per_symbol",
    }
    return DiagnosticInputs(
        bars=bars,
        calendar_days=calendar_window,
        signal_days=tuple(signal_days),
        source_max_day=source_max_day,
        calendar_sha256=hashlib.sha256(calendar_bytes).hexdigest(),
        schema_fingerprint=schema_fingerprint,
        queried_rows_sha256=_row_digest(bars),
        source_path=source_path,
        calendar_path=resolved_calendar_path,
        start_day=start_day,
        end_day=end_day,
        max_future_due_day=max_future_due_day,
        query_bounds=query_bounds,
    )


def verify_inputs_unchanged(inputs: DiagnosticInputs) -> None:
    """Reopen the same read-only inputs and reject any identity or row drift."""
    try:
        calendar_sha = hashlib.sha256(inputs.calendar_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("calendar file disappeared or became unreadable") from exc
    if calendar_sha != inputs.calendar_sha256:
        raise ValueError("calendar bytes changed after input snapshot")

    current = read_diagnostic_inputs(
        inputs.source_path,
        inputs.calendar_path,
        start=inputs.start_day,
        end=inputs.end_day,
    )
    if current.schema_fingerprint != inputs.schema_fingerprint:
        raise ValueError("H5i daily_bars schema changed after input snapshot")
    if current.source_max_day != inputs.source_max_day:
        raise ValueError("H5i source max changed after input snapshot")
    if current.queried_rows_sha256 != inputs.queried_rows_sha256:
        raise ValueError("queried H5i rows changed after input snapshot")
