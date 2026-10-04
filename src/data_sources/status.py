"""Read-only data-source and ingest status for operations and the dashboard."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .h5i import H5IUnavailableError, _default_db_factory, _default_h5i_path
from .metadata import deserialize_metadata
from .staging import _METADATA_RELATIVE, _STAGING_RELATIVE


def check_h5i(*, path: str | Path | None = None) -> dict[str, Any]:
    """Open and close h5i once, returning a non-throwing health result."""
    db_path = Path(path) if path is not None else _default_h5i_path()
    try:
        db = _default_db_factory(db_path)
    except H5IUnavailableError as exc:
        return {"available": False, "status": "unavailable", "path": str(db_path), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - health endpoint must remain available
        return {"available": False, "status": "unavailable", "path": str(db_path), "error": str(exc)}
    try:
        return {"available": True, "status": "ready", "path": str(db_path), "error": None}
    finally:
        try:
            db.close()
        except Exception:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_metadata(root: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    invalid = 0
    directory = root / _METADATA_RELATIVE
    if not directory.exists():
        return records, invalid
    for path in sorted(directory.glob("*.json")):
        try:
            metadata = deserialize_metadata(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - preserve invalid artifacts in the report
            invalid += 1
            continue
        records.append({
            "batch_id": metadata["batch_id"],
            "source": metadata["source"],
            "source_tier": metadata["source_tier"].value,
            "trade_day": metadata["trade_day"],
            "coverage": metadata["coverage"],
            "quality": metadata["quality"].value,
            "execution_allowed": metadata["execution_allowed"],
            "ingest_status": metadata["ingest_status"].value,
            "retrieved_at": metadata["retrieved_at"],
        })
    return records, invalid


def read_data_source_status(
    root: str | Path,
    *,
    h5i_check: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return an honest, read-only status snapshot for the source router.

    The function never treats a missing h5i runtime as an empty successful
    ingest.  It reports the condition as ``status=blocked`` while keeping the
    HTTP/dashboard layer usable.
    """
    repo_root = Path(root).resolve()
    records, invalid = _load_metadata(repo_root)
    checker = h5i_check or check_h5i
    try:
        h5i = dict(checker())
    except Exception as exc:  # noqa: BLE001 - status collection must not crash UI
        h5i = {
            "available": False,
            "status": "unavailable",
            "path": str(_default_h5i_path()),
            "error": str(exc),
        }
    h5i.setdefault("available", False)
    h5i.setdefault("status", "ready" if h5i["available"] else "unavailable")

    by_ingest = dict(sorted(Counter(item["ingest_status"] for item in records).items()))
    by_source = dict(sorted(Counter(item["source"] for item in records).items()))
    by_tier = dict(sorted(Counter(item["source_tier"] for item in records).items()))
    latest = max(records, key=lambda item: item["retrieved_at"], default=None)
    staged_count = sum(1 for item in records if item["ingest_status"] == "staged")
    staging_dir = repo_root / _STAGING_RELATIVE
    staging_count = len(list(staging_dir.glob("*.jsonl"))) if staging_dir.exists() else 0

    if not h5i.get("available"):
        overall = "blocked"
    elif invalid or staged_count or any(item["ingest_status"] == "failed" for item in records):
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "ok": True,
        "status": overall,
        "generated_at": _utc_now(),
        "h5i": h5i,
        "sources": {
            "by_source": by_source,
            "by_tier": by_tier,
        },
        "batches": {
            "total": len(records),
            "invalid": invalid,
            "by_ingest_status": by_ingest,
            "latest": latest,
            "recent": sorted(records, key=lambda item: item["retrieved_at"], reverse=True)[:20],
        },
        "staging": {"count": staging_count},
    }


__all__ = ["check_h5i", "read_data_source_status"]
