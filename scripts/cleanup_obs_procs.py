# -*- coding: utf-8 -*-
"""清点并清理观测栈的重复/孤儿进程（psutil 版, 需**管理员**才能看全 LocalSystem 进程）.

为什么不能用 CIM, 也不能用子串匹配
----------------------------------
先前三次测量各有各的坑, 记录下来以免重蹈:
  1. **CIM `Get-CimInstance Win32_Process` 在提权上下文里会挂**(实测卡死 >1 分钟无输出)。
  2. **pid 求交**: 8 小时启动过数百进程, **pid 会被回收**, 于是把别人的进程算进来
     ⇒ 虚高的 "48 个仍存活"。日志里记录过的 pid 只能当**线索**, 不能当身份。
  3. **命令行子串匹配**: `'alert_hook' in cmdline` 会让**诊断脚本匹配到自己**
     （脚本自身是 `python -c "<含该字符串的文本>"`）, 实测把一次诊断误判成"daemon 重启"。

本工具的对策:
  · 用 **psutil** 而不是 CIM（快、不挂）;
  · 判据是 **argv 某元素以 `<sep><脚本名>` 结尾**, 且**排除 `-c`/`-m` 内联调用**;
  · 非提权时会**明确报告"看不见 LocalSystem 进程"**, 而不是把它们当成不存在。

策略
----
· 每个组件**保留最早创建的那一个**, 其余判为重复;
· 孤儿 = 匹配某组件、但**父进程已不存在**且**不属于当前 daemon** 的进程;
· **绝不触碰**: daemon 本身、realtime_engine、nssm 宿主、非观测栈进程。
用法
  python scripts/cleanup_obs_procs.py                 # 预演(默认)
  python scripts/cleanup_obs_procs.py --apply         # 真正结束进程
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import os
import sys

import psutil

# 允许"重复则清理"的组件（daemon/引擎/nssm 不在列 —— 绝不动交易链路）
CLEANABLE = ("alert_hook.py", "metrics_server.py", "flower", "celery",
             # grafana 在 Windows 上有**两个**进程: `grafana.exe`(包装/启动器) 与
             # `grafana-server.exe`(真正干活并绑 :3000 的那个)。只清后者会留下前者
             # **继续占着端口** —— 实测第一次清理后 :3000 仍被 pid 39580(grafana) 占用。
             "prometheus.exe", "grafana-server.exe", "grafana.exe",
             "alertmanager.exe", "redis-server.exe")
KEEP_OLDEST = ("alert_hook.py", "metrics_server.py", "flower", "celery")


def argv_of(p) -> list:
    try:
        return list(p.info.get("cmdline") or [])
    except Exception:  # noqa: BLE001
        return []


def is_inline(argv: list) -> bool:
    return len(argv) >= 2 and argv[1] in ("-c", "-m")


def matches(argv: list, name: str) -> bool:
    """argv 中某元素以 `<sep>name` 结尾（或等于 name）; 排除内联脚本。"""
    if is_inline(argv):
        return False
    if name.endswith(".exe"):
        return any(str(a).lower().endswith(name.lower()) for a in argv[:1]) or \
               any(str(a).lower() == name.lower() for a in argv)
    for a in argv[1:]:
        s = str(a)
        if s == name or s.endswith(os.sep + name) or s.endswith("/" + name):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正结束进程（默认只预演）")
    ap.add_argument("--all", action="store_true",
                    help="清掉**全部**匹配到的组件进程（不只重复/孤儿），交给 daemon 重建 —— "
                         "适用于跨多个时期堆积、无法可靠区分归属的情形")
    args = ap.parse_args()

    me = psutil.Process()
    invisible = 0
    hits = collections.defaultdict(list)
    for p in psutil.process_iter(["name", "cmdline", "ppid", "create_time", "username"]):
        try:
            argv = argv_of(p)
            if not argv:
                invisible += 1
                continue
            for c in CLEANABLE:
                if matches(argv, c):
                    hits[c].append(p)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            invisible += 1

    print("=" * 78)
    print(f"观测栈进程清点   现在={dt.datetime.now():%Y-%m-%d %H:%M:%S}   apply={args.apply}")
    print("=" * 78)
    if invisible:
        print(f"  [注意] 有 {invisible} 个进程的命令行读不到（多半是 LocalSystem 拥有者）——")
        print(f"         非提权运行会**看不见服务自己拉起的组件**, 本清单因此可能不完整。")
        print("         （这正是先前『daemon.py: 0 个』的原因）")
    print()

    victims = []
    for comp in CLEANABLE:
        procs = sorted(hits.get(comp, []), key=lambda p: p.info["create_time"] or 0)
        if not procs:
            print(f"  {comp:<22} 0 个")
            continue
        # 策略: **保留「父进程仍活着」的那一个**（即真正由当前 daemon 拥有的），
        # 杀掉所有孤儿（父已退出）与其余重复。
        # 为什么不是"保留最早": 最早的那个很可能正是历史孤儿 —— 那样会**杀掉服务正在用的**。
        owned, orphan = [], []
        for p in procs:
            try:
                ok = psutil.pid_exists(p.info["ppid"])
            except Exception:  # noqa: BLE001
                ok = False
            (owned if ok else orphan).append(p)
        keep = owned[0] if owned else None
        if args.all:
            keep = None          # 全清模式: 一个都不留, 让 daemon 按新判据重建
        print(f"  {comp:<22} {len(procs)} 个   (归属当前父进程 {len(owned)}, 孤儿 {len(orphan)})")
        for p in procs:
            try:
                ct = dt.datetime.fromtimestamp(p.info["create_time"]).strftime("%m-%d %H:%M:%S")
            except Exception:  # noqa: BLE001
                ct = "?"
            tag = "保留(父在)" if (keep is not None and p.pid == keep.pid) else \
                  ("孤儿(父已退出)" if p in orphan else "重复")
            print(f"      pid={p.pid:<7} 创建={ct}  父={p.info['ppid']}  {tag}")
            if p.pid in (me.pid, me.ppid()):
                continue
            if keep is not None and p.pid == keep.pid:
                continue
            victims.append((comp, p, tag))

    print()
    print(f"  待清理: {len(victims)} 个")
    if not args.apply:
        print("\n(--预演: 未结束任何进程; 加 --apply 执行)")
        return 0

    killed, failed = 0, 0
    for comp, p, tag in victims:
        try:
            p.terminate()
            killed += 1
            print(f"  已结束 pid={p.pid} ({comp}, {tag})")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  结束失败 pid={p.pid} ({comp}): {type(e).__name__}: {e}")
    psutil.wait_procs([p for _, p, _ in victims], timeout=10)
    still = [p.pid for _, p, _ in victims if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
    print(f"\n  已结束 {killed}, 失败 {failed}, 仍未退出 {len(still)}: {still[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
