# -*- coding: utf-8 -*-
"""进程崩溃 + 守护重启演练（第三阶段清单项之一）.

验证目标（用户设计第四节的"进程崩溃 -> 守护重启，状态恢复"）:
  1) 崩溃被**正确识别**  —— `daemon._proc_alive()` 对已退出进程必须返回 False。
     历史上此处用 PROCESS_TERMINATE(1) 权限位, 对已结束进程**误判为存活**,
     导致看护**不重建**崩溃的 dashboard(静默失去可视化)。
  2) 看护**真的重建**    —— 把"已崩溃的 pid"预置进 pidfile 后调用
     `daemon._ensure_dashboard()`, 必须拉起新进程并把新 pid 写回 pidfile。
  3) **告警/留痕**       —— `_log` 有"已拉起"记录(可被运维看到)。
  4) **幂等**            —— 已健康运行时不得重复拉起(否则每轮周期都堆进程)。

安全设计:
  · **用临时端口 + 临时 pidfile/log**, 不触碰用户正在用的 8000 面板;
  · 结束时 kill 掉自己拉起的子进程。

用法
  & <py310> scripts\\preflight_daemon_heal.py
输出
  data/preflight_daemon_heal.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

OUT = os.path.join(_BASE, "data", "preflight_daemon_heal.json")
TEST_PORT = 8123          # 刻意避开用户的 8000


def _kill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    import daemon

    tmp = tempfile.mkdtemp(prefix="dsh_heal_")
    checks: list[dict] = []
    child_pids: list[int] = []

    def rec(name, ok, detail):
        checks.append({"name": name, "pass": bool(ok), "detail": str(detail)[:220]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {detail}")

    # ---- 1) 崩溃识别 ----
    # 关键: 必须**释放 Popen 句柄**再判定。Windows 上只要还有句柄指向已终止的进程对象,
    # OpenProcess 就会成功 —— 若此处保留句柄, 会把"已死"测成"活着", 得到**假 PASS**。
    # (本演练初版正是踩了这个坑: 见文件末尾"演练自身的两处 bug"。)
    dead = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])
    dead_pid = dead.pid
    dead.wait()
    rc = dead.returncode
    del dead
    import gc
    gc.collect()
    time.sleep(0.3)
    alive_dead = daemon._proc_alive(dead_pid)
    a = alive_dead is False
    b = daemon._proc_alive(os.getpid()) is True
    c = daemon._proc_alive(None) is False
    rec("崩溃进程被判为已退出", a,
        f"已退出 pid={dead_pid}(returncode={rc}) -> _proc_alive={alive_dead}（期望 False）")
    rec("存活进程被判为存活", b, f"自身 pid={os.getpid()} -> _proc_alive=True")
    rec("空 pid 判为 False", c, "_proc_alive(None)=False")

    # ---- 2) 看护重建（临时 pidfile/端口/日志）----
    daemon.DASH_PIDFILE = os.path.join(tmp, "dash.pid")
    daemon.DASH_LOG = os.path.join(tmp, "dash.log")
    daemon.DASH_PORT = TEST_PORT
    logged: list[str] = []
    real_log = daemon._log

    def _cap(txt, sub=None):
        logged.append(str(txt))
        return real_log(txt, sub)

    daemon._log = _cap
    with open(daemon.DASH_PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(dead_pid))          # 预置"已崩溃的 pid"

    t0 = time.time()
    ok = daemon._ensure_dashboard()
    new_pid = None
    try:
        new_pid = int(open(daemon.DASH_PIDFILE, encoding="utf-8").read().strip())
    except Exception:  # noqa: BLE001
        pass
    if new_pid:
        child_pids.append(new_pid)
    new_alive = bool(new_pid) and daemon._proc_alive(new_pid)
    # **必须**要求 pid 发生变化: 否则"看护认为旧 pid 还活着"也会让本项通过 —— 即假 PASS。
    rebuilt = bool(new_pid) and new_pid != dead_pid and new_alive
    rec("看护识别崩溃并重建(pid 已更换)", ok and rebuilt,
        f"_ensure_dashboard()->{ok}; 旧(崩溃)pid={dead_pid} 新 pid={new_pid} "
        f"存活={new_alive} 用时 {time.time()-t0:.1f}s")
    rec("重建有告警/留痕", any("已拉起" in t for t in logged),
        f"_log 记录: {[t for t in logged if '拉起' in t][:1]}")

    # ---- 3) 幂等：健康运行时不重复拉起 ----
    before = new_pid
    daemon._ensure_dashboard()
    after = int(open(daemon.DASH_PIDFILE, encoding="utf-8").read().strip())
    rec("已健康运行时不重复拉起(幂等)", before == after,
        f"pid 前={before} 后={after}")

    # 清理：只杀自己拉起的子进程
    for p in child_pids:
        _kill(p)
    daemon._log = real_log
    shutil.rmtree(tmp, ignore_errors=True)

    n_pass = sum(1 for c in checks if c["pass"])
    print(f"\n  汇总: {n_pass}/{len(checks)}")
    res = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "test_port": TEST_PORT,
        "note": ("使用临时端口/pidfile/log, 未触碰用户正在使用的 8000 面板; "
                 "子进程已在结束时清理。本演练只验证**看护逻辑**, "
                 "不验证 daemon 主循环的长期运行。"),
        "checks": checks,
        "_summary": {"pass": n_pass, "total": len(checks)},
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    print(f"  已保存: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
