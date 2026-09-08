# -*- coding: utf-8 -*-
"""tasks_db.py -- Celery 任务: 全量数据库更新 (update_all_tables).

- broker/backend: Redis (本机 127.0.0.1:6379, db0/db1)
- Windows 下 worker 需 solo pool:
    celery -A src.tasks_db worker --pool=solo -l info
- 进度与手动限频状态写入 data/db_update_state.json (原子), 供 dashboard 轮询;
  若 Redis 不可达(未部署), 同函数仍可直调 (同步执行) 作降级.

手动更新限频: MIN_INTERVAL_S=600 (10 分钟) 内重复触发直接拒绝.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
SRC = os.path.join(_BASE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.chdir(_BASE)

STATE_FILE = os.path.join(_BASE, "data", "db_update_state.json")
MIN_INTERVAL_S = int(os.environ.get("DB_UPDATE_MIN_INTERVAL_S", "600"))

# 全量同步的分表清单 (与 run_daily db_update 的 only 列表一致)
ALL_STAGES = ["daily_bars", "valuation_snapshot", "adj_factors",
              "northbound_money", "margin_daily", "dzjy_daily",
              "money_flow_estimate", "lhb", "stock_news", "events",
              "orderbook_snapshot", "block_trade"]

try:
    from celery import Celery
    _celery_ok = True
except Exception:  # pragma: no cover
    _celery_ok = False

REDIS_URL = os.environ.get("DB_REDIS_URL", "redis://127.0.0.1:6379/0")

if _celery_ok:
    app = Celery("astock_db", broker=REDIS_URL,
                 backend=REDIS_URL.replace("/0", "/1"))
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        broker_connection_retry_on_startup=True,
        timezone="Asia/Shanghai",
    )
else:  # pragma: no cover
    app = None


def _read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def update_state(**kw) -> dict:
    st = _read_state()
    st.update(kw)
    st["ts"] = datetime.now().isoformat(timespec="seconds")
    _write_state(st)
    return st


def can_manual_update() -> tuple[bool, str]:
    """限频检查: 距上次手动更新 >= MIN_INTERVAL_S 且无任务在跑."""
    st = _read_state()
    if st.get("running"):
        return False, "已有更新任务在运行"
    lm = st.get("last_manual")
    if lm:
        try:
            last = datetime.fromisoformat(lm)
            left = MIN_INTERVAL_S - (datetime.now() - last).total_seconds()
            if left > 0:
                return False, "手动更新限频中, 请 %.0f 分钟后再试" % (left / 60)
        except Exception:
            pass
    return True, "ok"


def _progress_cb(stage: str, payload: dict) -> None:
    update_state(stage=stage, payload=payload)


def run_update_all(day: str | None = None) -> dict:
    """同步执行全量更新 (worker 内调用; 无 redis 时也可直调降级).

    逐表调用 update_db.update_all(only=[t]), 每完成一张表写一次进度.
    """
    from db_stats import update_all_tables
    day = day or datetime.now().strftime("%Y-%m-%d")
    update_state(running=True, day=day, stage="start", ok=None, detail={},
                 finished=None)
    t0 = time.time()
    detail = {}
    try:
        for t in ALL_STAGES:
            update_state(running=True, stage="update", current=t)
            try:
                rep = update_all_tables(day=day, only=[t])
                detail[t] = {"ok": rep.get("ok"),
                             "tables": rep.get("tables", {}).get(t)}
            except Exception as e:  # noqa: BLE001
                detail[t] = {"ok": False, "err": str(e)[:160]}
            update_state(running=True, stage="update", current=None,
                         detail=detail)
    finally:
        ok = any(v.get("ok") for v in detail.values()) if detail else False
        update_state(running=False, stage="done", ok=ok,
                     detail=detail, finished=datetime.now().isoformat(
                         timespec="seconds"),
                     last_manual=datetime.now().isoformat(timespec="seconds"),
                     elapsed_s=round(time.time() - t0, 1))
    return {"ok": ok, "day": day, "detail": detail}


if _celery_ok and app is not None:
    @app.task(bind=True, name="astock_db.update_all_tables")
    def update_all_tables_task(self, day: str | None = None):
        """Celery 入口: 异步执行全量更新."""
        return run_update_all(day=day)


def enqueue_update(day: str | None = None) -> dict:
    """投递 Celery 异步任务 (带限频检查). 无 celery/redis 时同步降级."""
    ok, msg = can_manual_update()
    if not ok:
        return {"ok": False, "error": msg}
    update_state(pending=True)
    if _celery_ok and app is not None:
        try:
            update_all_tables_task.delay(day=day)
            update_state(pending=False, async_=True)
            return {"ok": True, "async": True, "msg": "后台任务已投递"}
        except Exception as e:  # noqa: BLE001
            update_state(pending=False, async_=False, note=str(e)[:120])
    # 降级: 同步执行 (阻塞调用方; dashboard 内少见)
    r = run_update_all(day=day)
    return {"ok": r.get("ok"), "async": False, "msg": "同步执行完成"}


RUN_DAILY_STATE = os.path.join(_BASE, "data", "run_daily_task_state.json")


def _rd_state() -> dict:
    try:
        with open(RUN_DAILY_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _rd_write(**kw) -> dict:
    st = _rd_state()
    st.update(kw)
    st["ts"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(RUN_DAILY_STATE), exist_ok=True)
    tmp = RUN_DAILY_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RUN_DAILY_STATE)
    return st


def read_run_daily_state() -> dict:
    return _rd_state()


def run_daily_now(day: str | None = None, mode: str = "full") -> dict:
    """子进程执行 run_daily.py (full 或 maint), 状态写入 run_daily_task_state.json.

    run_daily 整条管道(数据拉取+模型训练)可能长达数十分钟~1h, 仅应在后台任务调用.
    """
    import subprocess
    day = day or datetime.now().strftime("%Y-%m-%d")
    _rd_write(running=True, day=day, mode=mode, stage="start", ok=None,
              finished=None)
    cmd = [PY, os.path.join(_BASE, "src", "run_daily.py")]
    if mode == "maint":
        cmd.append("--maint")
    else:
        cmd.append(day)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=_BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=3600)
        tail = (p.stdout or "")[-1500:] + (p.stderr or "")[-300:]
        _rd_write(running=False, stage="done", ok=(p.returncode == 0),
                  returncode=p.returncode, tail=tail[-1600:],
                  finished=datetime.now().isoformat(timespec="seconds"),
                  elapsed_s=round(time.time() - t0, 1))
        return {"ok": p.returncode == 0, "day": day, "mode": mode,
                "returncode": p.returncode}
    except subprocess.TimeoutExpired:
        _rd_write(running=False, stage="timeout", ok=False,
                  error="run_daily 超时(>3600s)", elapsed_s=3600)
        return {"ok": False, "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        _rd_write(running=False, stage="error", ok=False, error=str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}


if _celery_ok and app is not None:
    @app.task(bind=True, name="astock_db.run_daily_full")
    def run_daily_full_task(self, day: str | None = None, mode: str = "full"):
        return run_daily_now(day=day, mode=mode)


def enqueue_run_daily(day: str | None = None, mode: str = "full") -> dict:
    """投递 Celery 异步 run_daily 任务; 已运行则拒绝, 无 celery 时同步降级."""
    st = _rd_state()
    if st.get("running"):
        return {"ok": False, "error": "已有 run_daily 任务在运行"}
    if _celery_ok and app is not None:
        try:
            run_daily_full_task.delay(day=day, mode=mode)
            return {"ok": True, "async": True,
                    "msg": "run_daily 后台任务已投递 (%s)" % mode}
        except Exception as e:  # noqa: BLE001
            _rd_write(note=str(e)[:120])
    r = run_daily_now(day=day, mode=mode)
    return {"ok": r.get("ok"), "async": False, "msg": "run_daily 同步执行完成"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="同步执行一次全量更新")
    ap.add_argument("--status", action="store_true", help="查看更新状态")
    a = ap.parse_args()
    if a.run:
        print(json.dumps(run_update_all(), ensure_ascii=False, indent=1)[:2000])
    else:
        print(json.dumps(_read_state(), ensure_ascii=False, indent=1))
