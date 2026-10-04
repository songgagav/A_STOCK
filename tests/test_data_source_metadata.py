from __future__ import annotations

import json

import pytest

from data_sources.metadata import (
    IngestStatus,
    QualityStatus,
    SourceTier,
    build_metadata,
    deserialize_metadata,
    serialize_metadata,
    transition_status,
)


BASE = {
    "source": "baostock",
    "source_tier": SourceTier.BACKUP,
    "trade_day": "2026-09-30",
    "coverage": 1.0,
    "retrieved_at": "2026-10-04T12:00:00Z",
    "quality": QualityStatus.PASSED,
    "input_hash": "a" * 64,
}


def make_metadata(**overrides):
    values = {**BASE, **overrides}
    return build_metadata(**values)


def test_metadata_has_all_required_fields():
    metadata = make_metadata()

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


@pytest.mark.parametrize("tier", list(SourceTier))
def test_source_tier_accepts_valid_values(tier):
    assert make_metadata(source_tier=tier)["source_tier"] is tier


def test_source_tier_rejects_unknown_string():
    with pytest.raises(ValueError, match="source_tier"):
        make_metadata(source_tier="unknown")


def test_source_tier_is_enum_not_string():
    metadata = make_metadata(source_tier="backup")

    assert isinstance(metadata["source_tier"], SourceTier)
    assert metadata["source_tier"] is SourceTier.BACKUP


def test_quality_status_is_enum():
    metadata = make_metadata(quality="passed")

    assert isinstance(metadata["quality"], QualityStatus)
    assert metadata["quality"] is QualityStatus.PASSED


def test_execution_allowed_defaults_false():
    assert make_metadata()["execution_allowed"] is False


def test_shadow_source_cannot_enable_execution():
    with pytest.raises(ValueError, match="execution_allowed"):
        make_metadata(source_tier=SourceTier.SHADOW, execution_allowed=True)


@pytest.mark.parametrize("coverage", [-0.01, 1.01, True, "0.5"])
def test_coverage_must_be_number_between_zero_and_one(coverage):
    with pytest.raises(ValueError, match="coverage"):
        make_metadata(coverage=coverage)


def test_trade_day_must_be_iso_date():
    with pytest.raises(ValueError, match="trade_day"):
        make_metadata(trade_day="20260930")


def test_retrieved_at_must_be_utc_iso8601_with_z():
    with pytest.raises(ValueError, match="retrieved_at"):
        make_metadata(retrieved_at="2026-10-04T20:00:00+08:00")


def test_batch_id_is_deterministic():
    first = make_metadata()
    second = make_metadata()

    assert first["batch_id"] == second["batch_id"]
    assert first["batch_id"] == (
        "2026-09-30-baostock-20261004T120000Z-aaaaaaaa"
    )


def test_batch_id_changes_when_input_hash_changes():
    first = make_metadata(input_hash="a" * 64)
    second = make_metadata(input_hash="b" * 64)

    assert first["batch_id"] != second["batch_id"]


def test_supplied_batch_id_must_match_deterministic_value():
    with pytest.raises(ValueError, match="batch_id"):
        make_metadata(batch_id="manually-chosen")


def test_stable_serialization_is_sorted_and_compact():
    payload = serialize_metadata(make_metadata())

    assert payload == serialize_metadata(make_metadata())
    assert payload == json.dumps(
        json.loads(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def test_round_trip_preserves_metadata_and_enum_types():
    original = make_metadata()
    restored = deserialize_metadata(serialize_metadata(original))

    assert restored == original
    assert isinstance(restored["source_tier"], SourceTier)
    assert isinstance(restored["quality"], QualityStatus)
    assert isinstance(restored["ingest_status"], IngestStatus)


def test_extra_fields_are_rejected():
    with pytest.raises(ValueError, match="unknown field"):
        make_metadata(unexpected="not allowed")


def test_initial_ingest_status_is_staged():
    assert make_metadata()["ingest_status"] is IngestStatus.STAGED


def test_staged_can_transition_to_committed():
    committed = transition_status(make_metadata(), IngestStatus.COMMITTED)

    assert committed["ingest_status"] is IngestStatus.COMMITTED


def test_staged_can_transition_to_failed():
    failed = transition_status(make_metadata(), IngestStatus.FAILED)

    assert failed["ingest_status"] is IngestStatus.FAILED


@pytest.mark.parametrize("terminal", [IngestStatus.COMMITTED, IngestStatus.FAILED])
def test_terminal_status_cannot_transition_again(terminal):
    metadata = transition_status(make_metadata(), terminal)

    with pytest.raises(ValueError, match="terminal"):
        transition_status(metadata, IngestStatus.COMMITTED)


def test_unknown_status_transition_is_rejected():
    with pytest.raises(ValueError, match="ingest_status"):
        transition_status(make_metadata(), "unknown")


def test_execution_allowed_must_be_boolean():
    with pytest.raises(ValueError, match="execution_allowed"):
        make_metadata(execution_allowed=1)


def test_input_hash_must_be_non_empty_string():
    with pytest.raises(ValueError, match="input_hash"):
        make_metadata(input_hash="")
