# ============================================================
# heartbeat.py -- 决策层活性探测 (心跳)
#
# 背景: 决策层(drl_train / llm_commentary / pre_drl_brief / incremental_learn)
# 中多个长阻塞调用(model.learn、LLM HTTP 60s 超时)在执行期间外部无可观测信号.
# 一旦卡死/静默崩溃, 只有一个"最后落盘产物"可事后推断, 无法区分"正常处理中"
# 与"卡死". 本模块提供一个极轻量的守护线程心跳: 组件启动时开启心跳, 后台每
#  HEARTBEAT_INTERVAL 秒刷新一次 <out>/<component>_heartbeat.json 的
#  started_at / last_seen / phase, 外部(premarket_healthcheck)只需读该文件的
#  last_seen 是否随时间更新, 即可判定组件"活着"还是"卡死".
#
# 用法:
#   from heartbeat import Heartbeat
#   hb = Heartbeat(day_dir, "drl_train")
#   hb.start(phase="brief_loaded")
#   ... 阻塞调用 ...
#   hb.ping(phase="learn(800)")    # model.learn 期间每隔一会主动 ping 一次(可选)
#   hb.stop(phase="done")          # 正常结束后打终态并停止线程
#
# 心跳文件: data/drl/<YYYYMMDD>/drl_train_heartbeat.json
#   若该文件的 mtime 长期不更新(超过 STALE_AFTER), 外部判定组件失联.
# ============================================================

from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Any

# 心跳刷新间隔(秒): 守护线程每这么长时间刷新一次 last_seen
HEARTBEAT_INTERVAL = 20.0
# 外部判定"卡死"的超时阈值(秒): 超过该时间 last_seen 未更新视为失联
STALE_AFTER = 60.0


class Heartbeat:
    """决策层组件活性探测器 (后台守护线程周期性刷新心跳文件)."""

    def __init__(self, out_dir: str, component: str,
                 extra: dict | None = None):
        self.out_dir = out_dir
        self.component = component
        self.extra = dict(extra or {})
        self._path = os.path.join(out_dir, f"{component}_heartbeat.json")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = {"component": component, "started_at": None,
                       "last_seen": None, "phase": "init",
                       "ok": None, "error": None}

    # ---- 状态读写 ----
    def _touch(self, phase: str | None = None, ok: bool | None = None,
               error: str | None = None) -> None:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            if phase is not None:
                self._state["phase"] = phase
            if ok is not None:
                self._state["ok"] = ok
            if error is not None:
                self._state["error"] = error
            self._state["last_seen"] = now
            payload = {**self._state, **self.extra}
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass  # 心跳写入失败不影响主流程

    # ---- 生命周期 ----
    def start(self, phase: str = "started") -> None:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._state["started_at"] = now
        self._touch(phase=phase)
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._beat_daemon,
                                            daemon=True)
            self._thread.start()

    def ping(self, phase: str | None = None) -> None:
        """组件在长阻塞内主动刷新心跳(证明还活着)."""
        self._touch(phase=phase)

    def stop(self, phase: str = "done", ok: bool = True,
             error: str | None = None) -> None:
        self._stop.set()
        self._touch(phase=phase, ok=ok, error=error)

    def _beat_daemon(self) -> None:
        """后台守护: 每 HEARTBEAT_INTERVAL 刷新 last_seen, 直到 stop."""
        while not self._stop.is_set():
            self._touch()
            self._stop.wait(HEARTBEAT_INTERVAL)


def heartbeat_path(out_dir: str, component: str) -> str:
    """心跳文件路径, 供检查方读取."""
    return os.path.join(out_dir, f"{component}_heartbeat.json")


def read_heartbeat(out_dir: str, component: str) -> dict | None:
    """读取某组件心跳文件, 不存在返回 None."""
    p = heartbeat_path(out_dir, component)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None