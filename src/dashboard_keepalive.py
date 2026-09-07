# ============================================================
# dashboard_keepalive.py -- dashboard.py 专用守护
#
# 职责:
#   1) 检测 dashboard.py 是否在 http://localhost:8000/ 运行
#   2) 死了自动拉起 (后台子进程, stdout/stderr -> logs/dashboard_keepalive.log)
#   3) 端口占用检测: 启动前确认 8000 端口可用, 若被其它进程占用则尝试 kill 旧 PID
#   4) PID 文件 logs/dashboard_keepalive.pid (供 --stop 杀进程)
#   5) 健康检查端点 (dashboard.py 的 /api/health, 见 dashboard.py 修改)
#
# 用法:
#   python dashboard_keepalive.py            # 前台常驻
#   python dashboard_keepalive.py --stop     # 停止守护
#   python dashboard_keepalive.py --status   # 查看状态
#   python dashboard_keepalive.py --restart  # 强制重启 dashboard
# ============================================================

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from urllib import error, request

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(_BASE, "logs")
KEEP_PID = os.path.join(LOG_DIR, "dashboard_keepalive.pid")
DASH_PIDFILE = os.path.join(LOG_DIR, "dashboard.pid")
KEEP_LOG = os.path.join(LOG_DIR, "dashboard_keepalive.log")
DASH_LOG = os.path.join(LOG_DIR, "dashboard.log")
DASH_PORT = int(os.environ.get("DASH_PORT", "8000"))
DASH_URL = f"http://localhost:{DASH_PORT}"
PY = sys.executable
_TRAE_PY = os.environ.get("TRAE_PYTHON", "")  # 可选: 指定含依赖的外部 Python 解释器
if os.path.exists(_TRAE_PY):
    PY = _TRAE_PY


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [keepalive] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(KEEP_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _proc_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(1, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _read_pid(pid_file: str) -> int | None:
    if not os.path.exists(pid_file):
        return None
    try:
        return int(open(pid_file, encoding="utf-8").read().strip())
    except Exception:
        return None


def _write_pid(pid_file: str, pid: int) -> None:
    try:
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except Exception:
        pass


def _check_port_in_use(port: int) -> int | None:
    """检查端口是否被占用, 返回占用进程的 PID 或 None."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, encoding="utf-8", errors="replace"
        )
    except Exception:
        return None
    needle = f":{port}"
    for line in out.splitlines():
        if needle in line and "LISTENING" in line:
            parts = line.split()
            try:
                return int(parts[-1])
            except Exception:
                continue
    return None


def _health_check() -> bool:
    """GET DASH_URL/api/health, 200 OK 即认为 dashboard 健康."""
    try:
        with request.urlopen(f"{DASH_URL}/api/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _kill_pid(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)
        _log(f"已 kill 旧进程 pid={pid}")
    except Exception as e:
        _log(f"taskkill pid={pid} 失败: {e}")


def _start_dashboard(force_kill: bool = False) -> int | None:
    """启动 dashboard.py 子进程, 返回新 PID."""
    # 端口占用检测
    holder = _check_port_in_use(DASH_PORT)
    if holder:
        _log(f"端口 {DASH_PORT} 被 pid={holder} 占用")
        if force_kill:
            _kill_pid(holder)
            time.sleep(2)
        else:
            # 看是不是 dashboard 自己
            if holder == _read_pid(DASH_PIDFILE):
                _log(f"dashboard 已运行 (pid={holder}), 跳过拉起")
                return holder
            # 否则 kill 旧进程
            _log(f"端口占用 PID 不是 dashboard, 强制 kill")
            _kill_pid(holder)
            time.sleep(2)

    # 删除旧 PID 文件
    if os.path.exists(DASH_PIDFILE):
        try:
            os.remove(DASH_PIDFILE)
        except Exception:
            pass

    os.makedirs(LOG_DIR, exist_ok=True)
    out = open(DASH_LOG, "a", encoding="utf-8")
    try:
        p = subprocess.Popen(
            [PY, os.path.join(_BASE, "src", "dashboard.py"), "--port", str(DASH_PORT)],
            cwd=_BASE, stdout=out, stderr=out,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as e:
        _log(f"启动 dashboard 失败: {e}")
        return None
    _write_pid(DASH_PIDFILE, p.pid)
    _log(f"已启动 dashboard pid={p.pid} -> {DASH_URL}/")
    # 等几秒做健康检查
    for _ in range(15):
        time.sleep(1)
        if _health_check():
            _log(f"dashboard 健康检查通过: {DASH_URL}/api/health")
            return p.pid
    _log(f"dashboard 启动但健康检查失败, pid={p.pid} (可能仍在初始化)")
    return p.pid


def run_loop():
    _log(f"dashboard_keepalive 启动 pid={os.getpid()} (监控 {DASH_URL})")
    consecutive_fail = 0
    while True:
        # 1) 读 PID + 健康检查
        dash_pid = _read_pid(DASH_PIDFILE)
        alive = dash_pid and _proc_alive(dash_pid)
        healthy = _health_check() if alive else False

        if not alive:
            _log(f"dashboard 不在运行 (pid_file={dash_pid})")
            consecutive_fail = 0
            _start_dashboard(force_kill=True)
            continue
        if not healthy:
            consecutive_fail += 1
            _log(f"dashboard pid={dash_pid} 在跑但健康检查失败 (consec={consecutive_fail})")
            if consecutive_fail >= 3:
                _log(f"连续 {consecutive_fail} 次失败, kill 并重启 dashboard")
                _kill_pid(dash_pid)
                time.sleep(2)
                _start_dashboard(force_kill=True)
                consecutive_fail = 0
            time.sleep(5)
            continue
        # 正常
        consecutive_fail = 0
        time.sleep(10)


def _stop():
    """停止守护, 同时杀 dashboard."""
    pid = _read_pid(KEEP_PID)
    if pid and _proc_alive(pid):
        _kill_pid(pid)
        _log(f"keepalive 已停止 pid={pid}")
    else:
        _log("keepalive 不在运行")
    dash = _read_pid(DASH_PIDFILE)
    if dash and _proc_alive(dash):
        _kill_pid(dash)
        _log(f"dashboard 已停止 pid={dash}")
    for f in (KEEP_PID, DASH_PIDFILE):
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass


def _status():
    print("=== dashboard_keepalive 状态 ===")
    keep = _read_pid(KEEP_PID)
    dash = _read_pid(DASH_PIDFILE)
    print(f"  keepalive pid    : {keep} "
          f"({'运行中' if _proc_alive(keep) else '已停止'})")
    print(f"  dashboard pid    : {dash} "
          f"({'运行中' if _proc_alive(dash) else '已停止'})")
    holder = _check_port_in_use(DASH_PORT)
    print(f"  端口 {DASH_PORT} 占用 : {holder or '(空闲)'}")
    healthy = _health_check()
    print(f"  /api/health      : {'200 OK' if healthy else '不可达'}")
    print("\n--- 最近 12 行 keepalive 日志 ---")
    try:
        with open(KEEP_LOG, encoding="utf-8") as f:
            lines = f.readlines()
        for ln in lines[-12:]:
            print("  " + ln.rstrip())
    except Exception:
        print("  无日志")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--restart", action="store_true",
                    help="强制重启 dashboard (杀 + 拉)")
    args = ap.parse_args()

    if args.stop:
        _stop()
        sys.exit(0)
    if args.status:
        _status()
        sys.exit(0)
    if args.restart:
        dash = _read_pid(DASH_PIDFILE)
        if dash:
            _kill_pid(dash)
        _start_dashboard(force_kill=True)
        sys.exit(0)

    os.makedirs(LOG_DIR, exist_ok=True)
    _write_pid(KEEP_PID, os.getpid())
    try:
        run_loop()
    except KeyboardInterrupt:
        _log("keepalive 收到 KeyboardInterrupt, 退出")
    finally:
        try:
            if os.path.exists(KEEP_PID):
                os.remove(KEEP_PID)
        except Exception:
            pass