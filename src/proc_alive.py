# -*- coding: utf-8 -*-
"""进程存活探测（三态：存活 / 确认不存在 / 无法判定）。

为什么需要独立一个模块
----------------------
本仓有过两处各自实现的 `_proc_alive`，且都踩过坑：

  · `daemon.py`(2026-09-19 修): 旧代码用 PROCESS_TERMINATE(1) 权限位，对已结束进程
    误判为"存活"；改用 QUERY_INFORMATION 后又发现 —— **只要还有任何句柄指向已终止的
    进程对象，OpenProcess 就会成功**，必须同时校验 `GetExitCodeProcess == STILL_ACTIVE(259)`。
    （该缺陷由故障注入演练发现，见 daemon.py 注释。）
  · `dashboard.py`: 用的是 PROCESS_QUERY_LIMITED_INFORMATION + OpenProcess 成功即算存活，
    **没有**上面那条退出码校验，也**没有**区分"打不开"的原因。

两者都不区分**"探测不到"与"已死"**。实测(2026-09-21 夜): 以交互用户身份检查以
LocalSystem 运行的守护/面板, `OpenProcess` 因**拒绝访问**失败, 于是告警中心把三个
**确实存活**的进程报成 `[critical] 进程未存活` —— 面板与守护的身份不同就会出现这种
假 CRITICAL, 而面板完全可能被外部脚本以别的身份拉起(见 P1-DASHRESTORE 的线索:
面板疑似有两个管理者)。

这属于本仓反复出现的那类**归因错误**: 把"我看不到"说成"它没了"。运维据此去重启一个
健康的进程, 比不报警更糟。

Windows 上的正确判据（本模块的核心）
------------------------------------
`OpenProcess` 失败后必须看 `GetLastError()`:
  · ERROR_INVALID_PARAMETER(87)  => 没有这个进程 => **确认不存在**
  · ERROR_ACCESS_DENIED(5)       => 对象存在但无权打开 => **存活**
    （拒绝访问本身就证明了该 pid 对应一个进程对象; 这正是跨身份探测的正确判据）
  · 其它错误                     => **无法判定**(不猜)
`OpenProcess` 成功时: 再校验退出码 == STILL_ACTIVE(259), 否则视为已结束。
"""
from __future__ import annotations

import os

#: OpenProcess 失败的两种可判读错误码
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_STILL_ACTIVE = 259


def probe(pid) -> bool | None:
    """探测 pid: True=存活, False=确认不存在, None=无法判定。

    非 Windows 走 `os.kill(pid, 0)`: 成功/EPERM => 存活; ESRCH => 不存在。
    """
    try:
        pid = int(pid or 0)
    except Exception:  # noqa: BLE001
        return False
    if pid <= 0:
        return False

    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True          # 存在但无权限 => 存活
        except ProcessLookupError:
            return False
        except Exception:  # noqa: BLE001
            return None

    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            err = k.GetLastError()
            if err == _ERROR_INVALID_PARAMETER:
                return False     # 确认不存在
            if err == _ERROR_ACCESS_DENIED:
                return True      # 存在, 只是无权打开
            return None          # 其它错误: 不猜
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                # 拿不到退出码但确实打开了句柄: 保守按存活(与 daemon 的既有语义一致,
                # 避免把活进程误判为死亡而反复重建)。
                return True
            return code.value == _STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return None


def alive(pid, unknown_means_alive: bool = True) -> bool:
    """二值化封装。`unknown_means_alive` 决定"无法判定"偏向哪边。

    默认偏向**存活**: 看护类调用方(守护)宁可少重启一次, 也不要因探测受限而反复重启
    一个健康进程 —— 后者正是"重启风暴"的成因。
    """
    r = probe(pid)
    return unknown_means_alive if r is None else bool(r)


def describe(pid) -> str:
    """给人看的措辞, 明确区分三态(供面板/日志)。"""
    r = probe(pid)
    return {True: "存活", False: "确认不存在", None: "无法判定(权限受限)"}[r]
