from __future__ import annotations

import math

import pytest

from src.data_sources.batch_hash import canonical_serialize, content_hash


def _record(symbol: str, *, close: float = 7.32, **extra: object) -> dict[str, object]:
    record: dict[str, object] = {
        "symbol": symbol,
        "trade_day": "2026-09-30",
        "open": 7.28,
        "high": 7.35,
        "low": 7.25,
        "close": close,
        "volume": 12_345_600,
        "amount": 90_234_567.89,
        "adj_factor": 1.0,
    }
    record.update(extra)
    return record


def test_hash_uses_fixed_canonical_fields_only() -> None:
    base = [_record("600000.SH")]
    with_noncanonical_fields = [
        _record(
            "600000.SH",
            source="baostock",
            retrieved_at="2026-10-04T12:00:00Z",
            quality="passed",
        )
    ]

    assert content_hash(base) == content_hash(with_noncanonical_fields)
    assert "source" not in canonical_serialize(base)
    assert "retrieved_at" not in canonical_serialize(base)


def test_hash_sorts_rows_by_trade_day_then_symbol() -> None:
    first = _record("600000.SH")
    second = {**_record("000001.SZ"), "trade_day": "2026-09-29"}

    assert content_hash([first, second]) == content_hash([second, first])
    serialized = canonical_serialize([first, second])
    assert serialized.index("2026-09-29") < serialized.index("2026-09-30")


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_hash_rejects_non_finite_float_values(bad_value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        content_hash([_record("600000.SH", close=bad_value)])


def test_hash_normalizes_float_precision_to_ten_decimal_places() -> None:
    low_precision = [_record("600000.SH", close=7.32)]
    equivalent_precision = [_record("600000.SH", close=7.32000000004)]

    assert content_hash(low_precision) == content_hash(equivalent_precision)
    assert '"close":"7.3200000000"' in canonical_serialize(low_precision)


def test_hash_rejects_negative_zero() -> None:
    with pytest.raises(ValueError, match="negative zero"):
        content_hash([_record("600000.SH", close=-0.0)])


def test_hash_is_stable_for_repeated_serialization() -> None:
    batch = [_record("600000.SH"), _record("000001.SZ", close=12.5)]

    assert canonical_serialize(batch) == canonical_serialize(batch)
    assert content_hash(batch) == content_hash(batch)
