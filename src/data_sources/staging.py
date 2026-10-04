"""Atomic filesystem staging for canonical data batches."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .adapters import CanonicalBatch
from .batch_hash import canonical_serialize, content_hash
from .metadata import IngestStatus, build_metadata, deserialize_metadata, serialize_metadata


_STAGING_RELATIVE = Path("data") / "staging" / "bars"
_METADATA_RELATIVE = Path("data") / "h5i" / "source_metadata"
_MANIFEST_RELATIVE = Path("data") / "h5i" / "manifests"


def _root_path(root: str | Path) -> Path:
    return Path(root).resolve()


def _validate_batch_id(batch_id: str) -> str:
    if not isinstance(batch_id, str) or not batch_id or Path(batch_id).name != batch_id:
        raise ValueError("batch_id must be a non-empty filename-safe string")
    return batch_id


def staging_path(root: str | Path, batch_id: str) -> Path:
    return _root_path(root) / _STAGING_RELATIVE / f"{_validate_batch_id(batch_id)}.jsonl"


def metadata_path(root: str | Path, batch_id: str) -> Path:
    return _root_path(root) / _METADATA_RELATIVE / f"{_validate_batch_id(batch_id)}.json"


def manifest_path(root: str | Path, batch_id: str) -> Path:
    return _root_path(root) / _MANIFEST_RELATIVE / f"{_validate_batch_id(batch_id)}.json"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _jsonl_for_batch(batch: CanonicalBatch) -> str:
    # canonical_serialize validates and normalizes all hash fields before the
    # records are persisted. The JSONL retains canonical record types for the
    # later injected sink while excluding non-canonical metadata fields.
    canonical_records = json.loads(canonical_serialize(batch))
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in canonical_records
    )


def stage_batch(
    root: str | Path,
    metadata: Mapping[str, Any],
    batch: CanonicalBatch,
) -> dict[str, Any]:
    """Atomically persist canonical JSONL before publishing staged metadata."""
    validated = build_metadata(**dict(metadata))
    if validated["ingest_status"] is not IngestStatus.STAGED:
        raise ValueError("batch metadata must have ingest_status=staged")

    for record in batch:
        if record.get("trade_day") != validated["trade_day"]:
            raise ValueError("all batch records must use metadata trade_day")

    batch_payload = _jsonl_for_batch(batch)
    batch_id = validated["batch_id"]
    staged_path = staging_path(root, batch_id)
    stored_metadata_path = metadata_path(root, batch_id)
    stored_manifest_path = manifest_path(root, batch_id)
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    stored_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    stored_manifest_path.parent.mkdir(parents=True, exist_ok=True)

    _atomic_write_text(staged_path, batch_payload)
    _atomic_write_text(stored_metadata_path, serialize_metadata(validated))

    return {
        "batch_id": batch_id,
        "trade_day": validated["trade_day"],
        "staging_path": staged_path,
        "metadata_path": stored_metadata_path,
        "manifest_path": stored_manifest_path,
        "row_count": len(batch),
        "content_hash": content_hash(batch),
    }


def load_staged(
    root: str | Path,
    batch_id: str,
) -> tuple[dict[str, Any], CanonicalBatch]:
    """Load and validate one staged metadata record and its canonical JSONL."""
    metadata_file = metadata_path(root, batch_id)
    staged_file = staging_path(root, batch_id)
    metadata = deserialize_metadata(metadata_file.read_text(encoding="utf-8"))
    if metadata["batch_id"] != batch_id:
        raise ValueError("staged metadata batch_id does not match requested batch")
    if metadata["ingest_status"] is not IngestStatus.STAGED:
        raise ValueError("staged metadata must have ingest_status=staged")

    batch: CanonicalBatch = []
    for line in staged_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("staged JSONL records must be objects")
        batch.append(record)
    canonical_serialize(batch)
    return metadata, batch
