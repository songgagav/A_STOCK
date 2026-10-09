"""Collection-time test partitioning shared by local and CI pytest runs."""

from __future__ import annotations

import json
from pathlib import Path


SUPPORTED_PARTITIONS = frozenset({"core", "drl"})


def load_manifest(repo_root: Path) -> dict[str, frozenset[str]]:
    """Load and validate the declarative collection partition manifest."""

    manifest_path = repo_root / "tests" / "ci_test_partitions.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    drl_paths = payload.get("drl")
    if not isinstance(drl_paths, list) or not all(
        isinstance(item, str) and item.startswith("tests/")
        for item in drl_paths
    ):
        raise ValueError("ci_test_partitions.json must declare tests/drl paths")
    return {"drl": frozenset(drl_paths)}


def _relative_test_path(path: Path, repo_root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    normalized = relative.as_posix()
    if normalized == "tests" or not normalized.startswith("tests/"):
        return None
    return normalized


def should_ignore(path: Path, partition: str | None, repo_root: Path) -> bool:
    """Return whether pytest should skip *path* for the requested partition."""

    if partition not in SUPPORTED_PARTITIONS:
        return False

    relative = _relative_test_path(Path(path), repo_root)
    if relative is None:
        return False

    drl_paths = load_manifest(repo_root)["drl"]
    candidate = Path(path)
    if candidate.is_dir():
        if partition == "core":
            return False
        prefix = relative.rstrip("/") + "/"
        return not any(item.startswith(prefix) for item in drl_paths)

    is_drl = relative in drl_paths
    return is_drl if partition == "core" else not is_drl
