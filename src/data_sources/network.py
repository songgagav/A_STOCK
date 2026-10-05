"""Optional real-network adapters for the source router.

The adapters keep third-party imports lazy and accept an injected transport.
That makes contract tests deterministic while the default path still uses the
real Baostock, mootdx, and ZZShare clients when installed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Callable, Iterable, Mapping

from .adapters import RawBatch


class OptionalDependencyError(RuntimeError):
    """A selected network adapter is unavailable in the current interpreter."""


Transport = Callable[[str, list[str]], Iterable[Mapping[str, Any]]]


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        value = value.to_dict("records")
    if isinstance(value, Mapping):
        value = [value]
    return [dict(row) for row in value]


def _bare_symbol(symbol: str) -> str:
    return str(symbol).split(".", 1)[0]


class BaostockAdapter:
    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = transport

    def name(self) -> str:
        return "baostock"

    def fetch(self, trade_day: str, symbols: list[str]) -> RawBatch:
        if self._transport is not None:
            return RawBatch(_records(self._transport(trade_day, symbols)), _now_utc(), self.name())
        try:
            from baostock_adapter import make_baostock_fetcher, to_baostock_code
        except Exception as exc:  # noqa: BLE001 - explicit optional dependency boundary
            raise OptionalDependencyError(f"baostock adapter unavailable: {exc}") from exc

        fetcher = make_baostock_fetcher(start=trade_day, end=trade_day, adjustflag="3")
        records: list[dict[str, Any]] = []
        try:
            for symbol in symbols:
                frame = fetcher(to_baostock_code(symbol))
                records.extend(_records(frame))
        finally:
            close = getattr(fetcher, "close", None)
            if close is not None:
                close()
        return RawBatch(records, _now_utc(), self.name())


class MootdxAdapter:
    def __init__(self, *, transport: Transport | None = None, offset: int = 800) -> None:
        self._transport = transport
        self._offset = int(offset)

    def name(self) -> str:
        return "mootdx"

    def fetch(self, trade_day: str, symbols: list[str]) -> RawBatch:
        if self._transport is not None:
            return RawBatch(_records(self._transport(trade_day, symbols)), _now_utc(), self.name())
        try:
            from mootdx.quotes import Quotes
        except Exception as exc:  # noqa: BLE001
            raise OptionalDependencyError(f"mootdx adapter unavailable: {exc}") from exc

        client = Quotes.factory(market="std", multithread=True, heartbeat=True)
        records: list[dict[str, Any]] = []
        try:
            for symbol in symbols:
                frame = client.bars(
                    symbol=_bare_symbol(symbol), frequency=9, start=0, offset=self._offset
                )
                for row in _records(frame):
                    day = str(row.get("datetime", row.get("date", "")))[:10]
                    if day == trade_day:
                        records.append({
                            "symbol": symbol,
                            "datetime": trade_day,
                            "open": row.get("open"),
                            "high": row.get("high"),
                            "low": row.get("low"),
                            "close": row.get("close"),
                            "vol": row.get("vol", row.get("volume")),
                            "amount": row.get("amount"),
                            "adj_factor": row.get("adj_factor", 1.0),
                        })
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                close()
        return RawBatch(records, _now_utc(), self.name())


class ZZShareAdapter:
    def __init__(
        self,
        *,
        transport: Transport | None = None,
        token: str | None = None,
    ) -> None:
        self._transport = transport
        self._token = token if token is not None else os.environ.get("ZZSHARE_TOKEN")

    def name(self) -> str:
        return "zzshare"

    def fetch(self, trade_day: str, symbols: list[str]) -> RawBatch:
        if self._transport is not None:
            return RawBatch(_records(self._transport(trade_day, symbols)), _now_utc(), self.name())
        try:
            from zzshare.client import DataApi
        except Exception as exc:  # noqa: BLE001
            raise OptionalDependencyError(f"zzshare adapter unavailable: {exc}") from exc

        api = DataApi(token=self._token) if self._token else DataApi()
        records: list[dict[str, Any]] = []
        compact_day = trade_day.replace("-", "")
        for symbol in symbols:
            frame = api.daily(
                ts_code=symbol,
                start_date=compact_day,
                end_date=compact_day,
            )
            for row in _records(frame):
                record = dict(row)
                record.setdefault("ts_code", symbol)
                record.setdefault("trade_date", compact_day)
                records.append(record)
        return RawBatch(records, _now_utc(), self.name())


def build_network_adapters() -> dict[str, Any]:
    """Build all adapters without importing optional packages eagerly."""
    return {
        "baostock": BaostockAdapter(),
        "mootdx": MootdxAdapter(),
        "zzshare": ZZShareAdapter(),
    }


__all__ = [
    "BaostockAdapter",
    "MootdxAdapter",
    "OptionalDependencyError",
    "ZZShareAdapter",
    "build_network_adapters",
]
