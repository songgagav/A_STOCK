from __future__ import annotations

import json
import math

import pytest

from src.data_sources.batch_hash import canonical_serialize, content_hash
from src.data_sources.commit import (
    CommitResult,
    ProbeResult,
    commit_staged,
    reconcile_staged,
)
from src.data_sources.metadata import build_metadata, deserialize_metadata
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


def _metadata_for(
    batch: list[dict[str, object]],
    *,
    input_hash: str = "a" * 64,
    retrieved_at: str = "2026-10-04T12:00:00Z",
) -> dict[str, object]:
    return build_metadata(
        source="baostock",
        source_tier="backup",
        trade_day=str(batch[0]["trade_day"]),
        coverage=1.0,
        retrieved_at=retrieved_at,
        quality="passed",
        input_hash=input_hash,
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


def test_staging_round_trip_preserves_canonical_numeric_types(tmp_path) -> None:
    batch = [_record("600000.SH", close=7.32)]

    result = stage_batch(tmp_path, _metadata_for(batch), batch)
    _, loaded_batch = load_staged(tmp_path, result["batch_id"])

    assert loaded_batch[0]["close"] == 7.32
    assert isinstance(loaded_batch[0]["close"], float)
    assert isinstance(loaded_batch[0]["volume"], int)


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


class _FakeSink:
    def __init__(self, result: CommitResult) -> None:
        self.result = result
        self.calls: list[list[dict[str, object]]] = []

    def commit(self, batch):
        self.calls.append(batch)
        return self.result


class _FakeProbe:
    def __init__(self, result: ProbeResult) -> None:
        self.result = result
        self.calls: list[tuple[str, list[str]]] = []

    def probe(self, trade_day: str, symbols: list[str]) -> ProbeResult:
        self.calls.append((trade_day, symbols))
        return self.result


def _stage_for_commit(tmp_path, batch=None):
    batch = batch or [_record("600000.SH"), _record("000001.SZ", close=12.5)]
    result = stage_batch(tmp_path, _metadata_for(batch), batch)
    return result, batch


def test_commit_success_publishes_self_contained_manifest(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    sink = _FakeSink(CommitResult("committed", len(batch)))
    probe = _FakeProbe(ProbeResult(False, 0, None))

    outcome = commit_staged(tmp_path, result["batch_id"], sink, probe)

    manifest = json.loads(result["manifest_path"].read_text(encoding="utf-8"))
    assert outcome["status"] == "committed"
    assert manifest["batch_id"] == result["batch_id"]
    assert manifest["trade_day"] == "2026-09-30"
    assert manifest["source_tier"] == "backup"
    assert manifest["row_count"] == len(batch)
    assert manifest["content_hash"] == content_hash(batch)
    assert deserialize_metadata(result["metadata_path"].read_text(encoding="utf-8"))["ingest_status"].value == "committed"
    assert len(sink.calls) == 1
    assert probe.calls == [("2026-09-30", ["000001.SZ", "600000.SH"])]


def test_commit_failure_transitions_metadata_without_manifest(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    sink = _FakeSink(CommitResult("failed", 0, "write rejected"))
    probe = _FakeProbe(ProbeResult(False, 0, None))

    outcome = commit_staged(tmp_path, result["batch_id"], sink, probe)

    assert outcome["status"] == "failed"
    assert outcome["error"] == "write rejected"
    assert not result["manifest_path"].exists()
    metadata = deserialize_metadata(result["metadata_path"].read_text(encoding="utf-8"))
    assert metadata["ingest_status"].value == "failed"


def test_commit_unknown_keeps_metadata_staged_and_no_manifest(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    sink = _FakeSink(CommitResult("unknown", len(batch), "connection lost after write"))
    probe = _FakeProbe(ProbeResult(False, 0, None))

    outcome = commit_staged(tmp_path, result["batch_id"], sink, probe)

    assert outcome["status"] == "unknown"
    assert not result["manifest_path"].exists()
    metadata = deserialize_metadata(result["metadata_path"].read_text(encoding="utf-8"))
    assert metadata["ingest_status"].value == "staged"


def test_commit_does_not_publish_manifest_before_sink_success(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)

    class InspectingSink(_FakeSink):
        def commit(self, staged_batch):
            assert not result["manifest_path"].exists()
            return super().commit(staged_batch)

    sink = InspectingSink(CommitResult("committed", len(batch)))
    probe = _FakeProbe(ProbeResult(False, 0, None))

    commit_staged(tmp_path, result["batch_id"], sink, probe)
    assert result["manifest_path"].exists()


def test_commit_blocks_existing_unmatched_h5i_data(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    sink = _FakeSink(CommitResult("committed", len(batch)))
    probe = _FakeProbe(ProbeResult(True, 1, "b" * 64))

    outcome = commit_staged(tmp_path, result["batch_id"], sink, probe)

    assert outcome["status"] == "occupied_unknown"
    assert len(sink.calls) == 0
    assert not result["manifest_path"].exists()


def test_commit_exact_probe_match_is_idempotent_without_second_sink_call(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    sink = _FakeSink(CommitResult("committed", len(batch)))
    first_probe = _FakeProbe(ProbeResult(False, 0, None))
    commit_staged(tmp_path, result["batch_id"], sink, first_probe)

    second_sink = _FakeSink(CommitResult("committed", len(batch)))
    matching_probe = _FakeProbe(ProbeResult(True, len(batch), content_hash(batch)))
    outcome = commit_staged(tmp_path, result["batch_id"], second_sink, matching_probe)

    assert outcome["status"] == "committed"
    assert len(second_sink.calls) == 0


def test_reconcile_exact_probe_match_commits_orphaned_staging(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    probe = _FakeProbe(ProbeResult(True, len(batch), content_hash(batch)))

    outcome = reconcile_staged(tmp_path, result["batch_id"], probe)

    assert outcome["outcome"] == "committed"
    assert outcome["content_hash"] == content_hash(batch)
    assert result["manifest_path"].exists()
    metadata = deserialize_metadata(result["metadata_path"].read_text(encoding="utf-8"))
    assert metadata["ingest_status"].value == "committed"


def test_reconcile_row_count_or_hash_mismatch_remains_staged(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    probe = _FakeProbe(ProbeResult(True, len(batch) + 1, content_hash(batch)))

    outcome = reconcile_staged(tmp_path, result["batch_id"], probe)

    assert outcome["outcome"] == "occupied_unknown"
    assert not result["manifest_path"].exists()
    assert deserialize_metadata(result["metadata_path"].read_text(encoding="utf-8"))["ingest_status"].value == "staged"


def test_reconcile_without_existing_data_remains_staged(tmp_path) -> None:
    result, _ = _stage_for_commit(tmp_path)
    probe = _FakeProbe(ProbeResult(False, 0, None))

    outcome = reconcile_staged(tmp_path, result["batch_id"], probe)

    assert outcome["outcome"] == "still_staged"
    assert not result["manifest_path"].exists()


def test_reconcile_same_batch_retry_is_idempotent(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    matching_probe = _FakeProbe(ProbeResult(True, len(batch), content_hash(batch)))

    first = reconcile_staged(tmp_path, result["batch_id"], matching_probe)
    second = reconcile_staged(tmp_path, result["batch_id"], matching_probe)

    assert first["outcome"] == "committed"
    assert second["outcome"] == "committed"
    assert result["manifest_path"].exists()


def test_reconcile_round_trip_probe_hash_matches_staged_batch(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)
    stored: list[dict[str, object]] = []

    class RoundTripProbe:
        def probe(self, trade_day: str, symbols: list[str]) -> ProbeResult:
            stored.extend(load_staged(tmp_path, result["batch_id"])[1])
            return ProbeResult(True, len(stored), content_hash(stored))

    outcome = reconcile_staged(tmp_path, result["batch_id"], RoundTripProbe())

    assert outcome["outcome"] == "committed"


def test_reconcile_lock_is_released_after_sink_exception(tmp_path) -> None:
    result, batch = _stage_for_commit(tmp_path)

    class RaisingSink:
        def commit(self, staged_batch):
            raise RuntimeError("simulated sink crash")

    probe = _FakeProbe(ProbeResult(False, 0, None))
    first = commit_staged(tmp_path, result["batch_id"], RaisingSink(), probe)
    assert first["status"] == "unknown"

    second_sink = _FakeSink(CommitResult("committed", len(batch)))
    second = commit_staged(tmp_path, result["batch_id"], second_sink, probe)
    assert second["status"] == "committed"


def test_reconcile_second_batch_for_same_trade_day_is_rejected(tmp_path) -> None:
    first_batch = [_record("600000.SH")]
    first_result = stage_batch(tmp_path, _metadata_for(first_batch), first_batch)
    sink = _FakeSink(CommitResult("committed", 1))
    probe = _FakeProbe(ProbeResult(False, 0, None))
    commit_staged(tmp_path, first_result["batch_id"], sink, probe)

    second_batch = [_record("000001.SZ")]
    second_metadata = _metadata_for(
        second_batch,
        input_hash="b" * 64,
        retrieved_at="2026-10-04T13:00:00Z",
    )
    second_result = stage_batch(tmp_path, second_metadata, second_batch)
    outcome = commit_staged(tmp_path, second_result["batch_id"], sink, probe)

    assert outcome["status"] == "occupied_unknown"
    assert len(sink.calls) == 1
