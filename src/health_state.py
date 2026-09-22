# -*- coding: utf-8 -*-
"""多指标自动降级状态机（路线图 #2 的第一增量：**状态装配器**）。

设计约束（用户既定）
--------------------
1. **阈值全部来自本仓观测，不照抄外部项目** —— 每个阈值下面都标注其证据来源；
2. 第一增量只做**装配**：把已存在、各自已有判据的可观测量汇成一个显式状态
   `NORMAL / DEGRADED / HALTED` + 原因列表。不新造指标，不新定"拍脑袋"阈值。

本仓观测证据（2026-09-21 实测）
--------------------------------
· 延迟双峰: 盘中健康时 `tick_ms.p50 ≈ 7.3ms / p95 ≈ 20ms`（13:08 采样）；
  代理退化时 `p50 ≈ 11.9s / p95 ≈ 14.4s`（15:02 采样，AtlasCore 抖动，见 P2-ATLASPROXY）。
  两峰相差三个数量级 ⇒ 阈值取 `p50 > 1s` 或 `p95 > 2s`（落在空旷地带，具体值不敏感）。
· 数据滞后: 复用 `engine_bars_sync.freshness()` 的判据「引擎是否追平最后一个已收盘交易日」
  —— 该判据有自己的事故史（初版拿日历年尾当基准的范畴错误），不再另造。
· 账户估值: 复用 `P2-LIVESRC` 的三态 —— `duckdb_reference_held` 即「持仓被静态价兜底」。
· 策略: 当日 `drl_degrade_events.jsonl` 里出现 L3（决策 D 的门禁语义：不产出新信号）。

状态语义
--------
  NORMAL    全部可观测量正常
  DEGRADED  有任一降级原因（延迟/数据滞后/账户静态估值），但系统仍在工作
  HALTED    **系统实际上已停止工作**：当日 L3 事件（策略停摆）或盘中数据停流
            （引擎死了/主循环死锁/行情源冻住，见 #4 `flow_watchdog`）—— 权重最高

归因纪律（2026-09-21 修，第二增量）
------------------------------------
第一增量的 `gather()` 把**探针自身失败**也写成 `freshness_ok=False`，于是
"`ModuleNotFoundError: stock_sdk`"（本机 shell 没继承 User 级 `STOCKDB_ROOT`）
被装配成 **「厂商未发布当日数据」** —— 这是本仓事故史上那类**范畴错误**的重演：
把失败归给了错误的因，运维会去等厂商，而真正要做的是配好环境。

故新鲜度现在是**三态**，各给各的原因、各给各的行动：
```text
freshness_ok=True                    正常
freshness_ok=False 且探针成功         厂商确实滞后 => 等厂商
探针失败(engine_error 非空)           监测盲区   => 查环境/查引擎, 不冒充"滞后"
```
探针失败仍计入 DEGRADED（监测盲区必须可见），但原因文本**明确区分**，见
`_classify_probe_error()`。

发布者 / 读取者分离（证据驱动）
--------------------------------
实测探针成本：`engine_available()` 查 4 只参考股全历史，SDK 可用时 **1.71s**
（SDK 缺失时 0.11s 快速失败）；它走 AtlasCore 本地代理，而该代理有
**15.5s/请求** 的实测抖动史（登记册 P2-ATLASPROXY），最坏情况可达数十秒。

而面板前端**每 3 秒**轮询一次 `/api/health` —— 把探针放进请求路径会直接
拖垮面板。故：

  `publish()`          采集 + 原子落盘（**慢**，需要 STOCKDB_ROOT 环境）
                       只由有环境的定期进程调用：守护进程 5 分钟看护节奏、收盘清单
  `read_published()`   纯文件读（**快**，无引擎探针、不需要任何环境变量）
                       面板 / 清单读取方专用，并如实给出快照 `age_s`

**发布者需要环境、读取者不需要** —— 这不是洁癖：守护进程以 LocalSystem 运行，
`STOCKDB_ROOT` 是本机 **User 级** 环境变量（Machine 级为空），靠 NSSM
`AppEnvironmentExtra` 显式注入才有；面板可能被外部脚本以别的身份拉起。
快照把"环境依赖"收敛在发布者一处。

快照自身的`age_s` 只作**信息性**标记（`stale`），不改状态语义 ——
"数据停流多久算事故"归 **#4 看门狗**（`src/flow_watchdog.py`，阈值 120s 及其
三条本仓证据见该模块 docstring）。本模块**消费**它的结论（`assemble` 的 `flow` 入参），
不重复定义阈值：同一件事只允许有一个数字。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

HEALTH_STATES = ("NORMAL", "DEGRADED", "HALTED")

# 阈值（证据见模块 docstring）
TICK_P50_MS_LIMIT = 1000.0   # 健康 p50≈7ms, 退化 p50≈12s ⇒ 取 1s
TICK_P95_MS_LIMIT = 2000.0   # 健康 p95≈20ms, 退化 p95≈14s ⇒ 取 2s

#: 快照"信息性陈旧"阈值（秒）。依据发布节奏：守护进程每 20 轮（约 5 分钟）发布一次，
#: 30 分钟 = 连续 6 次未发布。**只影响 `stale` 标记，不改变 state** ——
#: 盘中"停流多久算事故"的判据归 #4 看门狗，本模块不抢答。
PUBLISH_MAX_AGE_S = 1800.0

#: 探针失败的两类原因 → 两种不同的运维动作（见 _classify_probe_error）
_PROBE_ERR_CONFIG = ("stock_sdk", "pybao", "no module named", "厂商 sdk 不可用")
_PROBE_ERR_UNREACH = ("连接失败", "connect", "refused", "timed out", "timeout", "不可达")


def _classify_probe_error(err: str) -> str:
    """把探针错误分类成 'config' | 'unreachable' | 'unknown'。

    存在的意义不是好看，而是**行动不同**：
      config      本机环境没配好（STOCKDB_ROOT/pybao），去配环境，别等厂商
      unreachable 引擎/代理不通（见 P2-ATLASPROXY 的代理抖动），去查进程与代理
      unknown     未知错误，原文照报，人工判断
    """
    e = (err or "").lower()
    if any(k in e for k in _PROBE_ERR_CONFIG):
        return "config"
    if any(k in e for k in _PROBE_ERR_UNREACH):
        return "unreachable"
    return "unknown"


def assemble(snap: dict) -> dict:
    """把观测快照装配成单一健康状态（**纯函数**，CI 可测）。

    snap 形如:
      {'tick_ms': {'p50': ms, 'p95': ms} | None,
       'freshness_ok': bool | None,
       'live_source': str | None,
       'l3_today': int}
    返回 {'state': NORMAL|DEGRADED|HALTED, 'reasons': [str]}。
    未知字段一律忽略（向前兼容）。
    """
    reasons: list = []

    t = snap.get("tick_ms") or {}
    p50 = t.get("p50")
    p95 = t.get("p95")
    if isinstance(p50, (int, float)) and p50 > TICK_P50_MS_LIMIT:
        reasons.append(f"tick 延迟异常: p50={p50:.0f}ms (>{TICK_P50_MS_LIMIT:.0f}ms)")
    if isinstance(p95, (int, float)) and p95 > TICK_P95_MS_LIMIT:
        reasons.append(f"tick 延迟尾部异常: p95={p95:.0f}ms (>{TICK_P95_MS_LIMIT:.0f}ms)")

    # 新鲜度三态: 探针失败 ≠ 厂商滞后（归因纪律, 见模块 docstring）
    err = str(snap.get("engine_error") or "").strip()
    if err:
        kind = snap.get("probe_error_kind") or _classify_probe_error(err)
        why = {"config": "配置缺失: STOCKDB_ROOT/pybao 未就绪(P2-LAKEROOT)",
               "unreachable": "引擎/代理不可达(见 P2-ATLASPROXY)",
               "unknown": err[:110]}.get(kind, err[:110])
        reasons.append(f"数据新鲜度无法判定(引擎探针不可用 — {why})")
    elif snap.get("freshness_ok") is False:
        reasons.append("引擎数据未追平最后一个已收盘交易日(厂商未发布当日数据)")

    if snap.get("live_source") == "duckdb_reference_held":
        reasons.append("持仓被静态参考价兜底(非实时估值, P2-LIVESRC)")

    l3 = int(snap.get("l3_today") or 0)
    if l3:
        reasons.append(f"当日 {l3} 个 L3 降级事件(策略停摆)")

    # #4 看门狗结论（进程活着但数据停流）—— 归因文本原样带上, 不在这一层改写
    flow = snap.get("flow") or {}
    flevel = flow.get("level")
    if flevel == "CRITICAL":
        reasons.append(f"盘中数据停流(#4 看门狗): {flow.get('reason')}")
    elif flevel == "WARN":
        reasons.append(f"数据流动无法判定(#4 看门狗): {flow.get('reason')}")

    # [2026-09-22 数据源健康门禁] 只消费**结论**, 不在这里重跑判定 ——
    # 判定要读 run_daily 产物与引擎探针, 属采集层(`datasource_gate.evaluate`)的职责。
    # 这里只负责把它的 HALT 抬成 HALTED: "数据源连续失败到该停手" 与"策略停摆/数据停流"
    # 同属**系统实际已停止工作**, 该进同一个档位。
    ds = snap.get("datasource") or {}
    ds_level = ds.get("level")
    if ds_level == "HALT":
        reasons.append(
            "数据源健康门禁判定 HALT(连续失败达阈值, 应停止摄入与依赖数据的下游动作): "
            + "; ".join(ds.get("reasons") or [])[:220])
    elif ds_level == "DEGRADED":
        for r in (ds.get("reasons") or [])[:3]:
            reasons.append(f"数据源健康: {r}")
    elif ds_level == "UNKNOWN":
        reasons.append("数据源健康门禁未生效(没有任何数据源被判定 —— 不等于健康)")

    # [2026-09-22 死手开关接线] 消费 `deadman_switch.verdict()` 的结论。
    # 为什么它必须在这里被消费: 其它监控(心跳/看门狗/健康快照)都要求**监测者自己还活着**,
    # 而它判的是"本该出现的 tick 没出现" —— **失联本身即是证据**。本案(daemon 消失 4.6 小时
    # 而全系统零告警)正是其它监控原理上覆盖不到的情形, 因为监测者与被监测者一起没了。
    #
    # OVERDUE  => DEGRADED(有组件停摆。不用 HALTED: 单组件停摆不等于系统已停止工作)
    # UNKNOWN  => DEGRADED(**账本为空 = 这套监控从未生效, 不是健康**)。
    #             这一档刻意不写成 NORMAL: "没有告警"与"没有在监控"必须可区分。
    dm = snap.get("deadman") or {}
    dm_level = dm.get("level")
    if dm_level == "OVERDUE":
        reasons.append("死手开关 OVERDUE(组件超过 3× 周期没有 tick): "
                       + "; ".join(str(x) for x in (dm.get("reasons") or []))[:220])
    elif dm_level == "UNKNOWN":
        reasons.append("死手开关 UNKNOWN(账本为空, 这套监控从未生效 —— 不等于健康)")
    elif "deadman" in snap and not dm:
        # **区分"没这一项"与"有这一项但取不到"**: 前者是向后兼容(老快照/纯函数调用
        # 本就不带这个键, 不该因此被判 DEGRADED), 后者是采集层异常, 必须说出来。
        # 判据取 `"deadman" in snap` 而非 `dm is None` —— 因为 `gather()` 采集失败时
        # 恰恰就是把 `snap["deadman"] = None` 写进去, 两者必须分开。
        reasons.append("死手开关结论取不到(采集层异常) —— 不等于健康")

    # HALTED 语义 = **系统实际上已停止工作**: 策略停摆(L3) 或 盘中数据停流(引擎死了/死锁/
    # 行情源冻住)。这两种情况下账户既不会正确估值、也不会正确执行, 故同为 HALTED;
    # 归因文本各自保留, 由消费方区分该去查策略还是查引擎。
    halted = bool(l3) or flevel == "CRITICAL" or ds_level == "HALT"
    state = "HALTED" if halted else ("DEGRADED" if reasons else "NORMAL")
    return {"state": state, "reasons": reasons}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def gather() -> dict:
    """采集真实可观测量 → 装配状态（供 CLI / 面板 / 清单使用）。

    注意: 本函数触碰文件系统/引擎, **不在 CI 里跑**; 可测性由纯函数 `assemble` 保证。
    """
    root = _repo_root()
    snap: dict = {"tick_ms": None, "freshness_ok": None,
                  "live_source": None, "l3_today": 0}

    # 0) [2026-09-22 数据源健康门禁] 采集其结论(只读本地账本与最近一次日更产物,
    #    不重跑引擎探针 —— 探针在下面第 2 步已经跑了)。失败不影响下面的采集。
    try:
        import sys as _sys
        import datetime as _dt
        _sys.path.insert(0, os.path.join(root, "src"))
        import datasource_gate as _DG
        _sync = _dbu = None
        _p = os.path.join(root, "data", "daily",
                          _dt.datetime.now().strftime("%Y%m%d"),
                          "daily_summary.json")
        if os.path.isfile(_p):
            with open(_p, encoding="utf-8-sig") as _f:
                _steps = (json.load(_f).get("steps") or {})
            _sync = _steps.get("engine_bars_sync")
            _dbu = _steps.get("db_update")
        snap["datasource"] = _DG.evaluate(sync_step=_sync, db_update_step=_dbu)
    except Exception as _e:  # noqa: BLE001
        snap["datasource"] = {"level": "UNKNOWN",
                              "reasons": [f"门禁采集异常: {type(_e).__name__}: {_e}"]}

    # 1) tick_ms / live_source —— 来自 live_state.json（引擎每 tick 写）
    lv_fp = os.path.join(root, "data", "live_state.json")
    if os.path.isfile(lv_fp):
        try:
            with open(lv_fp, encoding="utf-8-sig") as f:
                lv = json.load(f)
            snap["tick_ms"] = (lv.get("ops") or {}).get("tick_ms")
            snap["live_source"] = lv.get("live_source")
            snap["live_data_ts"] = lv.get("data_ts")
        except Exception:  # noqa: BLE001
            pass

    # 2) 数据滞后 —— 引擎探针（与守护闸门同源）
    try:
        import sys
        sys.path.insert(0, os.path.join(root, "src"))
        import engine_bars_sync as E
        p = E.engine_available()
        if p.get("ok"):
            f = E.freshness(p.get("day"))
            snap["freshness_ok"] = f.get("ok")
            snap["engine_day"] = p.get("day")
            snap["expected_day"] = f.get("expected_day")
        else:
            snap["freshness_ok"] = False
            snap["engine_error"] = p.get("error")
            snap["probe_error_kind"] = _classify_probe_error(str(p.get("error") or ""))
    except Exception as e:  # noqa: BLE001
        snap["freshness_ok"] = None
        snap["engine_error"] = f"{type(e).__name__}: {e}"
        snap["probe_error_kind"] = _classify_probe_error(str(e))

    # 3) L3 事件 —— 降级账本
    try:
        import drl_degrade as D
        lp = D.event_ledger_path()
        if os.path.isfile(lp):
            with open(lp, encoding="utf-8-sig") as f:
                n = 0
                today = datetime.now().strftime("%Y%m%d")
                for ln in f:
                    if not ln.strip():
                        continue
                    r = json.loads(ln)
                    if r.get("level") == 3 and str(r.get("day", "")).endswith(today):
                        n += 1
                snap["l3_today"] = n
    except Exception:  # noqa: BLE001
        pass

    # 4) 数据流动 —— #4 看门狗（进程活着但数据停流）
    #    复用同一个判据, 不在这里重写阈值; 看门狗自己会读 live_state + 引擎 pid。
    try:
        import sys
        sys.path.insert(0, os.path.join(root, "src"))
        import flow_watchdog as FW
        snap["flow"] = FW.gather()
    except Exception as e:  # noqa: BLE001
        snap["flow"] = None
        snap["flow_error"] = f"{type(e).__name__}: {e}"

    # 5) 死手开关 —— **失联本身即是证据**
    #    [2026-09-22 修] 这个模块此前**只被喂 tick、从不被求值**: 全仓检索
    #    `deadman_switch.verdict` / `_DMS.verdict` 零命中, 唯一生产调用点是
    #    daemon 的 `beat()`。于是本仓唯一一个"不需要监测者自己活着"的机制 ——
    #    其它监控(心跳/看门狗/健康快照)都要求监测者还在跑, 而它判的是
    #    "本该出现的 tick 没出现" —— **恰恰是唯一没接线的那个**。
    #    当天实测: daemon 从 12:25 起消失 4.6 小时(机器 16:08 重启), 全系统零告警;
    #    我手工跑一次 `verdict()` 立刻得到 OVERDUE(obs_stack 已 66866s 无 tick)。
    #    判据完全正确, 就是没人去问。
    try:
        import sys
        sys.path.insert(0, os.path.join(root, "src"))
        import deadman_switch as DMS
        dv = DMS.verdict()
        snap["deadman"] = {"level": dv.get("level"),
                           "overdue": [i.get("component") for i in dv.get("overdue") or []],
                           "unknown": [i.get("component") for i in dv.get("unknown") or []],
                           "reasons": dv.get("reasons") or []}
    except Exception as e:  # noqa: BLE001
        snap["deadman"] = None
        snap["deadman_error"] = f"{type(e).__name__}: {e}"

    return {"observed": snap, **assemble(snap)}


# ---------------------------------------------------------------- 发布 / 读取

def state_path(path: str | None = None) -> str:
    """快照落盘位置（默认 `data/health/state.json`）。"""
    return path or os.path.join(_repo_root(), "data", "health", "state.json")


def _write_atomic(fp: str, payload: dict) -> None:
    """原子写：先写 `.tmp` 再 `os.replace`（同盘替换是原子的）。"""
    d = os.path.dirname(fp)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)


def publish(path: str | None = None, payload: dict | None = None) -> dict:
    """采集 + **原子**落盘（发布者；慢，需 STOCKDB_ROOT 环境）。

    用 `os.replace` 原子替换：读取方要么看到旧快照、要么看到新快照，
    永远不会读到写了一半的文件（面板每 3 秒就读一次，半截 JSON 会让卡片炸掉）。

    `payload` 仅供测试注入（默认 None = 真实采集）；生产路径永远是真实采集。
    """
    if payload is None:
        payload = gather()
        payload["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload["pid"] = os.getpid()
    _write_atomic(state_path(path), payload)
    return payload


def read_published(path: str | None = None, max_age_s: float = PUBLISH_MAX_AGE_S,
                   now=None) -> dict:
    """读快照（读取者；**快**，无引擎探针、不依赖任何环境变量）。

    返回 `{available, state, reasons, age_s, stale, ts, observed, error}`。
    快照缺失/损坏时 `available=False` 且 `state=None` —— **绝不凭空编造状态**：
    读不到就如实说读不到，由消费方决定怎么显示。
    """
    fp = state_path(path)
    out = {"available": False, "state": None, "reasons": [], "age_s": None,
           "stale": None, "ts": None, "observed": {}, "error": None}
    if not os.path.isfile(fp):
        out["error"] = f"快照不存在: {fp}（请先运行 health_state.publish()）"
        return out
    try:
        with open(fp, encoding="utf-8-sig") as f:
            j = json.load(f)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"快照不可解析: {type(e).__name__}: {e}"
        return out
    out.update(available=True, state=j.get("state"), reasons=list(j.get("reasons") or []),
               ts=j.get("ts"), observed=j.get("observed") or {})
    try:
        age = ((now or datetime.now())
               - datetime.strptime(str(j.get("ts")), "%Y-%m-%d %H:%M:%S")).total_seconds()
        out["age_s"] = age
        out["stale"] = age > max_age_s
    except Exception:  # noqa: BLE001
        # 时间戳读不出来时**不能声称新鲜** —— 未知一律按陈旧处理, 并说明原因
        out["stale"] = True
        out["error"] = f"快照时间戳不可解析: {j.get('ts')!r}"
    return out


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="健康状态机: 采集/发布/读取")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--publish", action="store_true", help="采集并原子落盘快照")
    g.add_argument("--read", action="store_true", help="读已发布快照(快, 无探针)")
    args = ap.parse_args(argv)
    if args.publish:
        r = publish()
    elif args.read:
        r = read_published()
    else:
        r = gather()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    # 退出码表达状态, 便于守护/外壳脚本消费: 0=NORMAL 1=DEGRADED 2=HALTED
    st = r.get("state")
    return {"NORMAL": 0, "DEGRADED": 1, "HALTED": 2}.get(st, 3)


if __name__ == "__main__":
    raise SystemExit(_main())
