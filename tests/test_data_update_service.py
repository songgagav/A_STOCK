"""AStockDataUpdate Windows service contract tests."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "install_data_update_service.ps1"


def _source() -> str:
    assert SCRIPT.is_file(), "数据更新服务安装脚本必须存在"
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_service_uses_dedicated_data_update_entrypoint_and_venv310():
    src = _source()
    assert "AStockDataUpdate" in src
    assert ".venv$Interpreter\\Scripts\\python.exe" in src
    assert "src\\data_update_daemon.py" in src
    assert "daemon.py" not in src.replace("data_update_daemon.py", "")
    assert "run_daily.py" not in src


def test_service_persists_explicit_environment_and_logs():
    src = _source()
    assert "STOCKDB_ROOT=$StockdbRoot" in src
    assert "PYTHONIOENCODING=utf-8" in src
    assert "PYTHONUTF8=1" in src
    assert "data_update_service.out.log" in src
    assert "data_update_service.err.log" in src
    assert "AppDirectory" in src
    assert "SERVICE_AUTO_START" in src
    assert "DATA_UPDATE_BACKFILL_TIMEOUT_S=900" in src


def test_service_defaults_to_manual_start_until_explicitly_enabled():
    src = _source()
    assert "[switch]$AutoStart" in src
    assert "SERVICE_DEMAND_START" in src
    assert "$startMode" in src


def test_service_keeps_nssm_hash_and_unsigned_acceptance_gate():
    src = _source()
    assert "Get-FileHash" in src
    assert "NSSM_ZIP_SHA256" in src
    assert "NSSM_EXE_SHA256" in src
    assert "IAcceptUnsignedNssm" in src
    assert "Authenticode" in src


def test_daemon_daily_stage_uses_primary_first_completion_orchestration():
    daemon = ROOT / "src" / "data_update_daemon.py"
    src = daemon.read_text(encoding="utf-8")
    assert "run_daily_completion(" in src
    assert "_primary_daily_sync" in src
    assert "_run_backfill_subprocess" in src
    assert "scripts/backfill_daily.py" not in src


def test_installer_run_wrapper_does_not_shadow_powershell_automatic_args():
    src = _source()
    assert "[string[]]$ArgList" in src
    assert "& $Exe @ArgList" in src
    assert "[string[]]$Args" not in src
    assert "& $Exe @Args" not in src


def test_installer_aborts_on_nssm_failure_instead_of_logging_success():
    src = _source()
    assert "if ($code -ne 0)" in src
    assert "throw \"NSSM command failed" in src
