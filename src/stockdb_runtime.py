"""Project-owned StockDB SDK boundary.

The vendor service owns its market data files and listens on a local endpoint.
This module owns only the Python loading contract: the repository-bundled SDK
is preferred, an explicitly configured installation is supported as a
fallback, and no process or database is started by import/inspection.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
from pathlib import Path
from typing import Any


_REQUIRED_SDK_FILES = ("stock_sdk.py", "stockdb.pyd")
_OPTIONAL_SDK_FILES = ("zhibiao.py", "zb_core.pyd")


def _as_path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    text = os.fspath(value).strip()
    return Path(text).expanduser() if text else None


def _pybao_candidate(value: str | os.PathLike[str] | None) -> Path | None:
    root = _as_path(value)
    if root is None:
        return None
    if root.name.lower() == "pybao":
        return root
    return root / "pybao"


def _usable_pybao(path: Path | None) -> bool:
    return bool(path and path.is_dir() and all((path / f).is_file() for f in _REQUIRED_SDK_FILES))


def resolve_pybao_dir(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    explicit_dir: str | os.PathLike[str] | None = None,
    external_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the SDK directory without probing or starting the service.

    Precedence is explicit caller path, project vendor path, then the
    compatibility environment/legacy external root.  A missing path is
    returned as a deterministic candidate so the caller can report the exact
    missing files instead of silently selecting another installation.
    """

    root = (_as_path(repo_root) or Path(__file__).resolve().parents[1]).resolve()
    explicit = _as_path(explicit_dir)
    if explicit is None:
        # Only the new, project-owned override is allowed to outrank the
        # bundled SDK.  PYBAO_DIR is a legacy external-path compatibility
        # variable and must not silently defeat a checked-in SDK.
        explicit = _as_path(os.environ.get("STOCKDB_PYBAO_DIR"))
    if _usable_pybao(explicit):
        return explicit.resolve()

    bundled = (root / "vendor" / "stockdb" / "pybao").resolve()
    if _usable_pybao(bundled):
        return bundled

    external = _pybao_candidate(external_root)
    if external is None:
        external = _pybao_candidate(os.environ.get("PYBAO_DIR"))
    if external is None:
        external = _pybao_candidate(os.environ.get("STOCKDB_ROOT"))
    if external is None:
        external = (root / "data" / "stockdb" / "pybao").resolve()
    return external.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_runtime(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    pybao_dir: str | os.PathLike[str] | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return deterministic SDK/file evidence; never starts StockDB."""

    root = (_as_path(repo_root) or Path(__file__).resolve().parents[1]).resolve()
    sdk_dir = (_as_path(pybao_dir) or resolve_pybao_dir(repo_root=root)).resolve()
    files = {}
    missing = []
    for name in (*_REQUIRED_SDK_FILES, *_OPTIONAL_SDK_FILES):
        path = sdk_dir / name
        item: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
        if path.is_file():
            item["sha256"] = _sha256(path)
        elif name in _REQUIRED_SDK_FILES:
            missing.append(name)
        files[name] = item

    exe_path = _as_path(executable)
    if exe_path is not None:
        exe_path = exe_path.resolve()
        executable_meta: dict[str, Any] = {
            "path": str(exe_path),
            "exists": exe_path.is_file(),
        }
        if exe_path.is_file():
            executable_meta["sha256"] = _sha256(exe_path)
    else:
        executable_meta = {"path": None, "exists": False}

    return {
        "ok": not missing,
        "repo_root": str(root),
        "pybao_dir": str(sdk_dir),
        "files": files,
        "missing_files": missing,
        "executable": executable_meta,
        "process_started": False,
    }


def load_sdk(
    *,
    pybao_dir: str | os.PathLike[str] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
):
    """Load the vendor ``stock_sdk`` module from the resolved SDK directory."""

    sdk_dir = (_as_path(pybao_dir) or resolve_pybao_dir(repo_root=repo_root)).resolve()
    status = inspect_runtime(repo_root=repo_root, pybao_dir=sdk_dir)
    if not status["ok"]:
        raise ImportError(
            "StockDB SDK 不完整: "
            f"缺少 {', '.join(status['missing_files'])} (PYBAO_DIR={sdk_dir})"
        )
    sdk_text = str(sdk_dir)
    if sdk_text not in sys.path:
        sys.path.insert(0, sdk_text)
    try:
        module = importlib.import_module("stock_sdk")
    except Exception as exc:  # noqa: BLE001
        raise ImportError(f"StockDB SDK 导入失败: {type(exc).__name__}: {exc}") from exc
    return module


def load_rd(
    *,
    pybao_dir: str | os.PathLike[str] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
):
    """Load the vendor ``stock_sdk.rd`` from the resolved SDK directory."""

    return load_sdk(pybao_dir=pybao_dir, repo_root=repo_root).rd


def daily_bar_query(code: str, date_query: str) -> tuple[str, str, str]:
    """Build the documented full daily-bar key tuple."""

    if not isinstance(code, str) or not code.strip():
        raise ValueError("StockDB 日K查询必须提供字符串 code")
    if not isinstance(date_query, str) or not date_query.strip():
        raise ValueError("StockDB 日K查询必须提供 date_query")
    return ("日k", code.strip(), date_query.strip())
