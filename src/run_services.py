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
os.chdir(_BASE)

PY = sys.executable
PID_DIR = os.path.join(_BASE, "logs")
ENGINE_PID = os.path.join(PID_DIR, "engine.pid")
DASH_PID = os.path.join(PID_DIR, "dashboard.pid")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _read_pid(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _proc_alive(pid):
    if not pid:
        return False
    try:
        import ctypes
        # PROCESS_QUERY_INFORMATION | SYNCHRONIZE 权限位, 进程结束后 OpenProcess 返回 NULL.
        # 旧代码用 1 (PROCESS_TERMINATE) 会对已结束进程产生句柄误判为"存活",
        # 导致 run_services/daemon 看护不重建崩溃的 dashboard.
        h = ctypes.windll.kernel32.OpenProcess(0x0400, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _write_pid(path, pid):
    os.makedirs(PID_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(str(pid))


def _spawn(name, script, args, pid_file, stdout_log):
    alive = _proc_alive(_read_pid(pid_file))
    if alive:
        log(f"{name} 已在运行 (pid={_read_pid(pid_file)})")
        return
    out = open(stdout_log, "a", encoding="utf-8")
    p = subprocess.Popen([sys.executable, os.path.join(_BASE, script)] + args,
                         cwd=_BASE, stdout=out, stderr=out,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
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
        _spawn("盘中引擎", "realtime_engine.py", ["--interval", "15"], ENGINE_PID,
               os.path.join(PID_DIR, "live_engine.log"))
    else:
        log("非交易日(周末/节假日), 不启动盘中引擎 (可跑 run_daily.py --maint 做数据拉取+模型训练)")
    _spawn("Web可视化", "dashboard.py", ["--port", "8000"], DASH_PID,
           os.path.join(PID_DIR, "dashboard.log"))


def stop():
    for name, pid_file in (("Web可视化", DASH_PID), ("盘中引擎", ENGINE_PID)):
        pid = _read_pid(pid_file)
        if _proc_alive(pid):
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True)
                log(f"{name} 已停止 pid={pid}")
            except Exception as e:
                log(f"{name} 停止失败: {e}")
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
        alive = _proc_alive(pid)
        log(f"{name}: pid={pid} {'运行中' if alive else '未运行'}")


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