# -*- coding: utf-8 -*-
"""数据流动看门狗（路线图 #4）：进程活着，但数据停流 / 主循环死锁。

为什么需要它（与既有看护的分工）
--------------------------------
守护进程对盘中引擎的监护是 **pid 级**的：`_proc_alive(engine_pid)` 死了就拉起。
但有一类故障 pid 检查**永远抓不到**：引擎进程活着、主循环却卡住不再写数据
（例如阻塞在 AtlasCore 代理的慢请求上，或卡在 h5i 锁等待）。此时守护认为一切正常，
账户估值与触发判定静静冻住 —— 盘面上「什么都没发生」，直到收盘才发现整天没动过。

本模块只做**判定**，不擅自动手：重启一个活着的引擎是高风险动作（可能丢掉它正持有的
状态），按既定红线应转人工，故这里仅让这类静默停摆**可见**（守护日志 / 面板 / 收盘清单
共用同一判据）。

阈值推导（本仓证据，不照抄外部）
--------------------------------
合法写间隔 = **处理耗时 + 间隔**，因为引擎主循环是 `tick(); sleep(interval)`
（`realtime_engine.py:1173-1198`），而守护启动引擎时传 `--interval 15`
（`daemon.py::_start_engine`）。

  · 健康：    处理 ≈ 7ms（2026-09-21 13:08 实测 p50）      => 写间隔 ≈ 15s
  · 实测退化：处理 p99 = 15.6s（2026-09-21 15:02 AtlasCore 抖动） => 写间隔 ≈ 30.6s

本仓 `heartbeat.py` 的房规是「阈值 = 3× 标称周期」（STALE_AFTER=60 / INTERVAL=20）。
照搬到这里是 45s —— **但 45s 只有实测退化间隔（30.6s）的 1.5 倍**：代理一慢就会误报
「主循环死锁」，而**恰好在代理慢的时候系统本身是正常的**；P2-ATLASPROXY 至今未闭环，
这种误报会规律性出现，正是「狼来了」式告警的来源，与本次加固要让告警可信的目标相悖。

故取 **120s**，三条独立理由：
  1. 是实测退化间隔的 **3.9 倍** => 代理抖动不致误报；
  2. 是标称间隔的 8 倍 => 盘中静默 2 分钟即报，远小于半小时级的决策窗口，人来得及处置；
  3. 与面板既有的事实判据一致（`dashboard.py` 告警中心：盘中且 `age > 120` 即告警）
     —— 单一事实源，不新造第二个数字。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

#: 盘中"数据停流"判定阈值（秒）。推导见模块 docstring（三条理由）。
STALL_AFTER_S = 120.0

#: 判定结果的原因分类。**归因必须精确**：行情源坏了 ≠ 主循环死锁，两者的运维动作不同。
CAUSES = ("idle", "ok", "stalled", "feed_stale", "dead_process", "unknown")

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _ts_age(ts, now: datetime):
    """时间戳距今秒数；缺失/不可解析返回 None（**不猜**）。"""
    if not ts:
        return None
    try:
        return (now - datetime.strptime(str(ts), _TS_FMT)).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def evaluate(sample: dict, now=None) -> dict:
    """把一次采样判成结论（**纯函数**，CI 可测）。

    sample 字段（缺一律当"未知"，不得当作"坏"）:
      in_session  引擎自报是否在盘中
      updated     引擎最近一次写 live_state 的时间 'YYYY-MM-DD HH:MM:SS'
      tick        引擎 tick 计数器（用于给"死锁"提供硬证据）
      prev_tick   上一次采样读到的计数
      pid_alive   引擎进程是否存活（True/False/None=未知）
      engine_pid  引擎 pid，仅用于措辞
      feed_error  引擎自报的行情源错误
      live_source 估值来源标签

    返回 {level, cause, reason, age_s, tick, tick_stuck}；level ∈ OK|WARN|CRITICAL。
    """
    now = now or datetime.now()
    st = sample or {}
    age = _ts_age(st.get("updated"), now)
    tick, prev = st.get("tick"), st.get("prev_tick")
    tick_stuck = (isinstance(tick, int) and isinstance(prev, int) and tick == prev)
    base = {"age_s": age, "tick": tick, "tick_stuck": tick_stuck}

    # 1) 非盘中不判定 —— 收盘后引擎本就不再写状态，此时 age 无限增大是**正常**的。
    #    (夜间误报是"狼来了"的典型来源; 2026-09-21 夜就踩过一次同类的 tick 窗口冻结。)
    if not st.get("in_session"):
        return {**base, "level": "OK", "cause": "idle",
                "reason": "非盘中, 不判定数据停流(收盘后引擎本就不再写状态)"}

    # 2) 进程不在 —— 属守护 pid 监护域，此处只如实报告，不越权。
    if st.get("pid_alive") is False:
        return {**base, "level": "CRITICAL", "cause": "dead_process",
                "reason": f"引擎进程不存在(pid={st.get('engine_pid')}) —— 属守护 pid 监护域"}

    # 3) 读不到时间戳 -> 未知，不冒充"停流"。
    if age is None:
        return {**base, "level": "WARN", "cause": "unknown",
                "reason": "盘中但数据流动无法判定: live_state 未写或时间戳不可解析"}

    # 4) 超阈值：先分清是**行情源坏了**还是**主循环卡了** —— 运维动作完全不同。
    if age > STALL_AFTER_S:
        err = str(st.get("feed_error") or "").strip()
        src = st.get("live_source")
        if err or src in ("price_hold", "duckdb_reference_held"):
            why = (f"行情源错误: {err[:110]}" if err
                   else f"估值退化为静态价(live_source={src})")
            return {**base, "level": "CRITICAL", "cause": "feed_stale",
                    "reason": f"盘中数据停更 {age:.0f}s, 但**不是主循环死锁** —— {why}"}
        ev = f", tick 停在 {tick} 未推进" if tick_stuck else ""
        return {**base, "level": "CRITICAL", "cause": "stalled",
                "reason": (f"主循环疑似死锁: 盘中 {age:.0f}s 未写状态"
                           f"(阈值 {STALL_AFTER_S:.0f}s){ev}")}

    # 5) 正常流动
    return {**base, "level": "OK", "cause": "ok",
            "reason": f"盘中数据在流动: 最近一次写入 {age:.0f}s 前"}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_proc_alive(pid):
    """默认存活探测：委托 `proc_alive.probe`（**三态**）。

    为什么必须是三态: `evaluate` 里 `pid_alive is False` 才判"进程不存在"; `None` 是
    "无法判定", 走别的分支。若这里把"打不开"压成 False, 跨身份场景下会把**活着的**引擎
    报成"进程不存在" —— 与面板告警中心踩过的是同一个坑(见 src/proc_alive.py)。

    该模块同时吸收了 daemon 那条硬换来的退出码校验(句柄残留会使已终止进程被
    OpenProcess 成功打开), 故此处不再自造一份。导入失败退 psutil, 最后退"未知"。
    """
    if not pid:
        return False
    try:
        import sys
        sys.path.insert(0, os.path.join(_repo_root(), "src"))
        from proc_alive import probe  # noqa: PLC0415
        return probe(pid)
    except Exception:  # noqa: BLE001
        pass
    try:
        import psutil  # noqa: PLC0415
        return bool(psutil.pid_exists(int(pid)))
    except Exception:  # noqa: BLE001
        return None


def state_path(path: str | None = None) -> str:
    """看门狗自己的采样状态（用于判定 tick 计数器是否推进）。"""
    return path or os.path.join(_repo_root(), "data", "flow_watchdog_state.json")


def _read_json(fp: str) -> dict:
    try:
        with open(fp, encoding="utf-8-sig") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_json_atomic(fp: str, payload: dict) -> None:
    d = os.path.dirname(fp)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)


def gather(live_state: str | None = None, pidfile: str | None = None,
           state: str | None = None, pid_alive_fn=None, now=None) -> dict:
    """采集真实可观测量 -> 判定，并把本次 tick 采样存下（供下次判定"是否推进"）。

    路径/存活探测均可注入，便于测试（本函数触碰文件系统，不在 CI 里直接跑）。
    """
    root = _repo_root()
    lv_fp = live_state or os.path.join(root, "data", "live_state.json")
    pf = pidfile or os.path.join(root, "logs", "engine.pid")
    sp = state_path(state)

    lv = _read_json(lv_fp)
    pid = None
    try:
        if os.path.isfile(pf):
            pid = int(open(pf, encoding="utf-8").read().strip() or 0) or None
    except Exception:  # noqa: BLE001
        pid = None

    prev = _read_json(sp)
    sample = {
        "in_session": bool(lv.get("in_session")),
        "updated": lv.get("updated"),
        "tick": lv.get("tick"),
        "prev_tick": prev.get("tick"),
        "feed_error": lv.get("feed_error"),
        "live_source": lv.get("live_source"),
        "engine_pid": pid,
        "pid_alive": (pid_alive_fn or _default_proc_alive)(pid),
    }
    verdict = evaluate(sample, now=now)

    try:
        _write_json_atomic(sp, {"tick": sample["tick"], "updated": sample["updated"],
                                "sample_ts": (now or datetime.now()).strftime(_TS_FMT)})
    except Exception:  # noqa: BLE001
        pass  # 采样落盘失败不影响判定本身

    return {"observed": sample, **verdict}


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="数据流动看门狗: 判定引擎是否『活着但停流』")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON(含采样)")
    args = ap.parse_args(argv)
    r = gather()
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(f"[{r['level']}] {r['cause']}: {r['reason']}")
    # 退出码表达结论, 便于守护/外壳脚本消费: 0=OK 1=WARN 2=CRITICAL
    return {"OK": 0, "WARN": 1, "CRITICAL": 2}.get(r["level"], 3)


if __name__ == "__main__":
    raise SystemExit(_main())
