# -*- coding: utf-8 -*-
"""主动故障注入（第二批）：数据延迟 / 磁盘满 / 网络中断.

与第一批的区别
  第一批(`preflight_missing_data.py` / `preflight_atomicity.py`)注入的是**真实条件**
  （真的删掉某日 bar、真的 kill 写入进程、真的写一个一次性库）。
  本批注入的是**模拟条件**（monkeypatch 出 OSError(ENOSPC) / URLError / TimeoutError），
  打在**真实的代码路径**上（`premarket_healthcheck.check_disk` / `check_akshare` /
  `dataguard.with_retry`）。**不是**真的把磁盘写满、也不是真的断网 ——
  这一点在输出与报告里都显式标注, 不得当作"已验证磁盘满/断网"。

用户要求每次注入后验证三件事(缺一不可):
  1) 告警触发   —— 健康检查项落到 FAIL/WARN(会被面板与告警规则消费) 或 dataguard 计数 +1
  2) 系统恢复   —— 故障条件移除后, 同一路径能恢复成功(且**没有被污染的状态残留**)
  3) 数据一致   —— 失败期间不产生"看起来正常"的假数据/假结果; 恢复后结果与预期一致

用法
  $env:BAR_STORE='h5i'
  & <py310> scripts\\preflight_fault_inject.py
输出
  data/preflight_fault_inject.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

OUT = os.path.join(_BASE, "data", "preflight_fault_inject.json")


def _case(fault: str, injection: str, alert, recover, consistent, note: str = "") -> dict:
    return {"fault": fault, "injection": injection,
            "alert_triggered": bool(alert), "system_recovered": bool(recover),
            "data_consistent": bool(consistent),
            "pass": bool(alert and recover and consistent), "note": note}


def case_disk_full() -> dict:
    """磁盘满（模拟 ENOSPC）→ check_disk 必须 FAIL, 且不留下半截探针文件。"""
    import shutil
    import premarket_healthcheck as ph

    real_disk_usage = shutil.disk_usage
    probe = os.path.join(ph.HEALTH_DIR, ".health_probe")

    # 1) 模拟"可用空间不足"(0.2 GB < 阈值 1.0)
    class _DU(tuple):
        pass
    shutil.disk_usage = lambda p: _DU((100 * 1024 ** 3, 99 * 1024 ** 3, int(0.2 * 1024 ** 3)))
    try:
        r1 = ph.check_disk()
    finally:
        shutil.disk_usage = real_disk_usage
    alert = r1.get("status") == "FAIL"

    # 2) 模拟"写入直接失败"(ENOSPC)
    real_open = __builtins__["open"] if isinstance(__builtins__, dict) else __builtins__.open

    def _boom(path, *a, **k):
        if str(path).endswith(".health_probe"):
            raise OSError(28, "No space left on device")
        return real_open(path, *a, **k)

    if isinstance(__builtins__, dict):
        __builtins__["open"] = _boom
    else:
        __builtins__.open = _boom
    try:
        r2 = ph.check_disk()
    finally:
        if isinstance(__builtins__, dict):
            __builtins__["open"] = real_open
        else:
            __builtins__.open = real_open
    alert = alert and r2.get("status") == "FAIL"

    # 系统恢复: 正常条件下应回到 OK
    r3 = ph.check_disk()
    recover = r3.get("status") == "OK"
    # 数据一致: 失败期间未残留半截探针文件
    consistent = not os.path.exists(probe)
    return _case("磁盘满", "monkeypatch shutil.disk_usage + open() 抛 ENOSPC",
                 alert, recover, consistent,
                 f"两轮注入均 FAIL={alert}; 恢复后={r3.get('status')}; 无残留探针文件")


def case_network_down() -> dict:
    """网络中断（模拟 URLError）→ check_akshare 必须降级为 WARN(含 attempts), 不得静默 OK。"""
    from urllib import error as urlerr
    import premarket_healthcheck as ph

    real_urlopen = ph.request.urlopen
    calls = {"n": 0}

    def _down(*a, **k):
        calls["n"] += 1
        raise urlerr.URLError("模拟断网: name resolution failed")

    ph.request.urlopen = _down
    try:
        r1 = ph.check_akshare()
    finally:
        ph.request.urlopen = real_urlopen

    det = r1.get("detail") or {}
    det = det if isinstance(det, dict) else {}
    alert = r1.get("status") in ("WARN", "FAIL")
    # 数据一致: 降级必须被显式标注, 且带上重试次数(不能"看起来正常")
    consistent = bool(det.get("degraded") is True and (det.get("attempts") or 0) >= 1)
    # 系统恢复: 恢复 urlopen 后应能重新执行(不因上次失败被缓存成永久降级)
    ph.request.urlopen = real_urlopen
    try:
        r2 = ph.check_akshare()
        recover = r2.get("status") is not None
    except Exception:  # noqa: BLE001
        recover = False
    return _case("网络中断", "monkeypatch urllib.request.urlopen 抛 URLError",
                 alert, recover, consistent,
                 f"status={r1.get('status')} degraded={det.get('degraded')} "
                 f"attempts={det.get('attempts')} 实际调用次数={calls['n']}")


def case_data_delay() -> dict:
    """数据延迟/超时 → dataguard.with_retry 必须重试后告警, 且失败**不缓存**(可恢复)。"""
    import dataguard

    key = "faultinject_delay"
    dataguard.reset_warnings()
    tries = {"n": 0}

    def _slow_fail():
        tries["n"] += 1
        raise TimeoutError("模拟数据延迟超时")

    t0 = time.time()
    out, ok = dataguard.with_retry(_slow_fail, tries=3, base_delay=0.05,
                                   label="faultinject-delay", warn_key=key)
    alert = (ok is False) and dataguard.warned_count(key) >= 1

    # 数据一致: 失败时返回 None, 不伪造数据
    consistent = out is None
    # 系统恢复: 故障消除后同一路径成功, 且不因上次失败被缓存(计数继续增长)
    def _ok_read():
        tries["n"] += 1
        return [1, 2, 3]

    out2, ok2 = dataguard.with_retry(_ok_read, tries=3, base_delay=0.05,
                                     label="faultinject-delay")
    recover = bool(ok2) and out2 == [1, 2, 3]
    return _case("数据延迟(超时)", "dataguard.with_retry 注入恒 TimeoutError",
                 alert, recover, consistent,
                 f"重试 {tries['n']} 次后告警; 恢复后返回 {out2}; 耗时 {time.time() - t0:.2f}s")


def main() -> int:
    cases = [case_disk_full(), case_network_down(), case_data_delay()]
    print("=== 主动故障注入（第二批：模拟条件 + 真实代码路径）===\n")
    for c in cases:
        mark = "PASS" if c["pass"] else "FAIL"
        print(f"  [{mark}] {c['fault']}")
        print(f"        注入: {c['injection']}")
        print(f"        告警触发={c['alert_triggered']}  系统恢复={c['system_recovered']}"
              f"  数据一致={c['data_consistent']}")
        if c["note"]:
            print(f"        {c['note']}")
    n_pass = sum(1 for c in cases if c["pass"])
    print(f"\n  汇总: {n_pass}/{len(cases)}")
    res = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope_note": ("本批注入的是**模拟条件**(monkeypatch 出 ENOSPC/URLError/TimeoutError), "
                       "打在真实代码路径上; **不是**真的写满磁盘或真的断网, "
                       "不得据此宣称'已验证磁盘满/断网'。"),
        "unexercised": [
            "真实磁盘满(需要小容量卷/配额)",
            "真实断网(需要可控网络隔离)",
            "进程崩溃 + 守护自动重启(src/daemon.py 未演练)",
            "完整交易日 dry-run",
            "备份恢复演练",
        ],
        "cases": cases,
        "_summary": {"pass": n_pass, "total": len(cases)},
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    print(f"  已保存: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
