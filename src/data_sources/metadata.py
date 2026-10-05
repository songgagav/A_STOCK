"""Pure in-memory metadata contracts for data-source ingestion.

This module intentionally has no filesystem, database, or data-source
dependencies. Persistence and orphaned-staged reconciliation belong to a
later ingestion phase.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
import json
import re
from typing import Any


class SourceTier(str, Enum):
    PRIMARY = "primary"
    BACKUP = "backup"
    SHADOW = "shadow"


class QualityStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class IngestStatus(str, Enum):
    STAGED = "staged"
    COMMITTED = "committed"
    FAILED = "failed"


_REQUIRED_FIELDS = frozenset(
    {
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
)
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TERMINAL_STATUSES = {IngestStatus.COMMITTED, IngestStatus.FAILED}


def _coerce_enum(value: Any, enum_type: type[Enum], field: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in enum_type)
            raise ValueError(f"invalid {field}: {value!r}; expected one of {allowed}") from exc
    raise ValueError(f"{field} must be a {enum_type.__name__}")


def _validate_trade_day(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("trade_day must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("trade_day must be a valid YYYY-MM-DD date") from exc
    return value


def _validate_retrieved_at(value: Any) -> str:
    if not isinstance(value, str) or not _UTC_TIMESTAMP_RE.fullmatch(value):
        raise ValueError("retrieved_at must be ISO8601 UTC ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("retrieved_at must be a valid ISO8601 timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("retrieved_at must be UTC")
    return value


def _validate_source(value: Any) -> str:
    if not isinstance(value, str) or not _SOURCE_RE.fullmatch(value):
        raise ValueError("source must be a non-empty identifier")
    return value


def _validate_coverage(value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("coverage must be a number between 0 and 1")
    if not 0 <= value <= 1:
        raise ValueError("coverage must be a number between 0 and 1")
    return value


def _validate_input_hash(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("input_hash must be a non-empty string")
    return value


def _make_batch_id(trade_day: str, source: str, retrieved_at: str, input_hash: str) -> str:
    compact_timestamp = retrieved_at.replace("-", "").replace(":", "")
    return f"{trade_day}-{source}-{compact_timestamp}-{input_hash[:8]}"


def build_metadata(
    *,
    source: str,
    source_tier: SourceTier | str,
    trade_day: str,
    coverage: float | int,
    retrieved_at: str,
    quality: QualityStatus | str,
    input_hash: str,
    execution_allowed: bool = False,
    ingest_status: IngestStatus | str = IngestStatus.STAGED,
    batch_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build and validate one exact-shape ingestion metadata record."""
    if extra:
        names = ", ".join(sorted(extra))
        raise ValueError(f"unknown field(s): {names}")

    tier = _coerce_enum(source_tier, SourceTier, "source_tier")
    quality_status = _coerce_enum(quality, QualityStatus, "quality")
    status = _coerce_enum(ingest_status, IngestStatus, "ingest_status")
    normalized_source = _validate_source(source)
    normalized_trade_day = _validate_trade_day(trade_day)
    normalized_timestamp = _validate_retrieved_at(retrieved_at)
    normalized_coverage = _validate_coverage(coverage)
    normalized_input_hash = _validate_input_hash(input_hash)

    if type(execution_allowed) is not bool:
        raise ValueError("execution_allowed must be a boolean")
    if tier is SourceTier.SHADOW and execution_allowed:
        raise ValueError("execution_allowed must be false for shadow sources")

    expected_batch_id = _make_batch_id(
        normalized_trade_day,
        normalized_source,
        normalized_timestamp,
        normalized_input_hash,
    )
    if batch_id is not None and batch_id != expected_batch_id:
        raise ValueError("batch_id does not match metadata contents")

    return {
        "batch_id": expected_batch_id,
        "source": normalized_source,
        "source_tier": tier,
        "trade_day": normalized_trade_day,
        "coverage": normalized_coverage,
        "retrieved_at": normalized_timestamp,
        "quality": quality_status,
        "input_hash": normalized_input_hash,
        "execution_allowed": execution_allowed,
        "ingest_status": status,
    }


def _json_ready(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def serialize_metadata(metadata: dict[str, Any]) -> str:
    """Serialize metadata deterministically as compact UTF-8-compatible JSON."""
    validated = build_metadata(**metadata)
    return json.dumps(
        _json_ready(validated),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_metadata(payload: str) -> dict[str, Any]:
    """Parse and validate one serialized metadata record."""
    try:
        raw = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("metadata must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("metadata JSON must contain an object")
    return build_metadata(**raw)


def transition_status(
    metadata: dict[str, Any], target: IngestStatus | str
) -> dict[str, Any]:
    """Move a staged record to exactly one terminal ingest status."""
    current = build_metadata(**metadata)
    target_status = _coerce_enum(target, IngestStatus, "ingest_status")
    current_status = current["ingest_status"]

    if current_status in _TERMINAL_STATUSES:
        raise ValueError("terminal ingest status cannot transition")
    if target_status is IngestStatus.STAGED:
        raise ValueError("ingest_status transition must end in committed or failed")

    current["ingest_status"] = target_status
    return current
