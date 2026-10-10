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
import hashlib
from datetime import datetime, timedelta

HEALTH_STATES = ("NORMAL", "DEGRADED", "HALTED")


def _sha256_file(path: str) -> str | None:
    """返回文件内容 SHA-256；读不到时返回 None，由调用方显式报监测盲区。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _sha256_source_tree(path: str) -> tuple[str | None, int]:
    """对一个源码文件或目录下全部 `.py` 做确定性清单哈希。"""
    source = os.path.abspath(path)
    if os.path.isfile(source):
        return _sha256_file(source), 1
    try:
        files = []
        for root, dirs, names in os.walk(source):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            files.extend(os.path.join(root, n) for n in names if n.endswith(".py"))
        files.sort(key=lambda p: os.path.relpath(p, source).replace("\\", "/"))
        h = hashlib.sha256()
        for fp in files:
            rel = os.path.relpath(fp, source).replace("\\", "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            with open(fp, "rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
            h.update(b"\0")
        return h.hexdigest(), len(files)
    except Exception:  # noqa: BLE001
        return None, 0


# 进程加载模块时冻结的版本。之后即使磁盘文件被覆盖，这两个值也不会变化，
# 因而能识别「代码已经改了，但常驻守护仍在执行旧模块」这一静默部署失败。
_MODULE_SOURCE_PATH = os.path.dirname(os.path.abspath(__file__))
_MODULE_LOADED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_MODULE_LOADED_SHA256, _MODULE_LOADED_FILE_COUNT = _sha256_source_tree(_MODULE_SOURCE_PATH)


def module_code_version(path: str | None = None) -> dict:
    """比较本进程已加载模块与磁盘源码版本（纯文件读，不 reload）。"""
    source = os.path.abspath(path or _MODULE_SOURCE_PATH)
    disk_sha, file_count = _sha256_source_tree(source)
    error = None
    if _MODULE_LOADED_SHA256 is None:
        error = "模块加载时未能计算 src 源码树 SHA-256"
    elif disk_sha is None:
        error = f"当前磁盘源码不可读: {source}"
    return {
        "module": "src_tree",
        "scope": "src/**/*.py",
        "path": source,
        "loaded_file_count": _MODULE_LOADED_FILE_COUNT,
        "disk_file_count": file_count,
        "loaded_at": _MODULE_LOADED_AT,
        "loaded_sha256": _MODULE_LOADED_SHA256,
        "disk_sha256": disk_sha,
        "matches": (_MODULE_LOADED_SHA256 == disk_sha
                    if _MODULE_LOADED_SHA256 and disk_sha else None),
        "error": error,
    }

# ============================================================
# [2026-09-28] 门禁结论: 快照只读**已落盘的权威结论**, 不再自己重算
#
# 背景(实测): `gather()` 原先调 `datasource_gate.evaluate(sync_step=..., db_update_step=...)`
# **自己重算**, 且只喂两个**辅助源**; 唯一的关键源 `stockdb_engine` 需要
# `engine_probe`, 而快照路径从不传它 ⇒ 快照档位在结构上只能是
# `OK/DEGRADED/UNKNOWN`, **HALT 不可达**。后果实测:
#   · 交易时段读不到当日判定(输入取自 19:10 才写的 daily_summary) —— 09-24 的
#     `00:03..19:08` 共 177 条快照全是 UNKNOWN;
#   · 09-27 落盘结论是 `HALT/allow=False`(权威), 而快照重算得 `DEGRADED/allow=True`
#     —— **方向相反**, 因为真正致停的关键源恰好缺席。
#
# 现改为: 读 `data/daily/<最近>/daily_summary.json` 的 `steps.datasource_gate`
# (那是 `run_daily` 用**真实探针**判过并落盘的), 原样上报, 并带上
#   · `gate_verdict_at`     该结论的生成时刻(取自其 `checked_at`)
#   · `gate_verdict_day`    该结论所属的日目录(它描述的是**哪一轮**)
#   · `gate_verdict_is_today` 是否为当日结论
# 语义定为**「最近一轮门禁结论」** —— 不是"当日结论": 当日 run_daily 写入前结论尚不存在,
# 此时如实给出"截至哪一轮"比伪装成当日更有用。
# ============================================================

#: 门禁结论多久没更新就告警(小时)。判据依据本仓节奏:
#: 收盘管道约每交易日跑一次 ⇒ 正常工作日间隔约 24h; 取 **30h** 留出"跑得晚"的余量,
#: 同时能在"漏跑一整天"后的下一个自然日被发现(而不是等到第二个交易日)。
#: 为什么不让它沉默: 结论陈旧意味着"门禁这段时间**没有**在判定", 那是监测盲区,
#: 与「未生效」同族 —— 不可当作健康。
GATE_VERDICT_STALE_HOURS = 30.0

#: 覆盖陈旧阈值的环境变量名(便于运维按实际节奏调整, 不必改代码)。
ENV_GATE_STALE_HOURS = "GATE_VERDICT_STALE_HOURS"


def _gate_stale_hours() -> float:
    """读陈旧阈值, 非法/缺失回落默认(**不抛异常** —— 阈值写错不该让快照崩)。"""
    raw = os.environ.get(ENV_GATE_STALE_HOURS, "").strip()
    if not raw:
        return GATE_VERDICT_STALE_HOURS
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return GATE_VERDICT_STALE_HOURS
    return v if v > 0 else GATE_VERDICT_STALE_HOURS


def _parse_verdict_ts(v) -> "datetime | None":
    """把落盘的时刻字符串解析成 naive datetime; 取不到返回 None(不猜)。"""
    if isinstance(v, datetime):
        return v
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def read_gate_verdict(daily_dir: str, today: "datetime | None" = None) -> dict:
    """读**最近一轮**已落盘的门禁结论。**纯文件读**, 不探针、不重算。

    返回:
      {'value': dict|None,   # 原样的 datasource_gate 结论(含 level/allow/reasons)
       'at': str|None,       # 该结论的生成时刻(其 checked_at)
       'day': str|None,      # 结论所属日目录 YYYYMMDD —— 说明它描述的是哪一轮
       'is_today': bool,     # 结论时间戳与目录均属于今天
       'label': str|None,    # 当日 / 截至昨日 / 截至 YYYY-MM-DD
       'age_hours': float|None,
       'stale': bool,        # 超过 `_gate_stale_hours()` 未更新
       'error': str|None}    # **判定失败**的原因(≠结论本身是坏的)

    `error` 非空表示"这次快照没能拿到任何权威结论"(例如目录为空、
    或最近一轮还没跑到门禁那一步) —— 调用方应据此报**不可判定**, 而不是健康。
    """
    today = today or datetime.now()
    out = {"value": None, "at": None, "day": None, "is_today": False,
           "label": None,
           "age_hours": None, "stale": False, "error": None}
    try:
        if not os.path.isdir(daily_dir):
            out["error"] = f"无 data/daily 目录: {daily_dir}"
            return out
        today8 = today.strftime("%Y%m%d")
        cands = sorted(d for d in os.listdir(daily_dir)
                       if len(d) == 8 and d.isdigit()
                       and d <= today8
                       and os.path.isdir(os.path.join(daily_dir, d)))
        if not cands:
            out["error"] = "data/daily 下没有任何日目录"
            return out
        # 从最近往回找**第一个含门禁结论**的目录 —— 用"最近一轮"而不是"今天",
        # 因为当日 run_daily 写入前今天的结论尚不存在; 往回找更有信息量。
        for day8 in reversed(cands):
            fp = os.path.join(daily_dir, day8, "daily_summary.json")
            if not os.path.isfile(fp):
                continue
            try:
                with open(fp, encoding="utf-8-sig") as f:
                    steps = (json.load(f).get("steps") or {})
            except Exception:                  # noqa: BLE001
                continue
            v = steps.get("datasource_gate")
            if not isinstance(v, dict) or not v.get("level"):
                continue
            out["value"] = v
            out["day"] = day8
            out["at"] = v.get("checked_at") or ""
            ts = _parse_verdict_ts(out["at"])
            if ts is not None:
                out["age_hours"] = (today - ts).total_seconds() / 3600.0
                out["stale"] = out["age_hours"] > _gate_stale_hours()
                out["is_today"] = (day8 == today8 and ts.strftime("%Y%m%d") == today8
                                   and out["age_hours"] >= 0)
                if ts.strftime("%Y%m%d") != day8:
                    out["error"] = f"结论 checked_at 与日目录不一致: {out['at']!r} / {day8}"
                elif out["age_hours"] < 0:
                    out["error"] = f"结论 checked_at 晚于当前时间: {out['at']!r}"
                elif out["is_today"]:
                    out["label"] = "当日"
                elif day8 == (today - timedelta(days=1)).strftime("%Y%m%d"):
                    out["label"] = "截至昨日"
                else:
                    out["label"] = f"截至 {day8[:4]}-{day8[4:6]}-{day8[6:]}"
            else:
                # 取不到时刻 => 无法判断新鲜度。**如实标记**, 不当作"新鲜"。
                out["stale"] = False
                out["error"] = f"结论缺可解析的 checked_at: {out['at']!r}"
            return out
        out["error"] = (f"{len(cands)} 个日目录里没有任何一轮落盘过门禁结论"
                        "(可能从未跑到该步骤)")
        return out
    except Exception as e:                     # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out


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
       'l3_today': int,
       # [2026-09-28] 门禁结论(最近一轮, 读自落盘, 不重算):
       'datasource': {...} | {},
       'gate_verdict_at': str | None,      # 该结论的生成时刻
       'gate_verdict_day': str | None,     # 它描述的是哪一轮(YYYYMMDD)
       'gate_verdict_is_today': bool,
       'gate_verdict_label': str | None,
       'gate_verdict_age_hours': float | None,
       'gate_verdict_stale': bool,
       'gate_verdict_error': str | None,
       'code_version': {loaded_sha256, disk_sha256, matches, ...}}
    返回 {'state': NORMAL|DEGRADED|HALTED, 'reasons': [str]}。
    未知字段一律忽略（向前兼容）。
    """
    reasons: list = []

    # 部署完整性：常驻进程不会自动重载已 import 的模块。磁盘与加载版本分叉时，
    # 「代码已修改」不等于「生产已生效」，必须显式降级并要求重启守护。
    code = snap.get("code_version")
    if isinstance(code, dict):
        if code.get("matches") is False:
            reasons.append(
                "运行时 src 源码树与磁盘代码版本不一致"
                f"(loaded={str(code.get('loaded_sha256') or '')[:12]}, "
                f"disk={str(code.get('disk_sha256') or '')[:12]}) —— 必须重启守护使代码生效")
        elif code.get("matches") is None:
            reasons.append("运行时模块版本无法判定: " +
                           str(code.get("error") or "SHA-256 不可用"))

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

    if snap.get("live_source") in ("duckdb_reference_held", "h5i_reference_held",
                                  "unknown_reference_held"):
        reasons.append("持仓被静态参考价兜底(非实时估值, P2-LIVESRC)")
    elif snap.get("live_source") == "price_missing_held":
        reasons.append("持仓缺价且参考价未补齐(估值不完整, P2-LIVESRC)")

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

    # [2026-09-28 改] 数据源健康门禁: 消费**已落盘的权威结论**(最近一轮), 不重算。
    # 语义 = 「最近一轮门禁结论」: 当日结论尚未产生时, 如实标注
    # "截至哪一轮"比伪装成当日更有用。
    ds = (snap.get("gate_verdict") if "gate_verdict" in snap
          else snap.get("datasource")) or {}
    ds_level = ds.get("level")
    gv_at = snap.get("gate_verdict_at")
    gv_day = snap.get("gate_verdict_day")
    gv_err = snap.get("gate_verdict_error")
    _new_path = ("gate_verdict_at" in snap) or ("gate_verdict_day" in snap)
    # 结论所属轮次的标签: 用于把"这是上一轮"讲清楚
    _when = ""
    if gv_day and not snap.get("gate_verdict_is_today"):
        _when = f"[截至 {gv_day} 那一轮, 非当日] "
    if ds_level == "HALT":
        reasons.append(
            _when + "数据源健康门禁判定 HALT(连续失败达阈值, 应停止摄入与依赖数据的下游动作): "
            + "; ".join(ds.get("reasons") or [])[:220])
    elif ds_level == "DEGRADED":
        for r in (ds.get("reasons") or [])[:3]:
            reasons.append(f"{_when}数据源健康: {r}")
    elif ds_level == "UNKNOWN":
        # UNKNOWN 是**门禁自己给的**结论(它判了, 但没判出任何源) —— 如实转达。
        reasons.append(_when + "数据源健康门禁判定 UNKNOWN"
                       "(没有任何数据源被判定 —— 不等于健康)")
    elif _new_path and not ds_level:
        # 新路径下**取不到任何权威结论** ⇒ 不可判定。
        # 这与"门禁说 UNKNOWN"不同: 那是判过而没判出源, 这是**根本没拿到结论**。
        # 两者都必须可见(≠健康), 但原因不同、要查的地方也不同。
        reasons.append("数据源健康门禁结论**取不到**(无可用的已落盘结论 —— 不可判定 ≠ 健康)"
                       + (f": {gv_err}" if gv_err else ""))
    if gv_err and ds_level:
        reasons.append(f"数据源健康门禁结论时间/来源异常: {gv_err}")
    # 新鲜度: 结论陈旧 => 这段时间门禁**没有**在判定, 属监测盲区(与"未生效"同族)。
    if snap.get("gate_verdict_stale"):
        _age = snap.get("gate_verdict_age_hours")
        _a = f"{_age:.1f}h" if isinstance(_age, (int, float)) else "未知"
        reasons.append(
            f"数据源健康门禁结论已 {_a} 未更新(阈值 {_gate_stale_hours():.0f}h, "
            f"最近一轮 {gv_day or '未知'} {gv_at or ''}) —— 该窗口内没有在判定")

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


