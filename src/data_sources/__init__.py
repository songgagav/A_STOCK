"""Data-source routing and pure ingestion-boundary primitives."""

from .adapters import CanonicalBatch, CanonicalRecord, DataSourceAdapter, RawBatch, RawRecord
from .batch_hash import canonical_serialize, content_hash
from .commit import (
    CommitResult,
    CommitSink,
    ContentProbe,
    ProbeResult,
    commit_staged,
    reconcile_staged,
)
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
from .staging import load_staged, manifest_path, metadata_path, stage_batch, staging_path
from .h5i import H5ICommitSink, H5IContentProbe, H5IUnavailableError
from .status import check_h5i, read_data_source_status

__all__ = [
    "IngestStatus",
    "QualityStatus",
    "SourceTier",
    "CanonicalBatch",
    "CanonicalRecord",
    "CommitResult",
    "CommitSink",
    "ContentProbe",
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
    "load_staged",
    "manifest_path",
    "metadata_path",
    "serialize_metadata",
    "ProbeResult",
    "commit_staged",
    "reconcile_staged",
    "stage_batch",
    "staging_path",
    "transition_status",
    "H5ICommitSink",
    "H5IContentProbe",
    "H5IUnavailableError",
    "check_h5i",
    "read_data_source_status",
]
