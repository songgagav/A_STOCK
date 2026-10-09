import os


def test_daemon_prefers_project_runtime_when_service_wrapper_reports_base_python(tmp_path):
    from src.daemon import _resolve_runtime_python

    preferred = tmp_path / ".venv310" / "Scripts" / "python.exe"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("", encoding="utf-8")

    selected = _resolve_runtime_python(
        base=str(tmp_path),
        current_python=r"C:\Python310\python.exe",
        environ={},
    )

    assert os.path.normcase(selected) == os.path.normcase(str(preferred))


def test_daemon_honors_valid_explicit_runtime_override(tmp_path):
    from src.daemon import _resolve_runtime_python

    override = tmp_path / "custom" / "python.exe"
    override.parent.mkdir(parents=True)
    override.write_text("", encoding="utf-8")
    preferred = tmp_path / ".venv310" / "Scripts" / "python.exe"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("", encoding="utf-8")

    selected = _resolve_runtime_python(
        base=str(tmp_path),
        current_python=r"C:\Python310\python.exe",
        environ={"TRAE_PYTHON": str(override)},
    )

    assert os.path.normcase(selected) == os.path.normcase(str(override))


def test_daemon_falls_back_to_current_python_when_project_runtime_missing(tmp_path):
    from src.daemon import _resolve_runtime_python

    current = str(tmp_path / "python.exe")
    selected = _resolve_runtime_python(
        base=str(tmp_path),
        current_python=current,
        environ={},
    )

    assert selected == current


def test_daemon_lock_rejects_a_second_instance_until_first_releases(tmp_path):
    from src.daemon import _acquire_daemon_lock

    path = tmp_path / "daemon.lock"
    first = _acquire_daemon_lock(str(path), pid=111)
    assert first is not None
    try:
        assert _acquire_daemon_lock(str(path), pid=222) is None
    finally:
        first.close()

    third = _acquire_daemon_lock(str(path), pid=333)
    assert third is not None
    third.close()
