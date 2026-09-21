# -*- coding: utf-8 -*-
"""提权诊断: 区分"run_daily 在跑"与"卡死" —— 隔 30 秒两次采样比 CPU 增量.

用 psutil 而非 Get-WmiObject/CIM:
  · 实测 `Get-CimInstance Win32_Process` 在**提权上下文里会挂住**(>1 分钟无输出);
  · psutil 同样能看到 LocalSystem 进程, 且快、可精确取 argv。
判据: CPU 时间增长 => 在跑; 不变 => 卡住(等待 I/O 或死锁)。
"""
from __future__ import annotations

import datetime as dt
import time

import psutil


def snap() -> dict:
    out = {}
    for p in psutil.process_iter(["name", "cmdline", "create_time", "ppid"]):
        try:
            nm = (p.info.get("name") or "").lower()
            if "python" not in nm:
                continue
            av = list(p.info.get("cmdline") or [])
            # 排除内联脚本(诊断自身), 只留真正的脚本进程
            if len(av) >= 2 and av[1] in ("-c", "-m"):
                # celery/flower 是 -m 启动的, 需保留
                if "-m" not in av[1:2] or not any("celery" in str(x) for x in av):
                    continue
            c = p.cpu_times()
            out[p.pid] = {
                "argv": av,
                "cpu": c.user + c.system,
                "rss": p.memory_info().rss / 1024 / 1024,
                "ctime": dt.datetime.fromtimestamp(p.info["create_time"]).strftime("%H:%M:%S"),
                "ppid": p.info["ppid"],
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def label(argv: list) -> str:
    for a in argv[1:]:
        s = str(a)
        if s.endswith(".py"):
            return s.replace("\\", "/").split("/")[-1]
    return " ".join(str(x) for x in argv[1:3])


a = snap()
print(f"  采样 T0 ({dt.datetime.now():%H:%M:%S}) python 进程数 = {len(a)}")
time.sleep(30)
b = snap()
print(f"  采样 T1 ({dt.datetime.now():%H:%M:%S}) python 进程数 = {len(b)}")
print()
print("  %-22s %-8s %-12s %-11s %s" % ("脚本", "pid", "CPU(T0->T1)", "ΔCPU/30s", "判定"))
rows = []
for pid, x in b.items():
    if pid not in a:
        rows.append((label(x["argv"]), pid, "（T1 新起）", "-", x))
        continue
    d = x["cpu"] - a[pid]["cpu"]
    rows.append((label(x["argv"]), pid, "%.1f->%.1fs" % (a[pid]["cpu"], x["cpu"]), "%.2fs" % d, x))
# 关心的排前面
order = {"run_daily.py": 0, "daemon.py": 1, "realtime_engine.py": 2}
rows.sort(key=lambda r: order.get(r[0], 9))
for lb, pid, cpus, d, x in rows[:16]:
    busy = "在跑" if (isinstance(d, str) and d not in ("-",) and float(d[:-1]) > 0.3) else \
           ("新增" if d == "-" else "空闲/等待 I/O")
    print("  %-22s %-8d %-12s %-11s %s" % (lb, pid, cpus, d, busy))
    if lb in ("run_daily.py", "daemon.py"):
        print("      argv = %s" % ([str(v)[:120] for v in x["argv"]]))
        print("      创建=%s ppid=%s rss=%.0fMB" % (x["ctime"], x["ppid"], x["rss"]))
print()
print("  ★ 判据: run_daily.py 的 ΔCPU/30s > 0.3s => 在干活; ≈0 => 卡住(等 I/O 或死锁)")
