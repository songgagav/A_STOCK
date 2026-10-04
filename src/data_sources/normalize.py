"""Pure source-to-canonical field normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .adapters import CanonicalBatch, CanonicalRecord, RawBatch


_MARKET_SUFFIXES = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


def _normalize_symbol(value: Any) -> str:
    code = str(value).strip()
    if "." in code:
        market, number = code.split(".", 1)
        suffix = _MARKET_SUFFIXES.get(market.lower())
        if suffix:
            return f"{number.upper()}.{suffix}"
    return code.upper()


def _as_float(value: Any) -> float:
    return float(value)


def _as_shares(value: Any) -> int:
    return int(float(value))


def normalize_baostock(raw: Mapping[str, Any]) -> CanonicalRecord:
    """Map one Baostock record to canonical units without quality validation.

    Canonical volume is shares. Baostock already reports shares, so no unit
    multiplier is applied here. OHLC values remain unadjusted and the factor is
    carried separately for downstream consumers.
    """
    return {
        "symbol": _normalize_symbol(raw["code"]),
        "trade_day": str(raw["date"]),
        "open": _as_float(raw["open"]),
        "high": _as_float(raw["high"]),
        "low": _as_float(raw["low"]),
        "close": _as_float(raw["close"]),
        "volume": _as_shares(raw["volume"]),
        "amount": _as_float(raw["amount"]),
        "adj_factor": _as_float(raw.get("adj_factor", 1.0)),
    }


def normalize_baostock_batch(raw_batch: RawBatch) -> CanonicalBatch:
    """Normalize every raw record in a Baostock batch in input order."""
    return [normalize_baostock(record) for record in raw_batch.records]
