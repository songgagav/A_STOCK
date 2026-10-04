"""Production h5i sink and content probe for canonical bar batches.

The existing ``h5i_sync.append_daily_bars`` path remains the single writer for
``daily_bars``.  This module only adapts the Batch 4 sink/probe contracts to
that path; it does not add a second writer or change the h5i schema.

``daily_bars`` currently stores bare six-digit symbols and has no ``adj_factor``
or ``batch_id`` columns.  The adapter therefore refuses non-unit adjustment
factors instead of silently dropping them.  Batch identity and provenance
remain in the sidecar metadata/manifest files managed by ``staging.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
import importlib
import os
from pathlib import Path
import re
from typing import Any

import pandas as pd

from .adapters import CanonicalBatch
from .batch_hash import content_hash
from .commit import CommitResult, ProbeResult


class H5IUnavailableError(RuntimeError):
    """The h5i runtime or database is unavailable; callers must fail closed."""


_CANONICAL_SYMBOL_RE = re.compile(r"^[0-9]{6}(?:\.(?:SH|SZ|BJ))?$")


def _default_h5i_path() -> Path:
    configured = os.environ.get("H5I_MARKET_DB")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data" / "h5i" / "market.db"


def _h5i_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not _CANONICAL_SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"invalid canonical symbol: {symbol!r}")
    return symbol.split(".", 1)[0]


def _canonical_symbol(symbol: Any) -> str:
    value = str(symbol).strip().upper()
    if "." in value:
        return value
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError(f"invalid h5i symbol: {symbol!r}")
    if value.startswith("6"):
        return f"{value}.SH"
    if value.startswith(("0", "3")):
        return f"{value}.SZ"
    if value.startswith(("4", "8")):
        return f"{value}.BJ"
    raise ValueError(f"cannot infer exchange for h5i symbol: {value!r}")


def _validate_trade_day(trade_day: str) -> str:
    if not isinstance(trade_day, str):
        raise ValueError("trade_day must be YYYY-MM-DD")
    try:
        return date.fromisoformat(trade_day).isoformat()
    except ValueError as exc:
        raise ValueError("trade_day must be YYYY-MM-DD") from exc


def _batch_frame(batch: CanonicalBatch) -> pd.DataFrame:
    """Map canonical rows to the pre-existing h5i_sync input contract."""
    if not batch:
        raise ValueError("cannot commit an empty canonical batch")
    for row in batch:
        if row.get("adj_factor") != 1.0:
            raise ValueError(
                "daily_bars schema has no adj_factor; only adj_factor=1.0 "
                "can be written without loss"
            )
    days = {row.get("trade_day") for row in batch}
    if len(days) != 1:
        raise ValueError("one h5i commit batch must contain exactly one trade_day")

    # content_hash validates every canonical field before the writer is called.
    content_hash(batch)
    return pd.DataFrame(
        [
            {
                "symbol": _h5i_symbol(str(row["symbol"])),
                "date": row["trade_day"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "amount": row["amount"],
                # Existing h5i schema permits these fields to be NaN when the
                # source adapter did not provide them.
                "change_pct": float("nan"),
                "turnover": float("nan"),
            }
            for row in batch
        ]
    )


def _default_writer(frame: pd.DataFrame) -> Mapping[str, Any]:
    """Use the repository's existing guarded, monotonic h5i writer."""
    try:
        importlib.import_module("h5i_db")
    except Exception as exc:  # noqa: BLE001 - convert to an explicit gate result
        raise H5IUnavailableError(f"h5i runtime unavailable: {exc}") from exc

    try:
        import bars_ingest

        result = bars_ingest.write_bars(
            frame,
            "stockdb_sdk",  # frame is already canonical/h5i-shaped
            dry_run=False,
            min_rows_per_day=0,
        )
    except Exception as exc:  # noqa: BLE001 - sink caller records unknown outcome
        raise H5IUnavailableError(f"h5i write path unavailable: {exc}") from exc

    if not result.get("ok"):
        return result
    if int(result.get("appended") or 0) != len(frame):
        return {
            **result,
            "ok": False,
            "error": (
                f"h5i writer appended {result.get('appended', 0)} rows, "
                f"expected {len(frame)}; skipped rows are not a commit"
            ),
        }
    return result


