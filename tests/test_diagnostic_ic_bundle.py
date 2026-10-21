from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


def _api():
    spec = importlib.util.find_spec("diagnostic_ic_bundle")
    assert spec is not None, "diagnostic IC bundle writer is not implemented"
    import diagnostic_ic_bundle

    return diagnostic_ic_bundle


def _require(name):
    api = _api()
    assert hasattr(api, name), f"diagnostic IC bundle is missing {name}"
    return getattr(api, name)


def _identity():
    return {
        "h5i_db_sha256": "a" * 64,
        "calendar_sha256": "b" * 64,
        "schema_sha256": "c" * 64,
        "code_sha": "d" * 40,
        "signal_range": ["2026-10-01", "2026-10-21"],
        "label_version": "0925_open_to_close_v1",
    }


def _artifacts():
    return {"ic.csv": "factor,horizon,ic\nvol,5,0.25\n", "coverage.json": b'{"n":5}\n'}


def _primary_checkout_root(repo: Path) -> Path:
    marker = repo / ".git"
    if marker.is_dir():
        return repo.resolve()
    pointer = marker.read_text(encoding="utf-8").strip()
    assert pointer.startswith("gitdir:")
    git_dir = Path(pointer.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    git_dir = git_dir.resolve()
    common_dir_text = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    common_dir = Path(common_dir_text)
    if not common_dir.is_absolute():
        common_dir = git_dir / common_dir
    return common_dir.resolve().parent


def test_bundle_requires_absolute_output_root_and_rejects_repository_paths(tmp_path):
    writer = _require("write_diagnostic_bundle")
    error = _require("DiagnosticBundleError")
    repo = Path(__file__).resolve().parents[1]

    with pytest.raises(error, match="absolute"):
        writer("relative-output", _identity(), _artifacts())
    with pytest.raises(error, match="repository"):
        writer(repo / "data" / "ic", _identity(), _artifacts())


def test_bundle_output_guard_also_rejects_primary_checkout_from_linked_worktree():
    api = _api()
    error = _require("DiagnosticBundleError")
    primary = _primary_checkout_root(Path(__file__).resolve().parents[1])

    with pytest.raises(error, match="repository"):
        api.validate_external_output_root(primary / "data" / "ic")


def test_bundle_refuses_identity_that_claims_production_authority(tmp_path):
    writer = _require("write_diagnostic_bundle")
    error = _require("DiagnosticBundleError")
    identity = {**_identity(), "authoritative": True}

    with pytest.raises(error, match="fixed flag authoritative"):
        writer(tmp_path / "stage", identity, _artifacts())


def test_bundle_rejects_symlink_root_that_resolves_into_repository(tmp_path):
    writer = _require("write_diagnostic_bundle")
    error = _require("DiagnosticBundleError")
    repo = Path(__file__).resolve().parents[1]
    link = tmp_path / "repo-link"
    link.symlink_to(repo, target_is_directory=True)

    with pytest.raises(error, match="repository"):
        writer(link / "staging", _identity(), _artifacts())


def test_bundle_has_canonical_identity_hash_content_hashes_and_diagnostic_flags(tmp_path):
    writer = _require("write_diagnostic_bundle")
    verify = _require("verify_diagnostic_bundle")
    identity = _identity()
    root = tmp_path / "stage"

    bundle = writer(root, identity, _artifacts())
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    verified = verify(bundle)

    assert bundle.name == (
        f"ic-diagnostic-{manifest['identity_sha256'][:16]}-"
        f"{manifest['bundle_sha256'][:16]}"
    )
    assert manifest["identity_sha256"] == hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                   allow_nan=False).encode("utf-8")
    ).hexdigest()
    assert manifest["diagnostic_only"] is True
    assert manifest["authoritative"] is False
    assert manifest["observation_eligible"] is False
    assert manifest["paperbook_fill_replay"] is False
    assert manifest["artifacts"]["ic.csv"]["sha256"] == hashlib.sha256(
        _artifacts()["ic.csv"].encode("utf-8")
    ).hexdigest()
    assert verified["identity"] == identity


def test_manifest_and_all_artifacts_exist_before_atomic_final_path_appears(tmp_path, monkeypatch):
    api = _api()
    writer = api.write_diagnostic_bundle
    real_replace = os.replace
    observed = []

    def inspect_before_rename(source, target):
        temporary = Path(source)
        assert (temporary / "manifest.json").is_file()
        assert (temporary / "ic.csv").read_text(encoding="utf-8").startswith("factor,")
        assert (temporary / "coverage.json").is_file()
        assert not Path(target).exists()
        observed.append(True)
        return real_replace(source, target)

    monkeypatch.setattr(api.os, "replace", inspect_before_rename)
    bundle = writer(tmp_path / "stage", _identity(), _artifacts())

    assert observed == [True]
    assert (bundle / "manifest.json").is_file()


