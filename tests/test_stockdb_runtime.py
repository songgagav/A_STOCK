"""StockDB project-owned runtime contract tests."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def runtime_module():
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    try:
        return importlib.import_module("stockdb_runtime")
    finally:
        sys.path.pop(0)


def test_project_sdk_is_preferred_over_external_root(runtime_module, tmp_path, monkeypatch):
    bundled = tmp_path / "vendor" / "stockdb" / "pybao"
    external = tmp_path / "external" / "pybao"
    bundled.mkdir(parents=True)
    external.mkdir(parents=True)
    (bundled / "stock_sdk.py").write_text("# bundled\n", encoding="utf-8")
    (bundled / "stockdb.pyd").write_bytes(b"bundled")
    (external / "stock_sdk.py").write_text("# external\n", encoding="utf-8")
    (external / "stockdb.pyd").write_bytes(b"external")
    monkeypatch.setenv("PYBAO_DIR", str(external))

    resolved = runtime_module.resolve_pybao_dir(
        repo_root=tmp_path / "vendor" / "..",
        explicit_dir=None,
        external_root=external,
    )

    assert resolved == bundled.resolve()


def test_explicit_pybao_dir_wins_when_valid(runtime_module, tmp_path):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "stock_sdk.py").write_text("# explicit\n", encoding="utf-8")
    (explicit / "stockdb.pyd").write_bytes(b"explicit")

    resolved = runtime_module.resolve_pybao_dir(
        repo_root=tmp_path,
        explicit_dir=explicit,
        external_root=tmp_path / "missing-external",
    )

    assert resolved == explicit.resolve()


def test_missing_sdk_is_reported_without_starting_process(runtime_module, tmp_path):
    status = runtime_module.inspect_runtime(
        repo_root=tmp_path,
        pybao_dir=tmp_path / "missing-pybao",
        executable=tmp_path / "missing-stockdb.exe",
    )

    assert status["ok"] is False
    assert status["process_started"] is False
    assert "stock_sdk.py" in status["missing_files"]
    assert "stockdb.pyd" in status["missing_files"]


def test_daily_query_requires_complete_stockdb_key(runtime_module):
    assert runtime_module.daily_bar_query("000001", "20261008") == (
        "日k",
        "000001",
        "20261008",
    )

    with pytest.raises(ValueError):
        runtime_module.daily_bar_query("000001", None)

    with pytest.raises(ValueError):
        runtime_module.daily_bar_query(None, "20261008")


def test_runtime_metadata_is_deterministic(runtime_module, tmp_path):
    pybao = tmp_path / "pybao"
    pybao.mkdir()
    for name in ("stock_sdk.py", "stockdb.pyd", "zhibiao.py", "zb_core.pyd"):
        (pybao / name).write_bytes(name.encode("ascii"))

    first = runtime_module.inspect_runtime(
        repo_root=tmp_path,
        pybao_dir=pybao,
        executable=None,
    )
    second = runtime_module.inspect_runtime(
        repo_root=tmp_path,
        pybao_dir=pybao,
        executable=None,
    )

    assert first == second
    assert first["ok"] is True
    assert first["process_started"] is False


def test_project_vendor_manifest_matches_pinned_files():
    import hashlib
    import json

    root = Path(__file__).parents[1] / "vendor" / "stockdb"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["artifacts"].items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        assert actual == expected, relative


def test_engine_loader_delegates_to_project_runtime(monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    try:
        import engine_bars_sync as engine
        marker = object()
        monkeypatch.setattr(engine.stockdb_runtime, "load_rd", lambda **_: marker)
        assert engine.load_rd() is marker
    finally:
        sys.path.pop(0)
