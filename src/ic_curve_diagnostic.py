"""Pure point-in-time calculations for the isolated H5i IC diagnostic.

This module deliberately has no runtime hooks. Its only strategy dependency is
the existing technical-factor score function; H5i loading and bundle writing
are added by later tasks in this diagnostic branch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import csv
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Any, Mapping, Sequence

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
FACTOR_DEFINITION_VERSION = "factor-library-selector-scores-v1"
_CSV_COLUMNS = (
    "day", "horizon", "ic", "status", "n_factor", "n_forward_valid",
    "n_pairs", "n_missing_factor", "n_missing_forward",
)
_MANIFEST_REQUIRED_KEYS = (
    "parameters", "inputs", "formulas", "horizons", "source_max_day",
    "calendar_due_dates", "last_settled_ic_day", "row_counts", "missingness",
    "calendar_provenance", "diagnostic_only", "authoritative", "observation_eligible",
)


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


def _positive_history_by_symbol(
    normalized: pd.DataFrame, finite_positive: np.ndarray
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Index each symbol's ordered positive-close history once for PIT lookups."""
    positive = normalized.loc[finite_positive, ["day", "symbol", "close"]]
    histories: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for symbol, group in positive.groupby("symbol", sort=False):
        histories[str(symbol)] = (
            group["day"].to_numpy(dtype=str),
            group["close"].to_numpy(dtype=float),
        )
    return histories


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
    history_by_symbol = _positive_history_by_symbol(normalized, finite_positive)
    for day in days:
        day_rows = normalized.loc[normalized["day"] == day]
        for row in day_rows.itertuples(index=False):
            if not np.isfinite(row.close) or row.close <= 0.0:
                _count(missing, day, "invalid_signal_close")
                continue

            symbol_days, symbol_closes = history_by_symbol[str(row.symbol)]
            end = int(np.searchsorted(symbol_days, day, side="right"))
            history_closes = symbol_closes[max(0, end - 21) : end]
            if len(history_closes) < 21:
                _count(missing, day, "insufficient_history")
                continue

            as_of_bars = pd.DataFrame({"close": history_closes})
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
            WITH pre_start_counts AS (
                SELECT CAST(ts AS DATE) AS day,
                       TRIM(CAST(symbol AS VARCHAR)) AS symbol,
                       close,
                       change_pct,
                       COUNT(*) OVER (
                           PARTITION BY TRIM(CAST(symbol AS VARCHAR)), CAST(ts AS DATE)
                       ) AS duplicate_count
                FROM daily_bars
                WHERE CAST(ts AS DATE) < DATE '{start_day[:4]}-{start_day[4:6]}-{start_day[6:]}'
            ), pre_start_ranked AS (
                SELECT day, symbol, close, change_pct, duplicate_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY day DESC
                       ) AS row_number
                FROM pre_start_counts
                WHERE close IS NOT NULL
                  AND close > 0
                  AND NOT isnan(close)
                  AND close < 1e308
            ), bounded_rows AS (
                SELECT day, symbol, close, change_pct, duplicate_count
                FROM pre_start_ranked
                WHERE row_number <= 20
                UNION ALL
                SELECT CAST(ts AS DATE) AS day,
                       TRIM(CAST(symbol AS VARCHAR)) AS symbol,
                       close,
                       change_pct,
                       1 AS duplicate_count
                FROM daily_bars
                WHERE CAST(ts AS DATE) BETWEEN
                      DATE '{start_day[:4]}-{start_day[4:6]}-{start_day[6:]}'
                      AND DATE '{query_end_day[:4]}-{query_end_day[4:6]}-{query_end_day[6:]}'
            )
            SELECT day, symbol, close, change_pct, duplicate_count
            FROM bounded_rows
            ORDER BY day, symbol
        """
        frame = database.sql(query).to_pandas()
    finally:
        database.close()

    if "duplicate_count" in frame.columns:
        duplicate_rows = pd.to_numeric(frame["duplicate_count"], errors="coerce").gt(1)
        if duplicate_rows.any():
            raise ValueError("duplicate day/symbol bar rows")
        frame = frame.drop(columns=["duplicate_count"])
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


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("identity/manifest values must be finite JSON data") from exc


def build_run_identity(
    parameters: Mapping[str, Any],
    *,
    code_sha: str,
    calendar_sha256: str,
    schema_fingerprint: str,
    queried_rows_sha256: str,
    source_max_day: str,
) -> tuple[str, dict[str, Any]]:
    """Return a deterministic identity bound to code, inputs, and formulas."""
    if not isinstance(parameters, Mapping):
        raise ValueError("parameters must be a mapping")
    commit_sha = str(code_sha).lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha):
        raise ValueError("code_sha must be a full hexadecimal commit SHA")
    hashes = {
        "calendar_sha256": calendar_sha256,
        "schema_fingerprint": schema_fingerprint,
        "queried_rows_sha256": queried_rows_sha256,
    }
    for name, value in hashes.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(value).lower()):
            raise ValueError(f"{name} must be a 64-character SHA-256 digest")
        hashes[name] = str(value).lower()
    try:
        normalized_parameters = json.loads(_canonical_json(dict(parameters)).decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("parameters must contain finite JSON values") from exc
    identity = {
        "identity_schema": "h5i-ic-diagnostic-run-v1",
        "parameters": normalized_parameters,
        "code_sha": commit_sha,
        "factor_definition_version": FACTOR_DEFINITION_VERSION,
        **hashes,
        "source_max_day": _day_key(source_max_day),
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest(), identity


def _validate_csv(name: str, payload: bytes) -> None:
    if name not in {f"{factor}.csv" for factor in FACTOR_NAMES}:
        raise ValueError(f"unexpected diagnostic CSV name: {name}")
    try:
        text = payload.decode("utf-8")
        records = list(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"{name} is not a valid UTF-8 CSV") from exc
    if not records or tuple(records[0]) != _CSV_COLUMNS:
        raise ValueError(f"{name} has an invalid IC CSV header")
    previous: tuple[str, int] | None = None
    if len(records) == 1:
        raise ValueError(f"{name} has no diagnostic rows")
    allowed_status = {"settled", "not_mature", "insufficient_pairs", "constant_rank"}
    for line_number, record in enumerate(records[1:], start=2):
        if len(record) != len(_CSV_COLUMNS):
            raise ValueError(f"{name}:{line_number} has an invalid number of fields")
        row = dict(zip(_CSV_COLUMNS, record))
        day = row["day"]
        if not re.fullmatch(r"\d{8}", day) or _day_key(day) != day:
            raise ValueError(f"{name}:{line_number} day must be YYYYMMDD")
        try:
            horizon = int(row["horizon"])
            if horizon <= 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"{name}:{line_number} has an invalid horizon") from exc
        key = (day, horizon)
        if previous is not None and key <= previous:
            raise ValueError(f"{name} rows must be unique and sorted by day,horizon")
        previous = key
        if row["status"] not in allowed_status:
            raise ValueError(f"{name}:{line_number} has an invalid status")
        if row["status"] == "settled":
            try:
                ic = float(row["ic"])
            except ValueError as exc:
                raise ValueError(f"{name}:{line_number} settled IC must be finite") from exc
            if not np.isfinite(ic):
                raise ValueError(f"{name}:{line_number} settled IC must be finite")
        elif row["ic"] != "":
            raise ValueError(f"{name}:{line_number} null IC must be an empty CSV field")
        try:
            counts = [
                int(row[field])
                for field in (
                    "n_factor", "n_forward_valid", "n_pairs", "n_missing_factor", "n_missing_forward"
                )
            ]
        except ValueError as exc:
            raise ValueError(f"{name}:{line_number} has an invalid count") from exc
        if any(count < 0 for count in counts):
            raise ValueError(f"{name}:{line_number} counts must be non-negative")
        n_factor, n_forward_valid, n_pairs = counts[:3]
        if n_pairs > min(n_factor, n_forward_valid):
            raise ValueError(f"{name}:{line_number} paired count exceeds a source count")


def _prepare_manifest(
    manifest: Mapping[str, Any], run_id: str, csv_files: Mapping[str, bytes]
) -> bytes:
    if not isinstance(manifest, Mapping):
        raise ValueError("manifest must be a mapping")
    missing = set(_MANIFEST_REQUIRED_KEYS).difference(manifest)
    if missing:
        raise ValueError(f"manifest missing required keys: {sorted(missing)}")
    if manifest.get("calendar_provenance") != _CALENDAR_PROVENANCE:
        raise ValueError("manifest calendar provenance must be local_calendar_unknown_upstream")
    expected_flags = {
        "diagnostic_only": True,
        "authoritative": False,
        "observation_eligible": False,
    }
    for key, expected in expected_flags.items():
        if manifest.get(key) is not expected:
            raise ValueError(f"manifest {key} must be {expected}")
    if manifest.get("run_id", run_id) != run_id:
        raise ValueError("manifest run_id does not match the destination")

    csv_hashes = {name: hashlib.sha256(csv_files[name]).hexdigest() for name in sorted(csv_files)}
    supplied_hashes = manifest.get("csv_sha256")
    if supplied_hashes is not None and supplied_hashes != csv_hashes:
        raise ValueError("manifest CSV hashes do not match the supplied bytes")
    result = dict(manifest)
    result.update(
        {
            "bundle_schema_version": "h5i-ic-diagnostic-bundle-v1",
            "factor_definition_version": FACTOR_DEFINITION_VERSION,
            "run_id": run_id,
            "csv_sha256": csv_hashes,
        }
    )
    return _canonical_json(result) + b"\n"


def _assert_safe_output_root(output_root: Path, repo_root: Path) -> Path:
    resolved_repo = repo_root.expanduser().resolve(strict=True)
    if not resolved_repo.is_dir():
        raise ValueError("repo_root must be a directory")
    try:
        candidate = Path(os.path.abspath(output_root.expanduser()))
        resolved_output = candidate.resolve(strict=False)
        protected_paths = (
            ("live-artifact path", (resolved_repo / "data").resolve(strict=False)),
            ("live IC artifact path", (resolved_repo / "data" / "ic").resolve(strict=False)),
            (
                "live evolution artifact path",
                (resolved_repo / "data" / "evolution").resolve(strict=False),
            ),
            (
                "live daily artifact path",
                (resolved_repo / "data" / "daily").resolve(strict=False),
            ),
        )
    except (OSError, RuntimeError) as exc:
        raise ValueError("output path cannot be resolved safely") from exc
    if resolved_output == resolved_repo or resolved_repo in resolved_output.parents:
        raise ValueError(f"output_root resolves inside protected repository root: {resolved_repo}")
    for label, protected_path in protected_paths:
        if resolved_output == protected_path or protected_path in resolved_output.parents:
            raise ValueError(f"output_root resolves inside protected {label}: {protected_path}")
    # Keep the caller's lexical path for no-follow, handle-relative traversal.
    # Returning resolve() here would silently convert an external symlink into
    # its target and make later path operations raceable again.
    return candidate


@dataclass
class _OutputDirectoryAnchor:
    path: Path
    fd: int | None = None
    handles: list[int] | None = None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.handles:
            _kernel32().CloseHandle.argtypes = [ctypes.c_void_p]
            _kernel32().CloseHandle.restype = ctypes.c_int
            for handle in reversed(self.handles):
                _kernel32().CloseHandle(ctypes.c_void_p(handle))
            self.handles.clear()


def _kernel32():
    if os.name != "nt":
        raise RuntimeError("Windows directory handles are unavailable on this platform")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _open_windows_directory(path: Path, *, access: int) -> int:
    from ctypes import wintypes

    kernel32 = _kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        access,
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deny rename/delete.
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    raw_handle = ctypes.cast(handle, ctypes.c_void_p).value
    if raw_handle in (None, ctypes.c_void_p(-1).value):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(path))

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    info = _AttributeTagInfo()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):  # FileAttributeTagInfo
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, ctypes.FormatError(error), str(path))
    if not info.FileAttributes & 0x10 or info.FileAttributes & 0x400:
        kernel32.CloseHandle(handle)
        raise ValueError(f"output path contains a non-directory or reparse point: {path}")
    return int(raw_handle)


def _acquire_output_directory(
    path: Path, *, create: bool, delete_final: bool = False, add_children: bool = False
) -> _OutputDirectoryAnchor:
    absolute = Path(os.path.abspath(path))
    if os.name == "nt":
        if not absolute.anchor or absolute.anchor.startswith("\\\\"):
            raise ValueError("UNC output roots are not supported by the safe writer")
        handles: list[int] = []
        prefix = Path(absolute.anchor)
        try:
            parts = absolute.parts
            for index, part in enumerate(parts):
                if index == 0:
                    current = prefix
                else:
                    current = current / part
                final = index == len(parts) - 1
                access = 0x00000080  # FILE_READ_ATTRIBUTES
                if final and delete_final:
                    access |= 0x00010000  # DELETE, required for handle-based rename.
                if final and add_children:
                    access |= 0x00000002 | 0x00000004  # FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY
                try:
                    handle = _open_windows_directory(current, access=access)
                except OSError as exc:
                    error_code = getattr(exc, "winerror", None) or exc.errno
                    if not create or error_code not in (2, 3):
                        raise
                    try:
                        os.mkdir(current)
                    except FileExistsError:
                        pass
                    handle = _open_windows_directory(current, access=access)
                handles.append(handle)
            return _OutputDirectoryAnchor(absolute, handles=handles)
        except Exception:
            _OutputDirectoryAnchor(absolute, handles=handles).close()
            raise

    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise RuntimeError("safe no-follow directory traversal is unavailable")
    if os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd:
        raise RuntimeError("handle-relative directory operations are unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return _OutputDirectoryAnchor(absolute, fd=current_fd)
    except Exception:
        os.close(current_fd)
        raise


def _write_bundle_file_at(directory: _OutputDirectoryAnchor, name: str, payload: bytes) -> None:
    if directory.fd is not None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=directory.fd)
        stream = os.fdopen(fd, "wb")
    else:
        stream = (directory.path / name).open("xb")
    with stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_existing_bundle(
    root: _OutputDirectoryAnchor, name: str, expected: Mapping[str, bytes]
) -> None:
    bundle = _acquire_output_directory(root.path / name, create=False)
    try:
        if bundle.fd is not None:
            actual_names = set(os.listdir(bundle.fd))
            if actual_names != set(expected):
                raise ValueError("existing bundle has unexpected or missing files")
            for filename, payload in expected.items():
                flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                try:
                    fd = os.open(filename, flags, dir_fd=bundle.fd)
                except OSError as exc:
                    raise ValueError("existing bundle contains a non-regular file") from exc
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read() != payload:
                        raise ValueError("existing bundle at run_id conflicts with requested bytes")
            return

        final_path = bundle.path
        actual_names = {item.name for item in final_path.iterdir()}
        if actual_names != set(expected):
            raise ValueError("existing bundle has unexpected or missing files")
        for filename, payload in expected.items():
            existing = final_path / filename
            if existing.is_symlink() or not existing.is_file() or existing.read_bytes() != payload:
                raise ValueError("existing bundle at run_id conflicts with requested bytes")
    finally:
        bundle.close()


def _rename_posix_noreplace(root_fd: int, source: str, destination: str) -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("atomic no-replace directory rename is unavailable on this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("libc renameat2 is required for immutable bundle publication")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(root_fd, os.fsencode(source), root_fd, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _rename_windows_by_handle(stage: _OutputDirectoryAnchor, destination: Path) -> None:
    from ctypes import wintypes

    if not stage.handles:
        raise RuntimeError("Windows rename requires an anchored staging directory handle")
    class _RenameInfo(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    encoded_name = str(destination).encode("utf-16-le")
    offset = _RenameInfo.FileName.offset
    buffer_size = ctypes.sizeof(_RenameInfo) + len(encoded_name)
    buffer = ctypes.create_string_buffer(buffer_size)
    info = ctypes.cast(buffer, ctypes.POINTER(_RenameInfo)).contents
    info.Flags = 0  # ReplaceIfExists = FALSE.
    # An absolute target is resolved while all destination ancestors are held
    # without FILE_SHARE_DELETE; the source itself remains bound by its handle.
    info.RootDirectory = None
    info.FileNameLength = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded_name, len(encoded_name))
    kernel32 = _kernel32()
    rename_by_handle = kernel32.SetFileInformationByHandle
    rename_by_handle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    rename_by_handle.restype = wintypes.BOOL
    if not rename_by_handle(
        wintypes.HANDLE(stage.handles[-1]), 3, buffer, buffer_size  # FileRenameInfo
    ):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(destination))


def _mark_windows_directory_for_delete(anchor: _OutputDirectoryAnchor) -> None:
    from ctypes import wintypes

    if not anchor.handles:
        return

    class _DispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    info = _DispositionInfo(True)
    kernel32 = _kernel32()
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    if not set_info(
        wintypes.HANDLE(anchor.handles[-1]), 4, ctypes.byref(info), ctypes.sizeof(info)
    ):  # FileDispositionInfo
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(anchor.path))


def _output_entry_exists(root: _OutputDirectoryAnchor, name: str) -> bool:
    if root.fd is not None:
        try:
            os.stat(name, dir_fd=root.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
    path = root.path / name
    return path.exists() or path.is_symlink()


def _assert_anchor_path_stable(root: _OutputDirectoryAnchor) -> None:
    if root.fd is None:
        # Windows ancestors are held open without FILE_SHARE_DELETE, so their
        # directory entries cannot be replaced while this anchor is alive.
        return
    try:
        anchored = os.fstat(root.fd)
        current = os.stat(root.path, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("output_root changed after safe directory anchoring") from exc
    if (anchored.st_dev, anchored.st_ino) != (current.st_dev, current.st_ino):
        raise ValueError("output_root changed after safe directory anchoring")


def _create_stage_directory(root: _OutputDirectoryAnchor, run_id: str) -> tuple[str, _OutputDirectoryAnchor]:
    for _ in range(10):
        name = f".{run_id}.tmp-{secrets.token_hex(8)}"
        path = root.path / name
        try:
            if root.fd is not None:
                os.mkdir(name, mode=0o700, dir_fd=root.fd)
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                fd = os.open(name, flags, dir_fd=root.fd)
                return name, _OutputDirectoryAnchor(path, fd=fd)
            os.mkdir(path)
            return name, _acquire_output_directory(path, create=False, delete_final=True)
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a unique diagnostic staging directory")


def _cleanup_stage(
    root: _OutputDirectoryAnchor,
    name: str,
    stage: _OutputDirectoryAnchor,
    filenames: Sequence[str],
) -> None:
    if stage.fd is not None:
        for filename in filenames:
            try:
                os.unlink(filename, dir_fd=stage.fd)
            except FileNotFoundError:
                pass
        stage.close()
        os.rmdir(name, dir_fd=root.fd)
        return

    for filename in filenames:
        try:
            (stage.path / filename).unlink()
        except FileNotFoundError:
            pass
    try:
        _mark_windows_directory_for_delete(stage)
    finally:
        stage.close()


def finalize_diagnostic_bundle(
    output_root: Path,
    *,
    repo_root: Path,
    run_id: str,
    csv_files: Mapping[str, bytes],
    manifest: Mapping[str, Any],
) -> Path:
    """Create a verified, immutable-by-convention bundle using an atomic rename."""
    if not re.fullmatch(r"[0-9a-f]{64}", str(run_id)):
        raise ValueError("run_id must be a 64-character lowercase SHA-256 digest")
    expected_names = {f"{factor}.csv" for factor in FACTOR_NAMES}
    if set(csv_files) != expected_names:
        raise ValueError(f"csv_files must contain exactly {sorted(expected_names)}")
    copied_csvs: dict[str, bytes] = {}
    for name in sorted(expected_names):
        payload = csv_files[name]
        if not isinstance(payload, bytes):
            raise ValueError(f"{name} content must be bytes")
        _validate_csv(name, payload)
        copied_csvs[name] = payload
    manifest_bytes = _prepare_manifest(manifest, run_id, copied_csvs)
    expected = {**copied_csvs, "manifest.json": manifest_bytes}

    safe_root = _assert_safe_output_root(Path(output_root), Path(repo_root))
    try:
        root = _acquire_output_directory(safe_root, create=True, add_children=True)
    except (OSError, ValueError) as exc:
        raise ValueError(f"output_root failed protected-path handle validation: {exc}") from exc

    final_path = root.path / run_id
    lock_name = f".{run_id}.lock"
    lock_stream = None
    lock_fd: int | None = None
    lock_acquired = False
    stage_name: str | None = None
    stage: _OutputDirectoryAnchor | None = None
    try:
        _assert_anchor_path_stable(root)
        if _output_entry_exists(root, run_id):
            _verify_existing_bundle(root, run_id, expected)
            _assert_anchor_path_stable(root)
            return final_path

        if root.fd is not None:
            lock_fd = os.open(
                lock_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=root.fd,
            )
            lock_stream = os.fdopen(lock_fd, "wb")
            lock_fd = None
        else:
            lock_stream = (root.path / lock_name).open("xb")
        lock_acquired = True
        with lock_stream:
            lock_stream.write(b"diagnostic bundle finalization in progress\n")
            lock_stream.flush()
            os.fsync(lock_stream.fileno())
        lock_stream = None

        if _output_entry_exists(root, run_id):
            _verify_existing_bundle(root, run_id, expected)
            _assert_anchor_path_stable(root)
            return final_path

        stage_name, stage = _create_stage_directory(root, run_id)
        for name in sorted(copied_csvs):
            _write_bundle_file_at(stage, name, copied_csvs[name])
        _write_bundle_file_at(stage, "manifest.json", manifest_bytes)
        if stage.fd is not None:
            os.fsync(stage.fd)
        _assert_anchor_path_stable(root)
        try:
            if root.fd is not None:
                _rename_posix_noreplace(root.fd, stage_name, run_id)
            else:
                _rename_windows_by_handle(stage, final_path)
        except OSError:
            if _output_entry_exists(root, run_id):
                _verify_existing_bundle(root, run_id, expected)
                _assert_anchor_path_stable(root)
                return final_path
            raise
        stage_name = None
        stage.close()
        stage = None
        _assert_anchor_path_stable(root)
        return final_path
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_stream is not None:
            lock_stream.close()
        try:
            if stage_name is not None and stage is not None:
                _cleanup_stage(root, stage_name, stage, list(expected))
        finally:
            try:
                if lock_acquired:
                    if root.fd is not None:
                        os.unlink(lock_name, dir_fd=root.fd)
                    else:
                        (root.path / lock_name).unlink(missing_ok=True)
            finally:
                root.close()
