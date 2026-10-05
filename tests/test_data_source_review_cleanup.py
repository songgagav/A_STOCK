from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os

from data_sources.batch_hash import content_hash
from data_sources.commit import CommitResult, ProbeResult, commit_staged
from data_sources.metadata import (
    IngestStatus,
    QualityStatus,
    deserialize_metadata,
    serialize_metadata,
    transition_status,
)
from data_sources.reconcile import (
    cleanup_staging,
    mark_occupied_unknown,
    resolve_occupied_unknown,
)
from data_sources.staging import load_staged, stage_batch, staging_path, metadata_path


def _batch() -> list[dict[str, object]]:
    return [{
        "symbol": "600000.SH",
        "trade_day": "2026-09-30",
        "open": 7.28,
        "high": 7.35,
        "low": 7.25,
        "close": 7.32,
        "volume": 12345600,
        "amount": 90234567.89,
        "adj_factor": 1.0,
    }]


def _metadata(batch: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source": "baostock",
        "source_tier": "backup",
        "trade_day": "2026-09-30",
        "coverage": 1.0,
        "retrieved_at": "2026-10-04T12:00:00Z",
        "quality": QualityStatus.PASSED,
        "input_hash": content_hash(batch),
        "execution_allowed": False,
        "ingest_status": IngestStatus.STAGED,
    }


def test_occupied_unknown_can_be_resolved_by_explicit_human_review(tmp_path) -> None:
    batch = _batch()
    staged = stage_batch(tmp_path, _metadata(batch), batch)
    mark_occupied_unknown(tmp_path, staged["batch_id"], reason="existing data differs")

    result = resolve_occupied_unknown(
        tmp_path,
        staged["batch_id"],
        reviewer="ops",
        verdict="confirmed_match",
        comment="Probe matched the staged canonical payload.",
        probe=type("Probe", (), {
            "probe": lambda self, day, symbols: ProbeResult(
                True, len(batch), content_hash(batch)
            )
        })(),
    )

    assert result["status"] == "committed"
    assert result["reviewer"] == "ops"
    assert json.loads(result["review_path"].read_text(encoding="utf-8"))["verdict"] == "confirmed_match"


def test_unmatched_probe_automatically_creates_review_marker(tmp_path) -> None:
    batch = _batch()
    staged = stage_batch(tmp_path, _metadata(batch), batch)

    class Sink:
        def commit(self, _batch):
            return CommitResult("committed", len(batch))

    result = commit_staged(
        tmp_path,
        staged["batch_id"],
        Sink(),
        type("Probe", (), {
            "probe": lambda self, day, symbols: ProbeResult(True, 99, "f" * 64)
        })(),
    )

    assert result["status"] == "occupied_unknown"
    review_path = tmp_path / "data" / "h5i" / "reconcile_reviews" / f'{staged["batch_id"]}.json'
    assert review_path.exists()


def test_occupied_unknown_rejection_marks_batch_failed(tmp_path) -> None:
    batch = _batch()
    staged = stage_batch(tmp_path, _metadata(batch), batch)
    mark_occupied_unknown(tmp_path, staged["batch_id"], reason="manual reject")

    result = resolve_occupied_unknown(
        tmp_path,
        staged["batch_id"],
        reviewer="ops",
        verdict="rejected",
        comment="Existing data is not this batch.",
        probe=None,
    )

    metadata = deserialize_metadata(
        metadata_path(tmp_path, staged["batch_id"]).read_text(encoding="utf-8")
    )
    assert result["status"] == "failed"
    assert metadata_path(tmp_path, staged["batch_id"]).exists()
    assert metadata["ingest_status"] is IngestStatus.FAILED


def test_staging_cleanup_removes_old_terminal_payload_but_keeps_staged(tmp_path) -> None:
    old_batch = _batch()
    old = stage_batch(tmp_path, _metadata(old_batch), old_batch)
    committed = transition_status(load_staged(tmp_path, old["batch_id"])[0], IngestStatus.COMMITTED)
    metadata_path(tmp_path, old["batch_id"]).write_text(
        serialize_metadata(committed), encoding="utf-8"
    )
    old_time = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    os.utime(staging_path(tmp_path, old["batch_id"]), (old_time, old_time))

    staged_batch = _batch()
    staged = stage_batch(
        tmp_path,
        {
            **_metadata(staged_batch),
            "source": "mootdx",
            "retrieved_at": "2026-09-01T12:00:01Z",
        },
        staged_batch,
    )
    os.utime(staging_path(tmp_path, staged["batch_id"]), (old_time, old_time))

    report = cleanup_staging(
        tmp_path,
        now=datetime(2026, 10, 5, tzinfo=timezone.utc),
        retention_days=30,
    )

    assert report["deleted"] == 1
    assert not staging_path(tmp_path, old["batch_id"]).exists()
    assert staging_path(tmp_path, staged["batch_id"]).exists()


def test_review_marker_rejects_path_traversal_batch_id(tmp_path) -> None:
    import pytest

    with pytest.raises(ValueError, match="filename-safe"):
        mark_occupied_unknown(tmp_path, "../escape", reason="invalid id")