def test_atomic_finalize_failure_cleans_only_its_partial_and_lock(tmp_path, monkeypatch):
    api = _api()
    error = api.DiagnosticBundleError
    root = tmp_path / "stage"
    root.mkdir()

    def fail_replace(_source, _target):
        raise OSError("simulated atomic rename failure")

    monkeypatch.setattr(api.os, "replace", fail_replace)
    with pytest.raises(error, match="finalize"):
        api.write_diagnostic_bundle(root, _identity(), _artifacts())

    assert list(root.glob("ic-diagnostic-*")) == []
    assert list(root.glob(".ic-diagnostic-*.partial-*")) == []
    assert list(root.glob(".ic-diagnostic-*.lock")) == []


def test_stale_partial_collision_is_refused_and_preserved(tmp_path):
    writer = _require("write_diagnostic_bundle")
    error = _require("DiagnosticBundleError")
    root = tmp_path / "stage"
    root.mkdir()
    digest = hashlib.sha256(json.dumps(
        _identity(), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()[:20]
    stale = root / f".ic-diagnostic-{digest[:16]}.partial-orphan"
    stale.mkdir()
    sentinel = stale / "keep.txt"
    sentinel.write_text("orphan evidence", encoding="utf-8")

    with pytest.raises(error, match="partial bundle collision"):
        writer(root, _identity(), _artifacts())

    assert sentinel.read_text(encoding="utf-8") == "orphan evidence"
    assert not list(root.glob(".ic-diagnostic-*.lock"))


def test_writer_rechecks_root_after_validation_before_creating_lock(tmp_path, monkeypatch):
    api = _api()
    error = api.DiagnosticBundleError
    requested = tmp_path / "route" / "staging"
    protected = tmp_path / "protected-repository"
    protected.mkdir()
    route = requested.parent
    real_validate = api._validate_root

    monkeypatch.setattr(api, "_repository_roots", lambda: (protected.resolve(),))

    def replace_ancestor_after_validation(path):
        resolved = real_validate(path)
        moved = tmp_path / "route-before-replacement"
        route.rename(moved)
        route.symlink_to(protected, target_is_directory=True)
        return resolved

    monkeypatch.setattr(api, "_validate_root", replace_ancestor_after_validation)
    with pytest.raises(error, match="repository"):
        api.write_diagnostic_bundle(requested, _identity(), _artifacts())

    assert list(protected.iterdir()) == []


def test_repeated_identity_refuses_overwrite_and_preserves_existing_bytes(tmp_path):
    writer = _require("write_diagnostic_bundle")
    error = _require("DiagnosticBundleError")
    root = tmp_path / "stage"
    bundle = writer(root, _identity(), _artifacts())
    before = {p.name: p.read_bytes() for p in bundle.iterdir()}

    with pytest.raises(error, match="already exists"):
        writer(root, dict(reversed(list(_identity().items()))), _artifacts())

    assert {p.name: p.read_bytes() for p in bundle.iterdir()} == before


def test_verifier_detects_tampered_artifact_and_unexpected_files(tmp_path):
    writer = _require("write_diagnostic_bundle")
    verify = _require("verify_diagnostic_bundle")
    error = _require("DiagnosticBundleError")
    bundle = writer(tmp_path / "stage", _identity(), _artifacts())
    original = (bundle / "ic.csv").read_bytes()
    (bundle / "ic.csv").write_bytes(b"x" * len(original))

    with pytest.raises(error, match="hash mismatch"):
        verify(bundle)


def test_verifier_rejects_unexpected_file_after_finalize(tmp_path):
    writer = _require("write_diagnostic_bundle")
    verify = _require("verify_diagnostic_bundle")
    error = _require("DiagnosticBundleError")
    bundle = writer(tmp_path / "stage", _identity(), _artifacts())
    (bundle / "untracked.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(error, match="unexpected files"):
        verify(bundle)


def test_verifier_binds_artifact_manifest_to_bundle_identity(tmp_path):
    writer = _require("write_diagnostic_bundle")
    verify = _require("verify_diagnostic_bundle")
    error = _require("DiagnosticBundleError")
    bundle = writer(tmp_path / "stage", _identity(), _artifacts())
    replacement = b"z" * (bundle / "ic.csv").stat().st_size
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["ic.csv"] = {
        "sha256": hashlib.sha256(replacement).hexdigest(),
        "size_bytes": len(replacement),
    }
    (bundle / "ic.csv").write_bytes(replacement)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    with pytest.raises(error, match="bundle hash mismatch"):
        verify(bundle)
