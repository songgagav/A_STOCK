"""Runtime selection and PID identity checks for service startup."""

from __future__ import annotations

import os
import sys
import types


def test_run_services_prefers_h5i_runtime_when_available(tmp_path):
    from src.run_services import _resolve_runtime_python

    preferred = tmp_path / ".venv310" / "Scripts" / "python.exe"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("", encoding="utf-8")

    selected = _resolve_runtime_python(
        base=str(tmp_path),
        current_python=r"C:\Python314\python.exe",
        environ={},
    )

    assert os.path.normcase(selected) == os.path.normcase(str(preferred))


def test_run_services_honors_valid_trae_python_override(tmp_path):
    from src.run_services import _resolve_runtime_python

    preferred = tmp_path / ".venv310" / "Scripts" / "python.exe"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("", encoding="utf-8")
    override = tmp_path / "custom" / "python.exe"
    override.parent.mkdir(parents=True)
    override.write_text("", encoding="utf-8")

    selected = _resolve_runtime_python(
        base=str(tmp_path),
        current_python=r"C:\Python314\python.exe",
        environ={"TRAE_PYTHON": str(override)},
    )

    assert os.path.normcase(selected) == os.path.normcase(str(override))


def test_run_services_honors_explicit_project_314_runtime_over_preferred_310(tmp_path):
    from src.run_services import _resolve_runtime_python

    preferred = tmp_path / ".venv310" / "Scripts" / "python.exe"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("", encoding="utf-8")
    current = tmp_path / ".venv314" / "Scripts" / "python.exe"
    current.parent.mkdir(parents=True)
    current.write_text("", encoding="utf-8")

    selected = _resolve_runtime_python(
        base=str(tmp_path),
        current_python=str(current),
        environ={},
    )

    assert os.path.normcase(selected) == os.path.normcase(str(current))


def test_run_services_falls_back_to_current_python_when_preferred_missing(tmp_path):
    from src.run_services import _resolve_runtime_python

    current = str(tmp_path / "python.exe")
    selected = _resolve_runtime_python(
        base=str(tmp_path),
        current_python=current,
        environ={},
    )

    assert selected == current


def test_run_services_rejects_reused_pid_owned_by_unrelated_process(monkeypatch):
    import src.run_services as rs

    class _Process:
        def cmdline(self):
            return [r"C:\Program Files\NVIDIA Corporation\NvContainer\nvcontainer.exe"]

    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=lambda pid: _Process()))
    monkeypatch.setattr(rs, "_proc_alive", lambda pid: True)

    assert not rs._service_pid_alive(10684, "src/dashboard.py")


def test_run_services_accepts_pid_running_expected_dashboard_script(monkeypatch):
    import src.run_services as rs

    class _Process:
        def cmdline(self):
            return [
                r"D:\Python\python.exe",
                os.path.join(rs._BASE, "src", "dashboard.py"),
                "--port",
                "8000",
            ]

    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=lambda pid: _Process()))
    monkeypatch.setattr(rs, "_proc_alive", lambda pid: True)

    assert rs._service_pid_alive(12345, "src/dashboard.py")


def test_run_services_marks_pid_identity_unknown_without_psutil(monkeypatch):
    import src.run_services as rs

    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(rs, "_proc_alive", lambda pid: True)

    assert rs._service_pid_state(12345, "src/dashboard.py") == "unknown"


def test_run_services_stop_does_not_kill_unverified_pid(monkeypatch, tmp_path):
    import src.run_services as rs

    pid_file = tmp_path / "dashboard.pid"
    pid_file.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(rs, "DASH_PID", str(pid_file))
    monkeypatch.setattr(rs, "ENGINE_PID", str(tmp_path / "missing-engine.pid"))
    monkeypatch.setattr(rs, "_service_pid_state", lambda pid, script: "unknown")
    killed = []
    monkeypatch.setattr(rs.subprocess, "run", lambda *args, **kwargs: killed.append(args))

    rs.stop()

    assert killed == []


def test_run_services_start_uses_src_script_paths(monkeypatch, tmp_path):
    import src.run_services as rs

    spawned = []
    monkeypatch.setattr(rs, "PID_DIR", str(tmp_path))
    monkeypatch.setitem(
        sys.modules,
        "trading_calendar",
        types.SimpleNamespace(is_trading_day=lambda day: True),
    )
    monkeypatch.setattr(
        rs,
        "_spawn",
        lambda name, script, args, pid_file, stdout_log: spawned.append((name, script)),
    )

    rs.start()

    assert [script for _, script in spawned] == [
        "src/realtime_engine.py",
        "src/dashboard.py",
    ]


def test_run_services_spawns_with_resolved_runtime_python(monkeypatch, tmp_path):
    import src.run_services as rs

    calls = []

    class _Popen:
        pid = 123

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(rs, "PY", r"C:\project\.venv310\Scripts\python.exe")
    monkeypatch.setattr(rs, "_service_pid_alive", lambda pid, script: False)
    monkeypatch.setattr(rs, "_write_pid", lambda path, pid: None)
    monkeypatch.setattr(rs.subprocess, "Popen", _Popen)
    monkeypatch.setattr(rs.time, "sleep", lambda seconds: None)

    log_path = tmp_path / "service.log"
    rs._spawn("dashboard", "src/dashboard.py", ["--port", "8000"], str(tmp_path / "pid"), str(log_path))

    assert calls[0][0][0] == r"C:\project\.venv310\Scripts\python.exe"


def test_run_services_uses_portable_creation_flags(monkeypatch, tmp_path):
    import src.run_services as rs

    calls = []

    class _Popen:
        pid = 123

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.delattr(rs.subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)
    monkeypatch.setattr(rs, "_service_pid_state", lambda pid, script: "stopped")
    monkeypatch.setattr(rs, "_write_pid", lambda path, pid: None)
    monkeypatch.setattr(rs.subprocess, "Popen", _Popen)
    monkeypatch.setattr(rs.time, "sleep", lambda seconds: None)

    log_path = tmp_path / "service.log"
    rs._spawn("dashboard", "src/dashboard.py", ["--port", "8000"], str(tmp_path / "pid"), str(log_path))

    assert calls[0][1]["creationflags"] == 0
