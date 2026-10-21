"""Atomic, immutable, non-authoritative staging bundles for selector IC diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = 1
BUILDER_VERSION = "h5i-selector-ic-diagnostic-v1"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FINAL_PREFIX = "ic-diagnostic-"
_FIXED_FLAGS = {
    "diagnostic_only": True,
    "authoritative": False,
    "observation_eligible": False,
    "paperbook_fill_replay": False,
}


class DiagnosticBundleError(RuntimeError):
    """Invalid output path, failed atomic finalize, or bundle-integrity error."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DiagnosticBundleError(f"identity is not canonical JSON: {exc}") from exc


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _repository_roots() -> tuple[Path, ...]:
    """Return this checkout and its primary checkout when running in a worktree."""
    repo = _repo_root().resolve()
    roots = {repo}
    marker = repo / ".git"
    try:
        if marker.is_dir():
            return tuple(sorted(roots, key=str))
        if not marker.is_file():
            raise DiagnosticBundleError("cannot identify repository metadata for output guard")
        pointer = marker.read_text(encoding="utf-8").strip()
        if not pointer.lower().startswith("gitdir:"):
            raise DiagnosticBundleError("cannot parse linked-worktree repository metadata")
        git_dir = Path(pointer.split(":", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = repo / git_dir
        git_dir = git_dir.resolve(strict=True)
        common_dir_file = git_dir / "commondir"
        common_dir_text = common_dir_file.read_text(encoding="utf-8").strip()
        if not common_dir_text:
            raise DiagnosticBundleError("linked-worktree common directory is empty")
        common_dir = Path(common_dir_text)
        if not common_dir.is_absolute():
            common_dir = git_dir / common_dir
        roots.add(common_dir.resolve(strict=True).parent)
    except DiagnosticBundleError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise DiagnosticBundleError(
            f"cannot resolve repository metadata for output guard: {exc}"
        ) from exc
    return tuple(sorted(roots, key=str))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_repository_path(path: Path) -> Path:
    try:
        lexical = Path(os.path.abspath(str(path)))
        resolved = path.resolve(strict=False)
        repositories = _repository_roots()
    except (OSError, RuntimeError) as exc:
        raise DiagnosticBundleError(f"cannot resolve output path: {exc}") from exc
    for repo in repositories:
        if _is_within(lexical, repo) or _is_within(resolved, repo):
            raise DiagnosticBundleError(
                "output path must be outside the repository and protected data"
            )
    return resolved


def validate_external_output_root(output_root: str | os.PathLike[str]) -> Path:
    """Validate and resolve a caller-selected path without creating it."""
    root = Path(output_root)
    if not root.is_absolute():
        raise DiagnosticBundleError("output_root must be an absolute caller-selected path")
    return _reject_repository_path(root)


def _assert_root_still_safe(root: Path, *, require_directory: bool = True) -> Path:
    # Boundary rechecks detect path swaps between writer phases. These pathname-based
    # operations are not a handle-relative defense against a swap inside one syscall.
    checked = validate_external_output_root(root)
    if checked != root:
        raise DiagnosticBundleError("output_root changed after validation")
    if require_directory and not checked.is_dir():
        raise DiagnosticBundleError("output_root is not a directory")
    return checked


def _assert_partial_still_safe(partial: Path, root: Path) -> None:
    _assert_root_still_safe(root)
    try:
        resolved = partial.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DiagnosticBundleError(f"partial bundle path changed: {exc}") from exc
    if partial.is_symlink() or not partial.is_dir() or not _is_within(resolved, root):
        raise DiagnosticBundleError("partial bundle path changed after creation")


def _validate_root(output_root: str | os.PathLike[str]) -> Path:
    resolved = validate_external_output_root(output_root)
    try:
        _assert_root_still_safe(resolved, require_directory=False)
        resolved.mkdir(parents=True, exist_ok=True)
        _assert_root_still_safe(resolved)
    except DiagnosticBundleError:
        raise
    except OSError as exc:
        raise DiagnosticBundleError(f"cannot create staging root: {exc}") from exc
    if not resolved.is_dir():
        raise DiagnosticBundleError("output_root is not a directory")
    return resolved


def _artifact_bytes(value: bytes | str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise DiagnosticBundleError("artifact values must be bytes or text")


def _validate_artifacts(artifacts: Mapping[str, bytes | str]) -> dict[str, bytes]:
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise DiagnosticBundleError("at least one explicit artifact is required")
    result = {}
    for raw_name, value in artifacts.items():
        name = str(raw_name)
        if (not _SAFE_NAME.fullmatch(name) or name in {"manifest.json", ".", ".."}
                or "/" in name or "\\" in name):
            raise DiagnosticBundleError(f"invalid artifact filename: {name!r}")
        result[name] = _artifact_bytes(value)
    return result


def _write_file(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _cleanup_owned_partial(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def write_diagnostic_bundle(
    output_root: str | os.PathLike[str],
    run_identity: Mapping[str, object],
    artifacts: Mapping[str, bytes | str],
) -> Path:
    """Write a content-addressed bundle once; refuse collisions and never overwrite."""
    if not isinstance(run_identity, Mapping) or not run_identity:
        raise DiagnosticBundleError("run_identity must be a non-empty mapping")
    for key, expected in _FIXED_FLAGS.items():
        if key in run_identity and run_identity[key] is not expected:
            raise DiagnosticBundleError(f"run_identity conflicts with fixed flag {key}")
    identity = dict(run_identity)
    identity_bytes = _canonical_json(identity)
    identity_sha256 = _sha256_bytes(identity_bytes)
    artifact_bytes = _validate_artifacts(artifacts)
    artifact_manifest = {
        name: {"sha256": _sha256_bytes(content), "size_bytes": len(content)}
        for name, content in sorted(artifact_bytes.items())
    }
    bundle_sha256 = _sha256_bytes(_canonical_json({
        "identity_sha256": identity_sha256,
        "artifacts": artifact_manifest,
    }))
    identity_prefix = f"{_FINAL_PREFIX}{identity_sha256[:16]}"
    final_name = f"{identity_prefix}-{bundle_sha256[:16]}"
    root = _validate_root(output_root)
    _assert_root_still_safe(root)
    final_path = root / final_name
    lock_path = root / f".{identity_prefix}.lock"
    partial_prefix = f".{identity_prefix}.partial-"
    owned_lock = False
    partial_path: Path | None = None
    lock_fd = None
    try:
        _assert_root_still_safe(root)
        if final_path.exists() or final_path.is_symlink():
            raise DiagnosticBundleError(f"bundle already exists: {final_path}")
        _assert_root_still_safe(root)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        owned_lock = True
        _assert_root_still_safe(root)
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.close(lock_fd)
        lock_fd = None
        _assert_root_still_safe(root)
        if any(root.glob(f"{identity_prefix}-*")):
            raise DiagnosticBundleError(f"bundle already exists for this run identity: {identity_prefix}")
        if any(root.glob(f"{partial_prefix}*")):
            raise DiagnosticBundleError(f"partial bundle collision for {final_path}")
        _assert_root_still_safe(root)
        partial_path = Path(tempfile.mkdtemp(prefix=partial_prefix, dir=str(root)))
        _assert_partial_still_safe(partial_path, root)

        for name, content in sorted(artifact_bytes.items()):
            _assert_partial_still_safe(partial_path, root)
            _write_file(partial_path / name, content)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "bundle_type": "h5i_selector_factor_ic_diagnostic",
            "builder_version": BUILDER_VERSION,
            "run_id": identity_sha256[:20],
            "identity": identity,
            "identity_sha256": identity_sha256,
            "bundle_sha256": bundle_sha256,
            **_FIXED_FLAGS,
            "artifacts": artifact_manifest,
        }
        # Manifest is deliberately the last file written into the hidden partial dir.
        _assert_partial_still_safe(partial_path, root)
        _write_file(partial_path / "manifest.json", _canonical_json(manifest) + b"\n")

        # Atomic same-parent rename makes the complete bundle visible all at once.
        _assert_partial_still_safe(partial_path, root)
        _assert_root_still_safe(root)
        if final_path.exists() or final_path.is_symlink():
            raise DiagnosticBundleError(f"bundle already exists: {final_path}")
        _assert_root_still_safe(root)
        os.replace(partial_path, final_path)
        partial_path = None
        return final_path
    except FileExistsError as exc:
        raise DiagnosticBundleError(f"bundle finalize already in progress: {final_path}") from exc
    except DiagnosticBundleError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert filesystem failures to a stable API
        raise DiagnosticBundleError(f"bundle finalize failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        try:
            _assert_root_still_safe(root)
            cleanup_safe = True
        except DiagnosticBundleError:
            cleanup_safe = False
        if cleanup_safe and partial_path is not None:
            try:
                _assert_partial_still_safe(partial_path, root)
                _cleanup_owned_partial(partial_path)
            except OSError:
                pass
            except DiagnosticBundleError:
                pass
        if cleanup_safe and owned_lock:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_diagnostic_bundle(path: str | os.PathLike[str]) -> dict:
    """Verify manifest identity, fixed authority flags, exact file set, sizes and hashes."""
    bundle = Path(path)
    if not bundle.is_absolute():
        raise DiagnosticBundleError("bundle path must be absolute")
    bundle = _reject_repository_path(bundle)
    if not bundle.is_dir() or bundle.is_symlink():
        raise DiagnosticBundleError("bundle path is not a finalized directory")
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise DiagnosticBundleError("manifest is missing or is a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiagnosticBundleError(f"manifest cannot be read: {exc}") from exc

    if not isinstance(manifest, dict):
        raise DiagnosticBundleError("manifest must be a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DiagnosticBundleError("unsupported diagnostic bundle schema")
    if manifest.get("bundle_type") != "h5i_selector_factor_ic_diagnostic":
        raise DiagnosticBundleError("unexpected diagnostic bundle type")
    if manifest.get("builder_version") != BUILDER_VERSION:
        raise DiagnosticBundleError("unexpected diagnostic bundle builder version")
    for key, expected in _FIXED_FLAGS.items():
        if manifest.get(key) is not expected:
            raise DiagnosticBundleError(f"invalid fixed bundle flag: {key}")
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise DiagnosticBundleError("manifest identity is invalid")
    identity_sha256 = _sha256_bytes(_canonical_json(identity))
    if manifest.get("identity_sha256") != identity_sha256:
        raise DiagnosticBundleError("manifest identity hash mismatch")
    if manifest.get("run_id") != identity_sha256[:20]:
        raise DiagnosticBundleError("manifest run id mismatch")

    listed = manifest.get("artifacts")
    if not isinstance(listed, dict) or not listed:
        raise DiagnosticBundleError("manifest artifact list is invalid")
    for name, metadata in listed.items():
        if not isinstance(metadata, dict):
            raise DiagnosticBundleError(f"artifact metadata is invalid: {name}")
        digest = metadata.get("sha256")
        size = metadata.get("size_bytes")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise DiagnosticBundleError(f"artifact metadata is invalid: {name}")
    bundle_sha256 = _sha256_bytes(_canonical_json({
        "identity_sha256": identity_sha256,
        "artifacts": listed,
    }))
    if manifest.get("bundle_sha256") != bundle_sha256:
        raise DiagnosticBundleError("bundle hash mismatch")
    identity_prefix = f"{_FINAL_PREFIX}{identity_sha256[:16]}"
    expected_dir = f"{identity_prefix}-{bundle_sha256[:16]}"
    if bundle.name != expected_dir:
        raise DiagnosticBundleError("bundle directory does not match its identity")

    expected_names = set(listed) | {"manifest.json"}
    actual_names = {entry.name for entry in bundle.iterdir()}
    if actual_names != expected_names:
        raise DiagnosticBundleError("bundle contains missing or unexpected files")
    for name, metadata in listed.items():
        if (not _SAFE_NAME.fullmatch(name) or name in {"manifest.json", ".", ".."}
                or "/" in name or "\\" in name):
            raise DiagnosticBundleError(f"invalid artifact filename in manifest: {name!r}")
        artifact = bundle / name
        if not artifact.is_file() or artifact.is_symlink():
            raise DiagnosticBundleError(f"artifact is missing or not a regular file: {name}")
        if artifact.stat().st_size != metadata.get("size_bytes"):
            raise DiagnosticBundleError(f"artifact size mismatch: {name}")
        if _sha256_file(artifact) != metadata.get("sha256"):
            raise DiagnosticBundleError(f"artifact hash mismatch: {name}")
    return manifest
