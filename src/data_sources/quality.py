"""Quality checks for canonical in-memory batches."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

from .metadata import QualityStatus


_REQUIRED_FIELDS = frozenset(
    {
        "symbol",
        "trade_day",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "adj_factor",
    }
)


@dataclass(frozen=True, slots=True)
class QualityReport:
    status: QualityStatus
    coverage: float
    reasons: tuple[str, ...]
    checked_rows: int


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_quality(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_symbols: Sequence[str] | None = None,
    expected_trade_day: str | None = None,
) -> QualityReport:
    """Return quality findings without mutating the canonical rows.

    Coverage is reported, not thresholded here. The source router owns the
    policy threshold, while this layer owns row-level data integrity checks.
    """
    materialized = list(rows)
    reasons: list[str] = []
    seen_symbols: set[str] = set()

    for index, row in enumerate(materialized):
        missing = sorted(_REQUIRED_FIELDS - row.keys())
        if missing:
            reasons.extend(f"row[{index}].missing:{field}" for field in missing)
            continue

        symbol = row.get("symbol")
        if isinstance(symbol, str):
            seen_symbols.add(symbol)

        if expected_trade_day is not None and row.get("trade_day") != expected_trade_day:
            reasons.append(f"row[{index}].trade_day_mismatch")

        numeric_fields = ("open", "high", "low", "close", "amount", "adj_factor")
        if not all(_finite_number(row[field]) for field in numeric_fields):
            reasons.append(f"row[{index}].non_numeric_field")
        else:
            low = row["low"]
            high = row["high"]
            opening = row["open"]
            close = row["close"]
            if not (low <= opening <= high and low <= close <= high):
                reasons.append(f"row[{index}].ohlc_inconsistent")
            if close <= 0:
                reasons.append(f"row[{index}].close_non_positive")

        volume = row["volume"]
        if not _finite_number(volume) or volume < 0:
            reasons.append(f"row[{index}].volume_negative")

    if expected_symbols is None:
        coverage = 1.0 if materialized else 0.0
    else:
        expected = set(expected_symbols)
        coverage = len(seen_symbols & expected) / len(expected) if expected else 1.0

    status = QualityStatus.FAILED if reasons else QualityStatus.PASSED
    return QualityReport(
        status=status,
        coverage=coverage,
        reasons=tuple(reasons),
        checked_rows=len(materialized),
    )
