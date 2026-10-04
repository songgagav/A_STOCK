"""Dependency-free contracts at the raw data-source boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


RawRecord = dict[str, Any]
CanonicalRecord = dict[str, Any]
CanonicalBatch = list[CanonicalRecord]


@dataclass(frozen=True, slots=True)
class RawBatch:
    """Raw source output before normalization or quality metadata exists."""

    records: list[RawRecord]
    retrieved_at: str
    source: str


class DataSourceAdapter(Protocol):
    """Minimal adapter contract; transports are injected by implementations."""

    def fetch(self, trade_day: str, symbols: list[str]) -> RawBatch:
        ...

    def name(self) -> str:
        ...
