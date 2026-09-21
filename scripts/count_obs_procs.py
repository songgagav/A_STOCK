# -*- coding: utf-8 -*-
"""精确清点观测栈组件进程（**避免"探测器匹配到自己"**）.

为什么要单独写这个
------------------
先前两次测量都不可靠, 必须留下记录以免重蹈:
  1. **pid 求交**: 拿"日志里记录过的 pid"与"当前存活 pid"求交 —— 8 小时里启动过数百个
     进程, **pid 会被回收复用**, 于是把别人的进程算成了泄漏进程 ⇒ 得出虚高的 "48 个仍存活"。
  2. **命令行子串匹配**: 用 `'alert_hook' in cmdline` 这类判据 —— 而本诊断脚本自身就是
     `python -c "<脚本文本>"`, **脚本文本里正好含这些字符串**, 于是**每个诊断进程都匹配了自己**
     ⇒ 计数被自身污染（实测把一次诊断的 `pid=42336/11308` 误判成"daemon 重启"）。

正确判据: **argv 中存在某个元素以目标脚本名结尾**(而不是在任意文本里出现子串),
且排除 `-c` 形式的调用。这样 `python -c "...alert_hook..."` 不会被计入。
"""
from __future__ import annotations

import collections
import datetime as dt
import os
import sys

import psutil

TARGETS = ("alert_hook.py", "metrics_server.py", "daemon.py", "realtime_engine.py",
           "gate_refresh_daemon.py")


def _argv(p) -> list:
    try:
        return list(p.info.get("cmdline") or [])
    except Exception:  # noqa: BLE001
        return []


def _is_inline(argv: list) -> bool:
    """`python -c ...` 形式的内联脚本 —— 绝不可按脚本文本匹配。"""
    return len(argv) >= 2 and argv[1] in ("-c", "-m") and "-c" in argv[:3]


def matched(argv: list, name: str) -> bool:
    """精确判据: 存在 argv 元素**以 `<sep>name` 结尾**(或等于 name), 且不是 -c/-m 内联。"""
    if _is_inline(argv):
        return False
    for a in argv[1:]:
        s = str(a)
        if s == name or s.endswith(os.sep + name) or s.endswith("/" + name):
            return True
    return False


def main() -> int:
    counts = collections.Counter()
    detail = collections.defaultdict(list)
    inline = 0
    for p in psutil.process_iter(["name", "cmdline", "ppid", "create_time"]):
        argv = _argv(p)
        if not argv:
            continue
        if _is_inline(argv):
            inline += 1
        for t in TARGETS:
            if matched(argv, t):
                counts[t] += 1
                detail[t].append((p.pid, p.info.get("ppid"), p.info.get("create_time")))
                break

    print("=== 精确清点（排除 -c/-m 内联脚本, 按 argv 结尾匹配）===")
    print(f"  (被排除的内联脚本进程数: {inline} —— 它们正是先前『自我匹配』的来源)")
    for t in TARGETS:
        rows = sorted(detail.get(t, []), key=lambda r: r[2] or 0)
        when = ", ".join(dt.datetime.fromtimestamp(r[2]).strftime("%H:%M:%S")
                         for r in rows[:6] if r[2]) or "-"
        print(f"  {t:<24} {counts.get(t, 0):3d} 个" + (f"   最早: {when}" if rows else ""))
        if len(rows) > 6:
            print(f"      … 共 {len(rows)} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
