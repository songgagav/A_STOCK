from __future__ import annotations

from typing import Any

from src.data_sources.adapters import DataSourceAdapter, RawBatch
from src.data_sources.metadata import QualityStatus
from src.data_sources.normalize import normalize_baostock, normalize_baostock_batch
from src.data_sources.quality import check_quality


BAOSTOCK_RAW = {
    "code": "sh.600000",
    "date": "2026-09-30",
    "open": "7.28",
    "high": "7.35",
    "low": "7.25",
    "close": "7.32",
    "volume": "12345600",
    "amount": "90234567.89",
}


def test_fake_adapter_returns_raw_batch_without_metadata() -> None:
    class FakeAdapter:
        def name(self) -> str:
            return "fake"

        def fetch(self, trade_day: str, symbols: list[str]) -> RawBatch:
            return RawBatch(
                records=[{"symbol": symbol} for symbol in symbols],
                retrieved_at="2026-10-04T08:00:00Z",
                source=self.name(),
            )

    adapter: DataSourceAdapter = FakeAdapter()
    batch = adapter.fetch("2026-09-30", ["600000.SH"])

    assert adapter.name() == "fake"
    assert batch.records == [{"symbol": "600000.SH"}]
    assert batch.retrieved_at == "2026-10-04T08:00:00Z"
    assert batch.source == "fake"
    assert not hasattr(batch, "metadata")


def test_normalize_baostock_raw_to_canonical() -> None:
    canonical = normalize_baostock(BAOSTOCK_RAW)

    assert canonical["symbol"] == "600000.SH"
    assert canonical["trade_day"] == "2026-09-30"
    assert canonical["open"] == 7.28
    assert canonical["high"] == 7.35
    assert canonical["low"] == 7.25
    assert canonical["close"] == 7.32
    assert canonical["volume"] == 12345600
    assert canonical["amount"] == 90234567.89


def test_normalize_baostock_batch_maps_raw_batch_to_canonical_batch() -> None:
    raw_batch = RawBatch(
        records=[BAOSTOCK_RAW],
        retrieved_at="2026-10-04T08:00:00Z",
        source="baostock",
    )

    canonical_batch = normalize_baostock_batch(raw_batch)

    assert len(canonical_batch) == 1
    assert canonical_batch[0]["symbol"] == "600000.SH"
    assert canonical_batch[0]["volume"] == 12345600


def test_normalize_preserves_unadjusted_prices_and_defaults_adj_factor() -> None:
    canonical = normalize_baostock(BAOSTOCK_RAW)

    assert canonical["adj_factor"] == 1.0
    assert set(canonical) == {
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


def test_normalize_does_not_apply_quality_rules() -> None:
    raw = {**BAOSTOCK_RAW, "low": "8.00", "high": "7.00", "close": "-1.00"}

    canonical = normalize_baostock(raw)

    assert canonical["low"] == 8.0
    assert canonical["high"] == 7.0
    assert canonical["close"] == -1.0


def test_quality_accepts_valid_canonical_batch_and_reports_coverage() -> None:
    canonical = normalize_baostock(BAOSTOCK_RAW)

    report = check_quality(
        [canonical],
        expected_symbols=["600000.SH", "000001.SZ"],
        expected_trade_day="2026-09-30",
    )

    assert report.status is QualityStatus.PASSED
    assert report.coverage == 0.5
    assert report.reasons == ()
    assert report.checked_rows == 1


def test_quality_rejects_inconsistent_ohlc_without_normalizer_involvement() -> None:
    canonical = normalize_baostock(
        {**BAOSTOCK_RAW, "low": "8.00", "high": "7.00"}
    )

    report = check_quality([canonical])

    assert report.status is QualityStatus.FAILED
    assert "row[0].ohlc_inconsistent" in report.reasons


def test_quality_rejects_non_positive_close_and_negative_volume() -> None:
    canonical = normalize_baostock(
        {**BAOSTOCK_RAW, "close": "0", "volume": "-1"}
    )

    report = check_quality([canonical])

    assert report.status is QualityStatus.FAILED
    assert "row[0].close_non_positive" in report.reasons
    assert "row[0].volume_negative" in report.reasons


def test_quality_rejects_trade_day_mismatch() -> None:
    canonical = normalize_baostock(BAOSTOCK_RAW)

    report = check_quality([canonical], expected_trade_day="2026-10-01")

    assert report.status is QualityStatus.FAILED
    assert "row[0].trade_day_mismatch" in report.reasons


def test_quality_rejects_missing_required_field() -> None:
    canonical: dict[str, Any] = normalize_baostock(BAOSTOCK_RAW)
    canonical.pop("amount")

    report = check_quality([canonical])

    assert report.status is QualityStatus.FAILED
    assert "row[0].missing:amount" in report.reasons
