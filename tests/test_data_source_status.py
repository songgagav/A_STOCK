from __future__ import annotations

import json

from src.data_sources.metadata import build_metadata, serialize_metadata
from src.data_sources.staging import manifest_path, metadata_path, stage_batch
from src.data_sources.status import read_data_source_status


def _batch() -> list[dict[str, object]]:
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
            "adj_factor": 1.0,
        }
    ]


def _metadata(*, status: str = "staged", source: str = "baostock") -> dict[str, object]:
    return build_metadata(
        source=source,
        source_tier="backup",
        trade_day="2026-09-30",
        coverage=1.0,
        retrieved_at="2026-10-04T12:00:00Z",
        quality="passed",
        input_hash="a" * 64,
        execution_allowed=False,
        ingest_status=status,
    )


def test_status_reports_h5i_blocked_and_staged_batch(tmp_path) -> None:
    stage_batch(tmp_path, _metadata(), _batch())

    result = read_data_source_status(
        tmp_path,
        h5i_check=lambda: {
            "available": False,
            "path": str(tmp_path / "data" / "h5i" / "market.db"),
            "error": "No module named 'h5i_db'",
        },
    )

    assert result["ok"] is True
    assert result["status"] == "blocked"
    assert result["h5i"]["status"] == "unavailable"
    assert result["batches"]["by_ingest_status"] == {"staged": 1}
    assert result["sources"]["by_source"]["baostock"] == 1
    assert result["staging"]["count"] == 1


def test_status_reports_ready_when_h5i_is_available_and_all_committed(tmp_path) -> None:
    metadata = _metadata(status="committed")
    path = metadata_path(tmp_path, metadata["batch_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_metadata({**metadata, "ingest_status": "committed"}), encoding="utf-8")
    manifest = manifest_path(tmp_path, metadata["batch_id"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"batch_id": metadata["batch_id"]}), encoding="utf-8")

    result = read_data_source_status(
        tmp_path,
        h5i_check=lambda: {"available": True, "path": "fake", "error": None},
    )

    assert result["status"] == "ok"
    assert result["h5i"]["status"] == "ready"
    assert result["batches"]["by_ingest_status"] == {"committed": 1}
    assert result["batches"]["latest"]["batch_id"] == metadata["batch_id"]


def test_status_counts_invalid_metadata_without_hiding_it(tmp_path) -> None:
    metadata_dir = tmp_path / "data" / "h5i" / "source_metadata"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "broken.json").write_text("{not-json", encoding="utf-8")

    result = read_data_source_status(
        tmp_path,
        h5i_check=lambda: {"available": True, "path": "fake", "error": None},
    )

    assert result["status"] == "degraded"
    assert result["batches"]["invalid"] == 1
    assert result["batches"]["by_ingest_status"] == {}


def test_status_detects_manifest_metadata_inconsistency(tmp_path) -> None:
    metadata = _metadata(status="staged")
    stage_batch(tmp_path, metadata, _batch())
    manifest = manifest_path(tmp_path, metadata["batch_id"])
    manifest.write_text(
        json.dumps({"batch_id": metadata["batch_id"], "trade_day": "2026-09-30"}),
        encoding="utf-8",
    )

    result = read_data_source_status(
        tmp_path,
        h5i_check=lambda: {"available": True, "path": "fake", "error": None},
    )

    assert result["status"] == "degraded"
    assert result["manifests"]["total"] == 1
    assert result["consistency"]["manifest_for_noncommitted"] == 1
