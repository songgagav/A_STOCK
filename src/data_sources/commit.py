"""Injected sink/probe coordination for staged canonical batches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Protocol

from .adapters import CanonicalBatch
from .batch_hash import content_hash
from .metadata import IngestStatus, deserialize_metadata, serialize_metadata, transition_status
from .staging import (
    _atomic_write_text,
    load_staged,
    manifest_path,
    metadata_path,
    staging_path,
)


_RESULT_STATUSES = frozenset({"committed", "failed", "unknown"})
_LOCKS_GUARD = threading.Lock()
_DAY_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True, slots=True)
class CommitResult:
    status: str
    row_count: int
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise ValueError(f"invalid commit status: {self.status!r}")
        if isinstance(self.row_count, bool) or self.row_count < 0:
            raise ValueError("row_count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    exists: bool
    row_count: int
    content_hash: str | None

    def __post_init__(self) -> None:
        if type(self.exists) is not bool:
            raise ValueError("exists must be a boolean")
        if isinstance(self.row_count, bool) or self.row_count < 0:
            raise ValueError("row_count must be a non-negative integer")
        if self.content_hash is not None and not isinstance(self.content_hash, str):
            raise ValueError("content_hash must be a string or None")


class CommitSink(Protocol):
    def commit(self, batch: CanonicalBatch) -> CommitResult:
        ...


class ContentProbe(Protocol):
    def probe(self, trade_day: str, symbols: list[str]) -> ProbeResult:
        ...


def _day_lock(trade_day: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _DAY_LOCKS.setdefault(trade_day, threading.Lock())


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _outcome(
    *,
    status: str,
    batch_id: str,
    trade_day: str,
    row_count: int,
    batch_hash: str,
    reason: str,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "batch_id": batch_id,
        "trade_day": trade_day,
        "row_count": row_count,
        "content_hash": batch_hash,
        "reason": reason,
    }
    if error is not None:
        result["error"] = error
    return result


def _mark_occupied_unknown(root: str | Path, batch_id: str, reason: str) -> None:
    """Persist an audit marker without importing reconcile at module load time."""
    try:
        from .reconcile import mark_occupied_unknown

        mark_occupied_unknown(root, batch_id, reason=reason)
    except Exception:
        # The original outcome remains visible to the caller; a missing audit
        # sidecar must never turn an h5i write result into a false success.
        return


def _occupied_outcome(
    root: str | Path,
    *,
    batch_id: str,
    trade_day: str,
    row_count: int,
    batch_hash: str,
    reason: str,
) -> dict[str, Any]:
    _mark_occupied_unknown(root, batch_id, reason)
    return _outcome(
        status="occupied_unknown",
        batch_id=batch_id,
        trade_day=trade_day,
        row_count=row_count,
        batch_hash=batch_hash,
        reason=reason,
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("manifest must contain an object")
    return raw


def _same_day_conflict(root: str | Path, trade_day: str, batch_id: str) -> str | None:
    metadata_dir = metadata_path(root, batch_id).parent
    if metadata_dir.exists():
        for path in metadata_dir.glob("*.json"):
            if path.name == f"{batch_id}.json":
                continue
            metadata = deserialize_metadata(path.read_text(encoding="utf-8"))
            if metadata["trade_day"] == trade_day:
                return f"trade_day already has batch {metadata['batch_id']}"

    manifests_dir = manifest_path(root, batch_id).parent
    if manifests_dir.exists():
        for path in manifests_dir.glob("*.json"):
            manifest = _read_manifest(path)
            if (
                manifest.get("trade_day") == trade_day
                and manifest.get("batch_id") != batch_id
            ):
                return f"trade_day already has manifest {manifest.get('batch_id')}"
    return None


def _write_committed_metadata(root: str | Path, metadata: dict[str, Any]) -> None:
    committed = transition_status(metadata, IngestStatus.COMMITTED)
    _atomic_write_text(
        metadata_path(root, committed["batch_id"]),
        serialize_metadata(committed),
    )


def _publish_committed(
    root: str | Path,
    metadata: dict[str, Any],
    batch: CanonicalBatch,
    *,
    reason: str,
) -> dict[str, Any]:
    batch_id = metadata["batch_id"]
    trade_day = metadata["trade_day"]
    row_count = len(batch)
    batch_hash = content_hash(batch)
    target = manifest_path(root, batch_id)

    if target.exists():
        manifest = _read_manifest(target)
        expected = {
            "batch_id": batch_id,
            "trade_day": trade_day,
            "source": metadata["source"],
            "source_tier": metadata["source_tier"].value,
            "row_count": row_count,
            "content_hash": batch_hash,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            return _occupied_outcome(
                root,
                batch_id=batch_id,
                trade_day=trade_day,
                row_count=row_count,
                batch_hash=batch_hash,
                reason="existing manifest does not match staged batch",
            )
    else:
        manifest = {
            "batch_id": batch_id,
            "trade_day": trade_day,
            "source": metadata["source"],
            "source_tier": metadata["source_tier"].value,
            "row_count": row_count,
            "content_hash": batch_hash,
            "committed_at": _now_utc(),
        }
        try:
            _atomic_write_text(
                target,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        except OSError as exc:
            return _outcome(
                status="unknown",
                batch_id=batch_id,
                trade_day=trade_day,
                row_count=row_count,
                batch_hash=batch_hash,
                reason="manifest publication failed",
                error=str(exc),
            )

    try:
        _write_committed_metadata(root, metadata)
    except OSError as exc:
        return _outcome(
            status="unknown",
            batch_id=batch_id,
            trade_day=trade_day,
            row_count=row_count,
            batch_hash=batch_hash,
            reason="metadata commit transition failed",
            error=str(exc),
        )
    return _outcome(
        status="committed",
        batch_id=batch_id,
        trade_day=trade_day,
        row_count=row_count,
        batch_hash=batch_hash,
        reason=reason,
    )


def commit_staged(
    root: str | Path,
    batch_id: str,
    sink: CommitSink,
    probe: ContentProbe,
) -> dict[str, Any]:
    """Commit one staged batch through an injected sink with an occupancy probe."""
    metadata_file = metadata_path(root, batch_id)
    metadata = deserialize_metadata(metadata_file.read_text(encoding="utf-8"))
    if metadata["batch_id"] != batch_id:
        raise ValueError("metadata batch_id does not match requested batch")
    if metadata["ingest_status"] is IngestStatus.COMMITTED:
        manifest = _read_manifest(manifest_path(root, batch_id))
        return _outcome(
            status="committed",
            batch_id=batch_id,
            trade_day=metadata["trade_day"],
            row_count=int(manifest["row_count"]),
            batch_hash=str(manifest["content_hash"]),
            reason="already committed",
        )
    if metadata["ingest_status"] is not IngestStatus.STAGED:
        raise ValueError("only staged metadata can be committed")

    _, batch = load_staged(root, batch_id)
    trade_day = metadata["trade_day"]
    batch_hash = content_hash(batch)
    row_count = len(batch)
    lock = _day_lock(trade_day)
    lock.acquire()
    try:
        conflict = _same_day_conflict(root, trade_day, batch_id)
        if conflict is not None:
            return _occupied_outcome(
                root,
                batch_id=batch_id,
                trade_day=trade_day,
                row_count=row_count,
                batch_hash=batch_hash,
                reason=conflict,
            )

        symbols = sorted({str(record["symbol"]) for record in batch})
        probe_result = probe.probe(trade_day, symbols)
        if probe_result.exists:
            if (
                probe_result.row_count == row_count
                and probe_result.content_hash == batch_hash
            ):
                return _publish_committed(
                    root, metadata, batch, reason="probe matched staged batch"
                )
            return _occupied_outcome(
                root,
                batch_id=batch_id,
                trade_day=trade_day,
                row_count=row_count,
                batch_hash=batch_hash,
                reason="probe found unmatched existing data",
            )

        try:
            result = sink.commit(batch)
        except Exception as exc:  # noqa: BLE001 - outcome is deliberately unknown
            return _outcome(
                status="unknown",
                batch_id=batch_id,
                trade_day=trade_day,
                row_count=row_count,
                batch_hash=batch_hash,
                reason="sink raised after commit attempt",
                error=str(exc),
            )
        if result.status == "failed":
            failed = transition_status(metadata, IngestStatus.FAILED)
            _atomic_write_text(
                metadata_file,
                serialize_metadata(failed),
            )
            return _outcome(
                status="failed",
                batch_id=batch_id,
                trade_day=trade_day,
                row_count=row_count,
                batch_hash=batch_hash,
                reason="sink confirmed no write",
                error=result.error,
            )
        if result.status == "unknown":
            return _outcome(
                status="unknown",
                batch_id=batch_id,
                trade_day=trade_day,
                row_count=row_count,
                batch_hash=batch_hash,
                reason="sink returned unknown outcome",
                error=result.error,
            )
        if result.row_count != row_count:
            return _outcome(
                status="unknown",
                batch_id=batch_id,
                trade_day=trade_day,
                row_count=row_count,
                batch_hash=batch_hash,
                reason="sink committed unexpected row count",
                error=f"expected {row_count}, got {result.row_count}",
            )
        return _publish_committed(root, metadata, batch, reason="sink committed")
    finally:
        lock.release()


def reconcile_staged(
    root: str | Path,
    batch_id: str,
    probe: ContentProbe,
) -> dict[str, Any]:
    """Reconcile one staged batch against an authoritative content probe."""
    metadata_file = metadata_path(root, batch_id)
    metadata = deserialize_metadata(metadata_file.read_text(encoding="utf-8"))
    if metadata["batch_id"] != batch_id:
        raise ValueError("metadata batch_id does not match requested batch")

    manifest_file = manifest_path(root, batch_id)
    if metadata["ingest_status"] is IngestStatus.COMMITTED:
        manifest = _read_manifest(manifest_file)
        return {
            "outcome": "committed",
            "batch_id": batch_id,
            "trade_day": metadata["trade_day"],
            "row_count": int(manifest["row_count"]),
            "content_hash": str(manifest["content_hash"]),
            "reason": "already committed",
        }
    if metadata["ingest_status"] is not IngestStatus.STAGED:
        raise ValueError("only staged metadata can be reconciled")

    _, batch = load_staged(root, batch_id)
    trade_day = metadata["trade_day"]
    batch_hash = content_hash(batch)
    row_count = len(batch)
    lock = _day_lock(trade_day)
    lock.acquire()
    try:
        conflict = _same_day_conflict(root, trade_day, batch_id)
        if conflict is not None:
            _mark_occupied_unknown(root, batch_id, conflict)
            return {
                "outcome": "occupied_unknown",
                "batch_id": batch_id,
                "trade_day": trade_day,
                "row_count": row_count,
                "content_hash": batch_hash,
                "reason": conflict,
            }

        symbols = sorted({str(record["symbol"]) for record in batch})
        probe_result = probe.probe(trade_day, symbols)
        if not probe_result.exists:
            return {
                "outcome": "still_staged",
                "batch_id": batch_id,
                "trade_day": trade_day,
                "row_count": row_count,
                "content_hash": batch_hash,
                "reason": "probe found no committed data",
            }
        if (
            probe_result.row_count != row_count
            or probe_result.content_hash != batch_hash
        ):
            _mark_occupied_unknown(root, batch_id, "probe found unmatched existing data")
            return {
                "outcome": "occupied_unknown",
                "batch_id": batch_id,
                "trade_day": trade_day,
                "row_count": row_count,
                "content_hash": batch_hash,
                "reason": "probe found unmatched existing data",
            }

        published = _publish_committed(
            root,
            metadata,
            batch,
            reason="probe matched staged batch",
        )
        return {
            "outcome": "committed" if published["status"] == "committed" else "still_staged",
            "batch_id": batch_id,
            "trade_day": trade_day,
            "row_count": row_count,
            "content_hash": batch_hash,
            "reason": published["reason"],
            **({"error": published["error"]} if "error" in published else {}),
        }
    finally:
        lock.release()
