# -*- coding: utf-8 -*-
"""Offline model loadability probe contracts."""

from __future__ import annotations

import zipfile
from pathlib import Path

from drl_model_validation import validate_model_loadability


def test_invalid_archive_is_rejected_before_loader(tmp_path):
    path = tmp_path / "model.zip"
    path.write_bytes(b"not-a-zip")
    called = False

    def loader(_path):
        nonlocal called
        called = True
        return object()

    result = validate_model_loadability(path, loader=loader)

    assert result["status"] == "invalid"
    assert result["load_attempted"] is False
    assert called is False


def test_loader_success_is_reported_without_writing_side_effects(tmp_path):
    path = tmp_path / "model.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data", "{}")

    result = validate_model_loadability(path, loader=lambda _path: object())

    assert result["status"] == "loadable"
    assert result["load_attempted"] is True
    assert result["side_effects"] == []
    assert path.read_bytes()


def test_loader_failure_is_blocked_and_preserves_error(tmp_path):
    path = tmp_path / "model.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data", "{}")

    result = validate_model_loadability(path, loader=lambda _path: (_ for _ in ()).throw(RuntimeError("bad model")))

    assert result["status"] == "blocked"
    assert result["load_attempted"] is True
    assert "RuntimeError: bad model" in result["reason"]