def detect_gate_verdict_conflict(daily_dir: str,
                                 today: "datetime | None" = None,
                                 snapshot_path: "str | None" = None) -> dict:
    """**冲突检测(独立探针, 不进快照热路径)**: 快照读到的结论 vs 落盘结论。

    ## 按设计, 正常情况下二者应当**完全一致**

    因为快照**就是**从落盘文件读的(不再重算) —— 同一个来源不可能给出两个值。
    本函数用于日常比对和不变量检查:

      · 若它报警, 说明有人**又**在快照路径里加了重算(或改了读取来源),
        而那正是本次修掉的缺陷形态 —— 属于"结构性回归"的早期信号;
      · 它同时跨**多个日目录**比对, 可发现"读了 A 轮却声称是 B 轮"这类错位。

    ## 为什么不放进 `gather()`

    `gather()` 每 ~5 分钟跑一次, 而"权威结论"需要用**真实引擎探针**才能重算
    (`datasource_gate.probe_engine()` 要起子进程)。把它塞进热路径等于每 5 分钟
    起一个探针进程, 且会与 `run_daily` 的账本计数语义纠缠。故此处**只读文件**:
    一份 daily_summary 与一份已发布快照, 不探针、不重算。

    `snapshot_path` 默认取与 daily 同级的 health/state.json。快照缺失/损坏
    单独作为不可比对项返回, 不伪称一致；实际值/时刻不等才算冲突。
    """
    today = today or datetime.now()
    details: list = []
    checked = 0
    snapshot_error = None
    snapshot_compared = False
    try:
        if not os.path.isdir(daily_dir):
            return {"ok": False, "conflict": False, "checked": 0,
                    "details": [f"无 data/daily 目录: {daily_dir}"]}
        cands = sorted(d for d in os.listdir(daily_dir)
                       if len(d) == 8 and d.isdigit()
                       and d <= today.strftime("%Y%m%d")
                       and os.path.isdir(os.path.join(daily_dir, d)))
        for day8 in cands:
            fp = os.path.join(daily_dir, day8, "daily_summary.json")
            if not os.path.isfile(fp):
                continue
            try:
                with open(fp, encoding="utf-8-sig") as f:
                    steps = (json.load(f).get("steps") or {})
            except Exception:                  # noqa: BLE001
                continue
            v = steps.get("datasource_gate")
            if not isinstance(v, dict) or not v.get("level"):
                continue
            checked += 1
            # 不变量①: 结论自带 allow, 必须与 level 自洽(HALT <=> allow=False)。
            # 这条能抓住"手改了 level 却没改 allow"以及写入侧将来改语义不同步。
            lvl, allow = v.get("level"), v.get("allow")
            if lvl == "HALT" and allow is not False:
                details.append(f"{day8}: level=HALT 但 allow={allow!r} (应 False)")
            if lvl in ("OK", "DEGRADED") and allow is not True:
                details.append(f"{day8}: level={lvl} 但 allow={allow!r} (应 True)")
            # 不变量②: checked_at 必须可解析 —— 否则新鲜度判据失效(会被当成"新鲜")
            if _parse_verdict_ts(v.get("checked_at")) is None:
                details.append(f"{day8}: checked_at 不可解析: {v.get('checked_at')!r}")
            # 不变量③: HALT 必须给出 halt_sources, 否则"为什么停手"无从归因
            if lvl == "HALT" and not (v.get("halt_sources") or []):
                details.append(f"{day8}: level=HALT 但没有 halt_sources —— 无法归因")
        rec = read_gate_verdict(daily_dir, today=today)
        # 不变量④: "最近一轮"的选取必须与逐目录扫描的结果一致(防错位)
        if rec.get("value") and cands:
            expect = None
            for day8 in reversed(cands):
                fp = os.path.join(daily_dir, day8, "daily_summary.json")
                if not os.path.isfile(fp):
                    continue
                try:
                    with open(fp, encoding="utf-8-sig") as f:
                        steps = (json.load(f).get("steps") or {})
                except Exception:              # noqa: BLE001
                    continue
                v = steps.get("datasource_gate")
                if isinstance(v, dict) and v.get("level"):
                    expect = day8
                    break
            if expect and rec.get("day") != expect:
                details.append(f"最近一轮错位: read_gate_verdict={rec.get('day')} "
                               f"而逐目录扫描={expect}")
        # 已发布快照 vs 权威落盘结论。只读两份文件, 绝不重算门禁。
        if rec.get("value") is not None:
            snap_fp = snapshot_path or os.path.join(os.path.dirname(os.path.normpath(daily_dir)),
                                                      "health", "state.json")
            pub = read_published(snap_fp, now=today)
            if not pub["available"]:
                snapshot_error = pub["error"] or "健康快照不可用"
            else:
                observed = pub["observed"]
                snapshot_error = pub.get("error")
                snapshot_value = observed.get("gate_verdict", observed.get("datasource"))
                if not isinstance(snapshot_value, dict) or not snapshot_value:
                    snapshot_error = "健康快照没有门禁结论字段"
                else:
                    snapshot_compared = True
                    if snapshot_value != rec["value"]:
                        details.append("健康快照门禁值与最近一轮落盘结论不一致"
                                       f" (快照 ts={pub.get('ts')}, 落盘={rec.get('day')} {rec.get('at')})")
                    if observed.get("datasource") != snapshot_value:
                        details.append("健康快照 gate_verdict 与兼容字段 datasource 不一致")
                    if "gate_verdict_at" in observed and observed["gate_verdict_at"] != rec["at"]:
                        details.append("健康快照 gate_verdict_at 与落盘 checked_at 不一致")
                    if "gate_verdict_day" in observed and observed["gate_verdict_day"] != rec["day"]:
                        details.append("健康快照 gate_verdict_day 与落盘日目录不一致")
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "conflict": False, "checked": checked,
                "details": [f"{type(e).__name__}: {e}"],
                "snapshot_error": snapshot_error,
                "snapshot_compared": snapshot_compared}
    return {"ok": True, "conflict": bool(details), "checked": checked,
            "details": details, "snapshot_error": snapshot_error,
            "snapshot_compared": snapshot_compared}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def gather() -> dict:
    """采集真实可观测量 → 装配状态（供 CLI / 面板 / 清单使用）。

    注意: 本函数触碰文件系统/引擎, **不在 CI 里跑**; 可测性由纯函数 `assemble` 保证。
    """
    root = _repo_root()
    snap: dict = {"tick_ms": None, "freshness_ok": None,
                  "live_source": None, "l3_today": 0}

    # -1) 部署版本指纹。必须最先采集，即使后续探针失败也能回答「当前跑的是哪版」。
    snap["code_version"] = module_code_version()

    # 0) [2026-09-28 改] 数据源健康门禁: **读已落盘的权威结论, 不重算**。
    #
    # 原先这里是 `_DG.evaluate(sync_step=..., db_update_step=...)` —— 自己重算,
    # 且只喂两个辅助源; 唯一关键源 `stockdb_engine` 需要 engine_probe, 而这里从不传
    # ⇒ 快照档位结构上只能 OK/DEGRADED/UNKNOWN, **HALT 不可达**;
    # 且输入取自"今天"的 daily_summary(19:10 才写) ⇒ 交易时段读的是上一轮。
    # 现已改为直接汇报 `run_daily` 用**真实探针**判过并落盘的结论。
    try:
        import sys as _sys
        import datetime as _dt
        _sys.path.insert(0, os.path.join(root, "src"))
        _rec = read_gate_verdict(os.path.join(root, "data", "daily"),
                                 today=_dt.datetime.now())
        snap["gate_verdict"] = _rec.get("value")
        snap["gate_verdict_at"] = _rec.get("at")
        snap["gate_verdict_day"] = _rec.get("day")
        snap["gate_verdict_is_today"] = _rec.get("is_today")
        snap["gate_verdict_label"] = _rec.get("label")
        snap["gate_verdict_age_hours"] = _rec.get("age_hours")
        snap["gate_verdict_stale"] = _rec.get("stale")
        snap["gate_verdict_error"] = _rec.get("error")
        # 兼容: 老的消费者读 `snap["datasource"]`。原样保留它, 内容**就是**落盘结论
        # (不再是重算结果), 于是老读取方也自动得到权威值。
        snap["datasource"] = _rec.get("value") or {}
    except Exception as _e:  # noqa: BLE001
        snap["gate_verdict"] = None
        snap["gate_verdict_error"] = f"门禁结论采集异常: {type(_e).__name__}: {_e}"
        snap["datasource"] = {}

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
            # [2026-09-25] 把「落后几个交易日」也带出来 —— 它是本项**唯一可告警的量化值**。
            #
            # 为什么必须补: `freshness_ok` 是布尔的, 而门禁的降级判据是
            # 「落后 > 发布宽限(1 个交易日)」。2026-09-25 实测厂商引擎停在 09-22、
            # 落后 **2 个交易日**, 系统全程 **DEGRADED 但 allow=True** ⇒
            # `DataSourceHalt`(只在 allow==0 时响)**不会响**; 而
            # `TableStaleDaily` 用的是 5 天且按**自然日**算(实测 daily_bars 3.08 天)
            # ⇒ 也要等到第 6 天才响。两条规则都盖不住"落后 2~3 天"这段窗口,
            # 于是「厂商连续几天没发数据」这件事**没有任何告警**。
            # 有 `lag_trading_days` 之后就能加一条按**交易日**、阈值 1 的规则。
            snap["lag_trading_days"] = f.get("lag_trading_days")
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
