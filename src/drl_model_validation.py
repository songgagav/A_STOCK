# -*- coding: utf-8 -*-
"""Explicit offline DRL model loadability probes.

The probe is intentionally dependency-injected: callers choose the loader in
the canonical runtime, while tests can use a small loader without importing
torch.  This module never advances a pointer, writes a plan, or mutates a
production model directory.
"""

from __future__ import annotations

import os
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


def validate_model_loadability(
    model_path: Path | str,
    *,
    loader: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Validate archive integrity and optionally perform an injected load.

    ``loader`` should be a read-only callable such as ``PPO.load``.  Omitting
    it intentionally reports only structural validity, never pretends that a
    Torch deserialization occurred.
    """

    path = Path(model_path)
    result: dict[str, Any] = {
        "schema_version": 1,
        "model_path": str(path),
        "status": "invalid",
        "load_attempted": False,
        "reason": None,
        "side_effects": [],
    }
    if not path.is_file():
        result["reason"] = "model_missing"
        return result
    if not zipfile.is_zipfile(path):
        result["reason"] = "model_not_zip"
        return result
    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        result["reason"] = f"archive_error:{type(exc).__name__}: {exc}"
        return result
    if bad_member is not None:
        result["reason"] = f"archive_member_corrupt:{bad_member}"
        return result
    result["archive_members"] = len(names)
    if loader is None:
        result["status"] = "structurally_valid"
        result["reason"] = "loader_not_supplied"
        return result

    before_stat = os.stat(path)
    result["load_attempted"] = True
    try:
        loader(str(path))
    except Exception as exc:  # noqa: BLE001 - probe must turn loader errors into evidence
        result["status"] = "blocked"
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result
    after_stat = os.stat(path)
    if (before_stat.st_size, before_stat.st_mtime_ns) != (after_stat.st_size, after_stat.st_mtime_ns):
        result["status"] = "tampered"
        result["reason"] = "loader_modified_model_artifact"
        result["side_effects"] = ["model_artifact_modified"]
        return result
    result["status"] = "loadable"
    return result
