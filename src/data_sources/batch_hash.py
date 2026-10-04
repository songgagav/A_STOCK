"""Canonical serialization and content hashing for staged bar batches."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

from .adapters import CanonicalBatch, CanonicalRecord


_CANONICAL_FIELDS = (
    "symbol",
    "trade_day",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
)
_FLOAT_FIELDS = {"open", "high", "low", "close", "amount", "adj_factor"}
_QUANTUM = Decimal("0.0000000001")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"{field} must be a finite number")
    if decimal_value.is_zero() and decimal_value.is_signed():
        raise ValueError(f"{field} must not be negative zero")
    return decimal_value


def _normalize_float(value: Any, field: str) -> str:
    decimal_value = _decimal(value, field).quantize(_QUANTUM)
    return format(decimal_value, "f")


def _normalize_volume(value: Any) -> int:
    decimal_value = _decimal(value, "volume")
    if decimal_value != decimal_value.to_integral_value():
        raise ValueError("volume must be an integer")
    return int(decimal_value)


def _normalize_record(record: CanonicalRecord) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "symbol": record["symbol"],
        "trade_day": record["trade_day"],
    }
    if not isinstance(normalized["symbol"], str) or not normalized["symbol"]:
        raise ValueError("symbol must be a non-empty string")
    if not isinstance(normalized["trade_day"], str) or not normalized["trade_day"]:
        raise ValueError("trade_day must be a non-empty string")

    for field in _CANONICAL_FIELDS[2:]:
        if field not in record:
            raise ValueError(f"missing canonical field: {field}")
        if field in _FLOAT_FIELDS:
            normalized[field] = _normalize_float(record[field], field)
        else:
            normalized[field] = _normalize_volume(record[field])
    return normalized


def canonical_serialize(batch: CanonicalBatch) -> str:
    """Return deterministic JSON for the canonical nine-field batch projection."""
    normalized = [_normalize_record(record) for record in batch]
    normalized.sort(key=lambda record: (record["trade_day"], record["symbol"]))
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    )


def content_hash(batch: CanonicalBatch) -> str:
    """Hash the canonical UTF-8 JSON representation with SHA-256."""
    payload = canonical_serialize(batch).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
