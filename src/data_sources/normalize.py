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
    if code.isdigit() and len(code) == 6:
        if code.startswith("6"):
            return f"{code}.SH"
        if code.startswith(("0", "3")):
            return f"{code}.SZ"
        if code.startswith(("4", "8", "9")):
            return f"{code}.BJ"
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


def _normalize_generic(
    raw: Mapping[str, Any],
    *,
    symbol_field: str,
    date_field: str,
    volume_field: str = "volume",
    amount_field: str = "amount",
) -> CanonicalRecord:
    """Normalize a vendor row whose prices already use the canonical units."""
    trade_day = str(raw[date_field]).replace("/", "-")
    if len(trade_day) == 8 and trade_day.isdigit():
        trade_day = f"{trade_day[:4]}-{trade_day[4:6]}-{trade_day[6:]}"
    return {
        "symbol": _normalize_symbol(raw[symbol_field]),
        "trade_day": trade_day,
        "open": _as_float(raw["open"]),
        "high": _as_float(raw["high"]),
        "low": _as_float(raw["low"]),
        "close": _as_float(raw["close"]),
        "volume": _as_shares(raw[volume_field]),
        "amount": _as_float(raw[amount_field]),
        "adj_factor": _as_float(raw.get("adj_factor", 1.0)),
    }


def normalize_mootdx(raw: Mapping[str, Any]) -> CanonicalRecord:
    """Normalize one mootdx daily bar; ``vol`` is shares in the adapter contract."""
    return _normalize_generic(
        raw,
        symbol_field="symbol",
        date_field="datetime",
        volume_field="vol",
    )


def normalize_zzshare(raw: Mapping[str, Any]) -> CanonicalRecord:
    """Normalize one ZZShare daily bar; ``vol`` is converted to shares upstream."""
    return _normalize_generic(
        raw,
        symbol_field="ts_code",
        date_field="trade_date",
        volume_field="vol",
    )


def normalize_source_batch(raw_batch: RawBatch) -> CanonicalBatch:
    """Normalize a supported source batch without applying quality policy."""
    normalizers = {
        "baostock": normalize_baostock,
        "mootdx": normalize_mootdx,
        "zzshare": normalize_zzshare,
    }
    try:
        normalizer = normalizers[raw_batch.source]
    except KeyError as exc:
        raise ValueError(f"no normalizer registered for source {raw_batch.source!r}") from exc
    return [normalizer(record) for record in raw_batch.records]
