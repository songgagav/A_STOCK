from __future__ import annotations

from data_sources.adapters import RawBatch
from data_sources.network import (
    BaostockAdapter,
    MootdxAdapter,
    ZZShareAdapter,
)
from data_sources.normalize import normalize_mootdx, normalize_zzshare


def _raw_rows(trade_day: str, symbols: list[str]) -> list[dict[str, object]]:
    return [
        {
            "code": symbol,
            "date": trade_day,
            "open": "7.28",
            "high": "7.35",
            "low": "7.25",
            "close": "7.32",
            "volume": "12345600",
            "amount": "90234567.89",
        }
        for symbol in symbols
    ]


def test_baostock_adapter_wraps_injected_network_transport() -> None:
    calls: list[tuple[str, list[str]]] = []

    def transport(trade_day: str, symbols: list[str]) -> list[dict[str, object]]:
        calls.append((trade_day, symbols))
        return _raw_rows(trade_day, symbols)

    batch = BaostockAdapter(transport=transport).fetch(
        "2026-09-30", ["600000.SH"]
    )

    assert isinstance(batch, RawBatch)
    assert batch.source == "baostock"
    assert batch.records[0]["close"] == "7.32"
    assert calls == [("2026-09-30", ["600000.SH"])]


def test_mootdx_adapter_wraps_injected_network_transport() -> None:
    def transport(trade_day: str, symbols: list[str]) -> list[dict[str, object]]:
        return [
            {
                "symbol": symbols[0],
                "datetime": trade_day,
                "open": 7.28,
                "high": 7.35,
                "low": 7.25,
                "close": 7.32,
                "vol": 12345600,
                "amount": 90234567.89,
            }
        ]

    batch = MootdxAdapter(transport=transport).fetch(
        "2026-09-30", ["600000.SH"]
    )

    assert batch.source == "mootdx"
    assert batch.records[0]["close"] == 7.32


def test_zzshare_adapter_wraps_injected_network_transport() -> None:
    def transport(trade_day: str, symbols: list[str]) -> list[dict[str, object]]:
        return [
            {
                "ts_code": symbols[0],
                "trade_date": trade_day.replace("-", ""),
                "open": 7.28,
                "high": 7.35,
                "low": 7.25,
                "close": 7.32,
                "vol": 12345600,
                "amount": 90234567.89,
            }
        ]

    batch = ZZShareAdapter(transport=transport).fetch(
        "2026-09-30", ["600000.SH"]
    )

    assert batch.source == "zzshare"
    assert batch.records[0]["close"] == 7.32


def test_network_normalizers_produce_same_canonical_units() -> None:
    mootdx = normalize_mootdx(
        {
            "symbol": "600000",
            "datetime": "2026-09-30",
            "open": 7.28,
            "high": 7.35,
            "low": 7.25,
            "close": 7.32,
            "vol": 12345600,
            "amount": 90234567.89,
        }
    )
    zzshare = normalize_zzshare(
        {
            "ts_code": "600000.SH",
            "trade_date": "20260930",
            "open": 7.28,
            "high": 7.35,
            "low": 7.25,
            "close": 7.32,
            "vol": 12345600,
            "amount": 90234567.89,
        }
    )

    assert mootdx == zzshare