class H5ICommitSink:
    """Adapt canonical batches to the existing h5i append path."""

    def __init__(
        self,
        *,
        writer: Callable[[pd.DataFrame], Mapping[str, Any]] | None = None,
    ) -> None:
        self._writer = writer or _default_writer

    def commit(self, batch: CanonicalBatch) -> CommitResult:
        try:
            frame = _batch_frame(batch)
        except Exception as exc:  # noqa: BLE001 - validation is a failed write
            return CommitResult("failed", 0, str(exc))
        try:
            result = dict(self._writer(frame))
        except H5IUnavailableError as exc:
            return CommitResult("failed", 0, str(exc))
        except Exception as exc:  # noqa: BLE001 - write completion is unknown
            return CommitResult("unknown", 0, str(exc))

        appended = int(result.get("appended") or 0)
        if not result.get("ok"):
            return CommitResult("failed", appended, str(result.get("error") or "h5i write failed"))
        if appended != len(frame):
            return CommitResult(
                "unknown",
                appended,
                f"h5i writer appended {appended} rows, expected {len(frame)}",
            )
        return CommitResult("committed", appended)


def _default_db_factory(path: Path):
    try:
        module = importlib.import_module("h5i_db")
    except Exception as exc:  # noqa: BLE001 - caller gets one typed failure
        raise H5IUnavailableError(f"h5i runtime unavailable: {exc}") from exc
    if not path.exists():
        raise H5IUnavailableError(f"h5i database path does not exist: {path}")
    try:
        return module.Database(str(path))
    except Exception as exc:  # noqa: BLE001
        raise H5IUnavailableError(f"h5i database could not be opened: {exc}") from exc


class H5IContentProbe:
    """Read a date/symbol slice from h5i and compare the canonical content hash."""

    def __init__(
        self,
        *,
        path: str | os.PathLike[str] | None = None,
        db_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else _default_h5i_path()
        self._db_factory = db_factory or (lambda: _default_db_factory(self.path))

    def probe(self, trade_day: str, symbols: list[str]) -> ProbeResult:
        normalized_day = _validate_trade_day(trade_day)
        normalized_symbols = sorted({_h5i_symbol(symbol) for symbol in symbols})
        if not normalized_symbols:
            return ProbeResult(False, 0, None)
        quoted = ",".join("'" + symbol.replace("'", "''") + "'" for symbol in normalized_symbols)
        query = (
            "SELECT CAST(ts AS DATE) AS trade_day, symbol, open, high, low, "
            "close, volume, amount FROM daily_bars "
            f"WHERE CAST(ts AS DATE) = DATE '{normalized_day}' "
            f"AND symbol IN ({quoted}) ORDER BY symbol"
        )
        try:
            db = self._db_factory()
        except H5IUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize injected/runtime failures
            raise H5IUnavailableError(f"h5i database unavailable: {exc}") from exc
        try:
            frame = db.sql(query).to_pandas()
            if frame is None or len(frame) == 0:
                return ProbeResult(False, 0, None)
            records = []
            for row in frame.to_dict("records"):
                records.append(
                    {
                        "symbol": _canonical_symbol(row["symbol"]),
                        "trade_day": normalized_day,
                        "open": row["open"],
                        "high": row["high"],
                        "low": row["low"],
                        "close": row["close"],
                        "volume": row["volume"],
                        "amount": row["amount"],
                        # No adj_factor column exists in daily_bars; the sink
                        # only writes unit-factor batches, so round-trip with 1.
                        "adj_factor": 1.0,
                    }
                )
            return ProbeResult(True, len(records), content_hash(records))
        finally:
            try:
                db.close()
            except Exception:
                pass


__all__ = [
    "H5ICommitSink",
    "H5IContentProbe",
    "H5IUnavailableError",
]
