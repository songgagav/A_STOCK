# ============================================================
# run_services.py -- 盘中模拟盘 服务管理器 (启动/停止引擎+可视化)
# 用法:
#   python run_services.py start          # 启动盘中引擎 + Web可视化(8000)
#   python run_services.py stop           # 停止已启动的服务
#   python run_services.py status         # 查看服务状态
# 由计划任务 "AStockRotationDaily" 在交易日 08:55 调用 start
# ============================================================

import os
import sys
import json
import time
import signal
import subprocess

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
_SRC = os.path.join(_BASE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
os.chdir(_BASE)

from proc_alive import alive as _process_alive  # noqa: E402


def _resolve_runtime_python(base=None, current_python=None, environ=None):
    """Choose the interpreter used for spawned services."""

    root = base or _BASE
    current = current_python or sys.executable
    env = os.environ if environ is None else environ

    override = str(env.get("TRAE_PYTHON", "") or "").strip()
    if override and os.path.isfile(override):
        return override

    current_abs = os.path.normcase(os.path.abspath(current))
    project_runtimes = {
        os.path.normcase(os.path.abspath(os.path.join(root, ".venv310", "Scripts", "python.exe"))),
        os.path.normcase(os.path.abspath(os.path.join(root, ".venv314", "Scripts", "python.exe"))),
    }
    if current_abs in project_runtimes and os.path.isfile(current):
        return current

    preferred = os.path.join(root, ".venv310", "Scripts", "python.exe")
    if os.path.isfile(preferred):
        return preferred

    return current


PY = _resolve_runtime_python()
PID_DIR = os.path.join(_BASE, "logs")
ENGINE_PID = os.path.join(PID_DIR, "engine.pid")
DASH_PID = os.path.join(PID_DIR, "dashboard.pid")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _read_pid(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _proc_alive(pid):
    """生命周期管理使用保守二值语义，未知状态按存活处理。"""
    return _process_alive(pid, unknown_means_alive=True)


def _norm_path(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path or ""))))


def _cmdline_has_script(cmdline, script):
    expected = _norm_path(os.path.join(_BASE, script))
    return any(_norm_path(token) == expected for token in (cmdline or []))


def _service_pid_state(pid, script):
    """Classify a PID without treating unverifiable identity as a match."""

    if not _proc_alive(pid):
        return "stopped"
    try:
        import psutil
    except Exception:
        return "unknown"
    try:
        process = psutil.Process(int(pid))
        return "matching" if _cmdline_has_script(process.cmdline(), script) else "mismatch"
    except Exception:
        return "unknown"


def _service_pid_alive(pid, script):
    """Preserve conservative startup/status semantics for unknown identity."""

    return _service_pid_state(pid, script) in {"matching", "unknown"}


def _write_pid(path, pid):
    os.makedirs(PID_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(pid))


def _spawn(name, script, args, pid_file, stdout_log):
    state = _service_pid_state(_read_pid(pid_file), script)
    if state == "matching":
        log(f"{name} 已在运行 (pid={_read_pid(pid_file)})")
        return
    if state == "unknown":
        log(f"{name} PID 身份无法核验, 为避免重复启动暂不拉起")
        return
    out = open(stdout_log, "a", encoding="utf-8")
    p = subprocess.Popen([PY, os.path.join(_BASE, script)] + args,
                         cwd=_BASE, stdout=out, stderr=out,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    _write_pid(pid_file, p.pid)
    time.sleep(2)
    log(f"{name} 已启动 pid={p.pid} -> {stdout_log}")


def start():
    os.makedirs(PID_DIR, exist_ok=True)
    from datetime import date
    try:
        from trading_calendar import is_trading_day as _tc_day
        tradable = _tc_day(date.today())
    except Exception:
        tradable = date.today().weekday() < 5
    if tradable:
        _spawn("盘中引擎", "src/realtime_engine.py", ["--interval", "15"], ENGINE_PID,
               os.path.join(PID_DIR, "live_engine.log"))
    else:
        log("非交易日(周末/节假日), 不启动盘中引擎 (可跑 run_daily.py --maint 做数据拉取+模型训练)")
    _spawn("Web可视化", "src/dashboard.py", ["--port", "8000"], DASH_PID,
           os.path.join(PID_DIR, "dashboard.log"))


def stop():
    for name, script, pid_file in (
        ("Web可视化", "src/dashboard.py", DASH_PID),
        ("盘中引擎", "src/realtime_engine.py", ENGINE_PID),
    ):
        pid = _read_pid(pid_file)
        state = _service_pid_state(pid, script)
        if state == "matching":
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True)
                log(f"{name} 已停止 pid={pid}")
            except Exception as e:
                log(f"{name} 停止失败: {e}")
        elif state == "unknown":
            log(f"{name} PID 身份无法核验, 为避免误杀暂不停止")
            continue
        else:
            log(f"{name} 未在运行")
        if os.path.exists(pid_file):
            os.remove(pid_file)


def status():
    for name, pid_file, exe in (
        ("盘中引擎", ENGINE_PID, "realtime_engine.py"),
        ("Web可视化", DASH_PID, "dashboard.py"),
    ):
        pid = _read_pid(pid_file)
        state = _service_pid_state(pid, os.path.join("src", exe))
        label = {"matching": "运行中", "unknown": "状态未知", "stopped": "未运行", "mismatch": "未运行"}[state]
        log(f"{name}: pid={pid} {label}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    else:
        print("用法: python run_services.py start|stop|status")
