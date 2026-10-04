from __future__ import annotations

import math

import pytest

from src.data_sources.batch_hash import canonical_serialize, content_hash
from src.data_sources.metadata import build_metadata
from src.data_sources.staging import (
    load_staged,
    manifest_path,
    metadata_path,
    stage_batch,
    staging_path,
)


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


def _metadata_for(batch: list[dict[str, object]]) -> dict[str, object]:
    return build_metadata(
        source="baostock",
        source_tier="backup",
        trade_day=str(batch[0]["trade_day"]),
        coverage=1.0,
        retrieved_at="2026-10-04T12:00:00Z",
        quality="passed",
        input_hash="a" * 64,
        execution_allowed=False,
        ingest_status="staged",
    )


def test_staging_writes_one_canonical_jsonl_record_per_line(tmp_path) -> None:
    batch = [_record("600000.SH"), _record("000001.SZ", close=12.5)]

    result = stage_batch(tmp_path, _metadata_for(batch), batch)

    staged_file = staging_path(tmp_path, result["batch_id"])
    lines = staged_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all('"symbol"' in line for line in lines)
    assert result["row_count"] == 2
    assert result["content_hash"] == content_hash(batch)


def test_staging_creates_metadata_only_after_staging_succeeds(tmp_path) -> None:
    batch = [_record("600000.SH")]

    result = stage_batch(tmp_path, _metadata_for(batch), batch)
    metadata = load_staged(tmp_path, result["batch_id"])[0]

    assert metadata_path(tmp_path, result["batch_id"]).exists()
    assert manifest_path(tmp_path, result["batch_id"]).parent.exists()
    assert set(metadata) == {
        "batch_id",
        "source",
        "source_tier",
        "trade_day",
        "coverage",
        "retrieved_at",
        "quality",
        "input_hash",
        "execution_allowed",
        "ingest_status",
    }
    assert metadata["ingest_status"].value == "staged"


def test_staging_rejects_metadata_that_is_not_staged(tmp_path) -> None:
    batch = [_record("600000.SH")]
    metadata = _metadata_for(batch)
    metadata["ingest_status"] = "committed"

    with pytest.raises(ValueError, match="staged"):
        stage_batch(tmp_path, metadata, batch)


def test_staging_failure_leaves_no_final_file_or_metadata(tmp_path, monkeypatch) -> None:
    batch = [_record("600000.SH")]

    def fail_replace(source, destination):
        if str(destination).endswith(".jsonl"):
            raise OSError("simulated staging replace failure")
        return original_replace(source, destination)

    import src.data_sources.staging as staging_module

    original_replace = staging_module.os.replace
    monkeypatch.setattr(staging_module.os, "replace", fail_replace)

    metadata = _metadata_for(batch)
    with pytest.raises(OSError, match="staging replace failure"):
        stage_batch(tmp_path, metadata, batch)

    batch_id = metadata["batch_id"]
    assert not staging_path(tmp_path, batch_id).exists()
    assert not metadata_path(tmp_path, batch_id).exists()
