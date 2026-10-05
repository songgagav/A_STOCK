"""Human review and conservative cleanup for uncertain source batches."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from .batch_hash import content_hash
from .commit import ContentProbe, _publish_committed
from .metadata import IngestStatus, deserialize_metadata, serialize_metadata, transition_status
from .staging import (
    _atomic_write_text,
    _METADATA_RELATIVE,
    _STAGING_RELATIVE,
    load_staged,
    metadata_path,
    staging_path,
)


_RECONCILE_RELATIVE = Path("data") / "h5i" / "reconcile_reviews"
_VERDICTS = {"confirmed_match", "rejected", "unresolved"}


def _review_path(root: str | Path, batch_id: str) -> Path:
    # Keep review sidecars under the fixed directory just like staging and
    # metadata paths.  Do not allow a caller-controlled batch id to become a
    # path traversal segment.
    if not isinstance(batch_id, str) or not batch_id or Path(batch_id).name != batch_id:
        raise ValueError("batch_id must be a non-empty filename-safe string")
    return Path(root).resolve() / _RECONCILE_RELATIVE / f"{batch_id}.json"


def mark_occupied_unknown(root: str | Path, batch_id: str, *, reason: str) -> Path:
    """Persist the open review marker for an uncertain existing h5i payload."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be non-empty")
    path = _review_path(root, batch_id)
    payload = {
        "batch_id": batch_id,
        "status": "occupied_unknown",
        "review_status": "open",
        "reason": reason,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return path


def resolve_occupied_unknown(
    root: str | Path,
    batch_id: str,
    *,
    reviewer: str,
    verdict: str,
    comment: str,
    probe: ContentProbe | None,
) -> dict[str, Any]:
    """Resolve one uncertain batch with an auditable human verdict.

    ``confirmed_match`` requires an injected probe to match row count and
    canonical content hash before the staged metadata can become committed.
    ``rejected`` marks the staged metadata failed. ``unresolved`` records the
    review and leaves the batch staged for later investigation.
    """
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer must be non-empty")
    if verdict not in _VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    if not isinstance(comment, str) or not comment.strip():
        raise ValueError("comment must be non-empty")

    marker = _review_path(root, batch_id)
    if not marker.exists():
        raise FileNotFoundError(f"occupied_unknown review marker not found: {marker}")
    metadata, batch = load_staged(root, batch_id)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    outcome = "occupied_unknown"
    error: str | None = None

    if verdict == "confirmed_match":
        if probe is None:
            raise ValueError("confirmed_match requires a content probe")
        symbols = sorted({str(record["symbol"]) for record in batch})
        found = probe.probe(metadata["trade_day"], symbols)
        expected_hash = content_hash(batch)
        if not found.exists or found.row_count != len(batch) or found.content_hash != expected_hash:
            outcome = "occupied_unknown"
            error = "human confirmation probe did not match staged batch"
        else:
            published = _publish_committed(
                root, metadata, batch, reason="human review confirmed probe match"
            )
            outcome = published["status"]
            error = published.get("error")
    elif verdict == "rejected":
        failed = transition_status(metadata, IngestStatus.FAILED)
        _atomic_write_text(metadata_path(root, batch_id), serialize_metadata(failed))
        outcome = "failed"

    review = {
        "batch_id": batch_id,
        "status": outcome,
        "review_status": "closed" if outcome in {"committed", "failed"} else "open",
        "reviewer": reviewer,
        "reviewed_at": now,
        "verdict": verdict,
        "comment": comment,
        "reason": error,
    }
    _atomic_write_text(marker, json.dumps(review, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return {"status": outcome, "reviewer": reviewer, "review_path": marker, "error": error}


def cleanup_staging(
    root: str | Path,
    *,
    now: datetime | None = None,
    retention_days: int = 30,
) -> dict[str, Any]:
    """Delete only old terminal staging payloads; preserve uncertain batches."""
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
        raise ValueError("retention_days must be a positive integer")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.timestamp() - timedelta(days=retention_days).total_seconds()
    base = Path(root).resolve()
    staging_dir = base / _STAGING_RELATIVE
    deleted = 0
    kept = 0
    invalid = 0
    orphaned = 0
    if not staging_dir.exists():
        return {"deleted": 0, "kept": 0, "invalid": 0, "orphaned": 0, "retention_days": retention_days}

    for path in sorted(staging_dir.glob("*.jsonl")):
        if path.stat().st_mtime > cutoff:
            kept += 1
            continue
        batch_id = path.stem
        metadata_file = metadata_path(base, batch_id)
        if not metadata_file.exists():
            # No metadata means the write ordering was interrupted. Keep it
            # for manual recovery rather than deleting an unknown payload.
            orphaned += 1
            kept += 1
            continue
        try:
            metadata = deserialize_metadata(metadata_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - invalid artifacts require manual review
            invalid += 1
            kept += 1
            continue
        if metadata["ingest_status"] not in {IngestStatus.COMMITTED, IngestStatus.FAILED}:
            kept += 1
            continue
        try:
            path.unlink()
        except OSError:
            # Cleanup is best-effort maintenance.  A locked or permission-
            # protected artifact remains available for the next run/manual
            # recovery and must not make the daemon fail.
            invalid += 1
            kept += 1
            continue
        deleted += 1

    return {
        "deleted": deleted,
        "kept": kept,
        "invalid": invalid,
        "orphaned": orphaned,
        "retention_days": retention_days,
    }


__all__ = ["cleanup_staging", "mark_occupied_unknown", "resolve_occupied_unknown"]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Resolve an occupied_unknown data-source batch")
    parser.add_argument("batch_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--verdict", choices=sorted(_VERDICTS), required=True)
    parser.add_argument("--comment", required=True)
    args = parser.parse_args()
    probe = None
    if args.verdict == "confirmed_match":
        from .h5i import H5IContentProbe

        probe = H5IContentProbe()
    result = resolve_occupied_unknown(
        args.root,
        args.batch_id,
        reviewer=args.reviewer,
        verdict=args.verdict,
        comment=args.comment,
        probe=probe,
    )
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0 if result["status"] in {"committed", "failed", "occupied_unknown"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
