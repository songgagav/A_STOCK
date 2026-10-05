from __future__ import annotations

from data_sources.adapters import RawBatch
from data_sources.production import run_daily_source_router
from data_sources.staging import staging_path


class _Adapter:
    def __init__(self, source: str) -> None:
        self.source = source

    def fetch(self, trade_day: str, symbols: list[str]) -> RawBatch:
        return RawBatch(
            records=[
                {
                    "code": symbols[0],
                    "date": trade_day,
                    "open": "7.28",
                    "high": "7.35",
                    "low": "7.25",
                    "close": "7.32",
                    "volume": "12345600",
                    "amount": "90234567.89",
                }
            ],
            retrieved_at="2026-10-04T12:00:00Z",
            source=self.source,
        )


def test_shadow_router_stages_quality_checked_batch_without_h5i_write(tmp_path) -> None:
    result = run_daily_source_router(
        tmp_path,
        "2026-09-30",
        symbols=["600000.SH"],
        source_names=["baostock"],
        mode="shadow",
        adapters={"baostock": _Adapter("baostock")},
    )

    assert result["status"] == "shadow_staged"
    assert staging_path(tmp_path, result["batch_id"]).exists()


def test_enforce_requires_explicit_confirmation(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATA_SOURCE_ROUTER_CONFIRM", raising=False)
    result = run_daily_source_router(
        tmp_path,
        "2026-09-30",
        symbols=["600000.SH"],
        source_names=["baostock"],
        mode="enforce",
        adapters={"baostock": _Adapter("baostock")},
    )

    assert result == {"status": "blocked", "reason": "enforce requires explicit confirmation"}
