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
from .normalize import normalize_mootdx, normalize_source_batch, normalize_zzshare
from .quality import QualityReport, check_quality
from .staging import load_staged, manifest_path, metadata_path, stage_batch, staging_path
from .h5i import H5ICommitSink, H5IContentProbe, H5IUnavailableError
from .status import check_h5i, read_data_source_status
from .network import (
    BaostockAdapter,
    MootdxAdapter,
    OptionalDependencyError,
    ZZShareAdapter,
    build_network_adapters,
)
from .production import run_daily_source_router
from .reconcile import cleanup_staging, mark_occupied_unknown, resolve_occupied_unknown

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
    "normalize_mootdx",
    "normalize_zzshare",
    "normalize_source_batch",
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
    "BaostockAdapter",
    "MootdxAdapter",
    "ZZShareAdapter",
    "OptionalDependencyError",
    "build_network_adapters",
    "run_daily_source_router",
    "cleanup_staging",
    "mark_occupied_unknown",
    "resolve_occupied_unknown",
]
