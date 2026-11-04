from __future__ import annotations

import datetime as dt
import os
import sys


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))


def test_daemon_launches_engine_in_explicit_observation_mode(monkeypatch, tmp_path):
    import daemon as D

    captured = {}

    class FakeProcess:
        pid = 12345

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(D.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(D.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, raising=False)
    monkeypatch.setattr(D, "_BASE", str(tmp_path))
    monkeypatch.setattr(D, "PY", sys.executable)
    monkeypatch.setattr(D, "ENGINE_LOG", str(tmp_path / "engine.log"))
    monkeypatch.setattr(D, "ENGINE_PIDFILE", str(tmp_path / "engine.pid"))
    monkeypatch.setattr(D, "_proc_alive", lambda pid: True)
    monkeypatch.setattr(D, "_write_state", lambda: None)
    monkeypatch.setattr(D, "_state", {}, raising=False)

    D._start_engine(dt.date(2026, 10, 30))

    command = captured["command"]
    assert command[command.index("--execution-mode") + 1] == "observe"
