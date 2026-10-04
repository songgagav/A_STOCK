from __future__ import annotations

import pandas as pd
import pytest

from src.data_sources.batch_hash import content_hash
from src.data_sources.commit import CommitResult, ProbeResult
from src.data_sources.h5i import H5IContentProbe, H5IUnavailableError, H5ICommitSink


def _batch(*, adj_factor: float = 1.0) -> list[dict[str, object]]:
    return [
        {
            "symbol": "600000.SH",
            "trade_day": "2026-09-30",
            "open": 7.28,
            "high": 7.35,
            "low": 7.25,
            "close": 7.32,
            "volume": 12_345_600,
            "amount": 90_234_567.89,
            "adj_factor": adj_factor,
        },
        {
            "symbol": "000001.SZ",
            "trade_day": "2026-09-30",
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 2_000_000,
            "amount": 20_100_000.0,
            "adj_factor": adj_factor,
        },
    ]


class _FakeResult:
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame

    def to_pandas(self) -> pd.DataFrame:
        return self._frame.copy()


class _FakeDB:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.queries: list[str] = []
        self.closed = False

    def sql(self, query: str):
        self.queries.append(query)
        return _FakeResult(self.frame)

    def close(self):
        self.closed = True


def _h5i_frame(batch: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_day": row["trade_day"],
                "symbol": row["symbol"].split(".", 1)[0],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "amount": row["amount"],
            }
            for row in batch
        ]
    )


def test_h5i_sink_maps_canonical_batch_to_existing_daily_bars_contract() -> None:
    seen: dict[str, object] = {}

    def writer(frame: pd.DataFrame) -> dict[str, object]:
        seen["frame"] = frame.copy()
        return {"ok": True, "appended": len(frame)}

    result = H5ICommitSink(writer=writer).commit(_batch())

    assert result == CommitResult("committed", 2)
    frame = seen["frame"]
    assert list(frame.columns) == [
        "symbol", "date", "open", "high", "low", "close",
        "volume", "amount", "change_pct", "turnover",
    ]
    assert frame["symbol"].tolist() == ["600000", "000001"]
    assert frame["date"].tolist() == ["2026-09-30", "2026-09-30"]


def test_h5i_sink_rejects_adjustment_factor_that_daily_bars_cannot_store() -> None:
    result = H5ICommitSink(writer=lambda _frame: {"ok": True, "appended": 2}).commit(
        _batch(adj_factor=1.1)
    )

    assert result.status == "failed"
    assert result.row_count == 0
    assert "adj_factor" in (result.error or "")


def test_h5i_sink_maps_writer_failure_and_exception_explicitly() -> None:
    failed = H5ICommitSink(
        writer=lambda _frame: {"ok": False, "error": "database locked"}
    ).commit(_batch())
    assert failed == CommitResult("failed", 0, "database locked")

    unknown = H5ICommitSink(
        writer=lambda _frame: (_ for _ in ()).throw(RuntimeError("lost connection"))
    ).commit(_batch())
    assert unknown.status == "unknown"
    assert "lost connection" in (unknown.error or "")


def test_h5i_probe_round_trip_uses_canonical_hash_and_closes_database() -> None:
    batch = _batch()
    db = _FakeDB(_h5i_frame(batch))
    probe = H5IContentProbe(db_factory=lambda: db)

    result = probe.probe("2026-09-30", ["600000.SH", "000001.SZ"])

    assert result == ProbeResult(True, 2, content_hash(batch))
    assert "CAST(ts AS DATE)" in db.queries[0]
    assert "600000" in db.queries[0]
    assert db.closed is True


def test_h5i_probe_reports_empty_batch_without_hash() -> None:
    db = _FakeDB(pd.DataFrame())
    result = H5IContentProbe(db_factory=lambda: db).probe(
        "2026-09-30", ["600000.SH"]
    )

    assert result == ProbeResult(False, 0, None)
    assert db.closed is True


def test_h5i_probe_fails_explicitly_when_runtime_is_unavailable() -> None:
    def unavailable():
        raise ModuleNotFoundError("No module named 'h5i_db'")

    with pytest.raises(H5IUnavailableError, match="h5i"):
        H5IContentProbe(db_factory=unavailable).probe("2026-09-30", ["600000.SH"])
