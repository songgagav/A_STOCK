"""Data-source routing and pure ingestion-boundary primitives."""

from .adapters import CanonicalBatch, CanonicalRecord, DataSourceAdapter, RawBatch, RawRecord
from .batch_hash import canonical_serialize, content_hash
from .metadata import (
    IngestStatus,
    QualityStatus,
    SourceTier,
    build_metadata,
    deserialize_metadata,
    serialize_metadata,
    transition_status,
)
from .normalize import normalize_baostock, normalize_baostock_batch
from .quality import QualityReport, check_quality

__all__ = [
    "IngestStatus",
    "QualityStatus",
    "SourceTier",
    "CanonicalBatch",
    "CanonicalRecord",
    "DataSourceAdapter",
    "QualityReport",
    "RawBatch",
    "RawRecord",
    "build_metadata",
    "canonical_serialize",
    "check_quality",
    "content_hash",
    "deserialize_metadata",
    "normalize_baostock",
    "normalize_baostock_batch",
    "serialize_metadata",
    "transition_status",
]
