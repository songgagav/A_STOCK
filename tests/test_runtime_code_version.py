# -*- coding: utf-8 -*-
"""常驻守护的运行时模块版本与磁盘源码版本一致性回归。"""
from __future__ import annotations

import os
import sys
from datetime import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))
sys.path.insert(0, _REPO)

import health_state as H  # noqa: E402
import premarket_healthcheck as P  # noqa: E402
from scripts import verify_runtime_code_version as V  # noqa: E402


def _payload(code: dict) -> dict:
    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "state": "NORMAL",
        "reasons": [],
        "observed": {"code_version": code},
    }


def test_current_module_matches_disk():
    r = H.module_code_version()
    assert r["loaded_sha256"]
    assert r["disk_sha256"]
    assert r["matches"] is True


def test_detects_source_changed_after_import(tmp_path, monkeypatch):
    fp = tmp_path / "health_state.py"
    fp.write_text("old", encoding="utf-8")
    monkeypatch.setattr(H, "_MODULE_LOADED_SHA256", H._sha256_file(str(fp)))
    fp.write_text("new", encoding="utf-8")
    r = H.module_code_version(str(fp))
    assert r["matches"] is False
    assert r["loaded_sha256"] != r["disk_sha256"]


def test_source_tree_detects_change_in_another_module(tmp_path, monkeypatch):
    (tmp_path / "health_state.py").write_text("same", encoding="utf-8")
    other = tmp_path / "daemon.py"
    other.write_text("old", encoding="utf-8")
    loaded, count = H._sha256_source_tree(str(tmp_path))
    monkeypatch.setattr(H, "_MODULE_LOADED_SHA256", loaded)
    monkeypatch.setattr(H, "_MODULE_LOADED_FILE_COUNT", count)
    other.write_text("new", encoding="utf-8")
    r = H.module_code_version(str(tmp_path))
    assert r["matches"] is False
    assert r["loaded_file_count"] == r["disk_file_count"] == 2


def test_mismatch_degrades_health_state():
    r = H.assemble({"code_version": {
        "matches": False, "loaded_sha256": "a" * 64, "disk_sha256": "b" * 64}})
    assert r["state"] == "DEGRADED"
    assert any("必须重启守护" in x for x in r["reasons"])


def test_deploy_verifier_accepts_matching_snapshot(tmp_path):
    fp = tmp_path / "state.json"
    H.publish(str(fp), payload=_payload(H.module_code_version()))
    r = V.verify(str(fp), max_age_s=60)
    assert r["ok"] is True
    assert r["runtime_sha256"] == r["current_disk_sha256"]


def test_deploy_verifier_rejects_old_runtime(tmp_path):
    fp = tmp_path / "state.json"
    code = dict(H.module_code_version())
    code.update(loaded_sha256="0" * 64, matches=False)
    H.publish(str(fp), payload=_payload(code))
    r = V.verify(str(fp), max_age_s=60)
    assert r["ok"] is False
    assert any("不一致" in x for x in r["errors"])


def test_premarket_check_fails_on_runtime_disk_mismatch(tmp_path, monkeypatch):
    health_dir = tmp_path / "health"
    fp = health_dir / "state.json"
    code = dict(H.module_code_version())
    code.update(loaded_sha256="0" * 64, matches=False)
    H.publish(str(fp), payload=_payload(code))
    monkeypatch.setattr(P, "DATA_DIR", str(tmp_path), raising=False)
    r = P.check_runtime_code_version()
    assert r["status"] == "FAIL"
    assert r["detail"]["runtime_matches_current_disk"] is False


def test_deployment_docs_and_service_control_keep_restart_gate():
    doc = open(os.path.join(_REPO, "docs", "deployment.md"), encoding="utf-8").read()
    ps = open(os.path.join(_REPO, "ops", "service_control.ps1"), encoding="utf-8-sig").read()
    assert "代码部署清单（强制）" in doc
    assert "observed.code_version.scope=src/**/*.py" in doc
    assert "matches=true" in doc
    assert "VerifyRuntimeCodeVersion" in ps
    assert "verify_runtime_code_version.py" in ps
    assert "runtime_sha256" in ps and "current_disk_sha256" in ps
