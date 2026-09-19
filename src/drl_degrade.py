# -*- coding: utf-8 -*-
"""DRL 降级链（DRL-4）—— 轻量模块，**不依赖 torch / gymnasium / stable_baselines3**.

四级链（用户 2026-09-19 设计）
    Level 0 正常      : 增量训练成功 -> 部署新模型（并记录"从降级恢复"）
    Level 1 保留旧模型 : 训练失败(ok=False) 或验证不通过 -> 继续用当前模型 + WARNING
    Level 2 回退上一版 : 当前模型文件缺失/损坏/无法加载 -> 扫描最近 ok=True 版本 + ERROR
    Level 3 暂停交易   : 无任何有效版本 -> **不生成当日 plan** + CRITICAL

设计要点
  ① **可逆**：后续训练成功后记录一次"恢复"事件（`level=0` + `recovered_from`），
     轨迹连续可查。
  ② **可审计**：每次降级/恢复都落 `data/drl_degrade_events.jsonl`，
     字段含 时间 / 级别 / 触发原因 / 处置动作 / 生效来源日。
  ③ **可告警**：Level 1→WARNING, Level 2→ERROR, Level 3→CRITICAL(**阻断当日 plan**)。
  ④ **不得静默**（本项的核心价值）：原先 `ok=False` 只落盘不告警、旧模型文件虽在但消费方
     不会自动回退 —— 这些都是静默路径；本模块把它们全部变成显式降级 + 告警 + 留痕。

关于「模型可用」的判据（**如实标注**）
  本模块把"模型可用"定义为**结构性代理判据**：
      `data/drl/<day>/model.zip` 存在 且 `train_meta.json` 可解析 且 `final_weights` 非空。
  真正"能否被加载"需要 torch 反序列化，本模块**不做**（否则会拖入 torch、并让
  不装 torch 的 CI core job 无法运行）。故此处是"文件层完好性"，不是"加载成功"。

关于「版本」的甄别（**实测判据, 非推测**）
  `data/drl/<YYYYMMDD>/` 下**混放**着两类 8 位数字目录。实测盘点（2026-09-19,
  共 32 个, 可用 `scripts/preflight_drl_degrade_realdata.py` 复现）:
    · 有实盘标记 12 个  —— 2026-08-26 ~ 2026-09-08（run_daily 盘后流水线产出）
    · 无实盘标记 20 个  —— 2015-06-12 ~ 2021-12-31 共 19 个回测/验证遗留
                          **外加 2026-08-25 一个实盘日**（见下方"已知偏差"）
  无标记目录里**有 7 个满足朴素"可用"判据**（model.zip + train_meta.ok=True）:
    20181019 / 20190628 / 20200323 / 20200630 / 20210210 / 20211231 / 20260825
  若 `latest_valid()` 只按日期倒序扫, 在**近端版本全部不可用**时会回退到 2021 年的
  回测模型 —— 这是"静默用错模型", 是比 L3 告警更危险的失败模式。
  故本模块叠加**两道保险**:
    ① `is_live_version()`: 必须有实盘流水线独有的标记文件（默认 `pre_drl_brief.json`）
    ② `FALLBACK_LOOKBACK_DAYS` 回退窗口: 只认"上一版", 不认"五年前那一版"
  被跳过的目录会**计数留痕**（`skipped_not_live` / `skipped_out_of_window`）而非默默忽略:
  万一将来判据失效, 账本上会显示"扫到 N 个但全部被跳过", 可直接定位,
  而不是表现为一句无从排查的"无有效版本"。

  **已知偏差（如实标注）**: `20260825` 是实盘日却**没有**标记文件（那天的流水线尚未
  产出 `pre_drl_brief.json`）。故它会被 ① 排除在回退候选之外。方向是**保守**的:
  它是最老的实盘版本, 只有在 08-26~09-05 全部不可用时才会被考虑, 此时本模块会
  选择 L3 暂停而非用它 —— 即"可能误停", 不会"静默用错"。要收紧需改用更稳的
  实盘判据（如显式 provenance 文件, 由流水线自 2026-09-19 起写入）。

关于「验证不通过」（**按用户要求：只记录数值，不定阈值**）
  用户明确：`验证不通过` 的判据需要统计阈值，必须"先记录 → 再评估(1-2 月) → 才决策"，
  **不得在本批次定死阈值**（否则重犯 METHOD-1 那个坑）。故本模块提供
  `record_validation()` 只把每轮验证数值落盘，且 `resolve()` **不使用**它触发任何降级。
"""
from __future__ import annotations

import datetime as dt
import json
import os

import config
from dataguard import warn_once

LEVEL_OK = 0
LEVEL_RETAIN = 1
LEVEL_FALLBACK = 2
LEVEL_HALT = 3

LEVEL_NAME = {0: "L0_正常", 1: "L1_保留旧模型", 2: "L2_回退上一有效版本",
              3: "L3_暂停交易"}
LEVEL_SEVERITY = {1: "WARNING", 2: "ERROR", 3: "CRITICAL"}
#: 每个级别一个告警 key（`warn_once` 按 key 去重"打印"，计数仍累加便于审计）
LEVEL_WARN_KEY = {1: "drl_degrade_L1", 2: "drl_degrade_L2", 3: "drl_degrade_L3"}

EVENT_LEDGER_NAME = "drl_degrade_events.jsonl"
VALIDATION_LEDGER_NAME = "drl_validation_metrics.jsonl"
POINTER_NAME = "current_model.json"

#: 实盘版本判据标记（实测: 32 个目录中, 19 个 2021 前遗留目录 0 命中, 见模块 docstring）。
#: 回测/验证遗留目录**不产出**此文件。可用 `DRL_LIVE_MARKER` 覆盖（判据变更时的逃生口）。
#: 已知偏差: 实盘日 20260825 早于该标记的引入, 也会被判为"非实盘"（保守方向, 见 docstring）。
LIVE_MARKER_NAME = "pre_drl_brief.json"
#: L2 回退的搜索窗口（自然日）。防"回退到几年前的遗留模型"的第二道保险。
#: 取 45 天: 覆盖正常连续交易日 + 长假断档, 又远小于任何遗留目录的年龄（≥ 5 年）。
FALLBACK_LOOKBACK_DAYS = 45


# --------------------------------------------------------------------- 路径

def _drl_root() -> str:
    return os.path.join(config.DATA_DIR, "drl")


def event_ledger_path() -> str:
    """降级事件账本。**每次动态解析** `config.DATA_DIR`（沙箱/测试可覆盖）。"""
    p = os.environ.get("DRL_DEGRADE_LEDGER")
    return p or os.path.join(config.DATA_DIR, EVENT_LEDGER_NAME)


def validation_ledger_path() -> str:
    p = os.environ.get("DRL_VALIDATION_LEDGER")
    return p or os.path.join(config.DATA_DIR, VALIDATION_LEDGER_NAME)


def pointer_path() -> str:
    p = os.environ.get("DRL_MODEL_POINTER")
    return p or os.path.join(_drl_root(), POINTER_NAME)


# --------------------------------------------------------------------- 判据

def _live_marker_name() -> str:
    return os.environ.get("DRL_LIVE_MARKER") or LIVE_MARKER_NAME


def is_live_version(day: str) -> bool:
    """该目录是否为**实盘逐日版本**（而非回测/验证遗留）。

    判据: 存在实盘流水线独有的标记文件（默认 `pre_drl_brief.json`）。
    实测 29 个目录零重叠, 见模块 docstring。
    """
    d = str(day or "").replace("-", "")
    if not (d.isdigit() and len(d) == 8):
        return False
    return os.path.isfile(os.path.join(_drl_root(), d, _live_marker_name()))


def version_usable(day: str, require_live: bool = True) -> bool:
    """结构性"可用"判据（**非**真正的反序列化加载，见模块 docstring）。

    `require_live=True`（默认）时额外要求 `is_live_version()` —— 回测遗留版本
    **永远不能被当作可部署的生产版本**（见模块 docstring 的实测依据）。
    """
    d = str(day or "").replace("-", "")
    if not (d.isdigit() and len(d) == 8):
        return False
    if require_live and not is_live_version(d):
        return False
    vdir = os.path.join(_drl_root(), d)
    if not os.path.isfile(os.path.join(vdir, "model.zip")):
        return False
    fp = os.path.join(vdir, "train_meta.json")
    if not os.path.isfile(fp):
        return False
    try:
        with open(fp, encoding="utf-8") as f:
            m = json.load(f)
    except Exception:  # noqa: BLE001
        return False
    return bool(m.get("final_weights")) and bool(m.get("ok", True))


def version_weights(day: str) -> "dict | None":
    d = str(day or "").replace("-", "")
    try:
        with open(os.path.join(_drl_root(), d, "train_meta.json"), encoding="utf-8") as f:
            m = json.load(f)
        w = m.get("final_weights")
        return {k: float(v) for k, v in w.items()} if isinstance(w, dict) and w else None
    except Exception:  # noqa: BLE001
        return None


def scan_versions(before_day: str, exclude_day: str | None = None,
                  window_days: int | None = None) -> dict:
    """扫描严格早于 `before_day`（且在窗口内）的候选版本。**只读, 不抛异常语义**: 无目录即空。

    返回 `{"candidates", "skipped_not_live", "skipped_out_of_window", "window_days",
    "scanned"}` —— 跳过项**计数留痕**, 便于"判据失效"时定位（见模块 docstring）。
    """
    win = FALLBACK_LOOKBACK_DAYS if window_days is None else int(window_days)
    out = {"candidates": [], "skipped_not_live": [], "skipped_out_of_window": [],
           "window_days": win, "scanned": 0}
    root = _drl_root()
    if not os.path.isdir(root):
        return out
    d8 = str(before_day or "").replace("-", "")
    ex = str(exclude_day or "").replace("-", "")
    if not (d8.isdigit() and len(d8) == 8):
        return out
    lo = ""
    if win > 0:
        try:
            lo = (dt.datetime.strptime(d8, "%Y%m%d").date()
                  - dt.timedelta(days=win)).strftime("%Y%m%d")
        except Exception:  # noqa: BLE001
            lo = ""
    for n in sorted(os.listdir(root), reverse=True):
        if not (os.path.isdir(os.path.join(root, n)) and n.isdigit() and len(n) == 8):
            continue
        if n >= d8 or n == ex:
            continue
        out["scanned"] += 1
        if lo and n < lo:
            out["skipped_out_of_window"].append(n)
            continue
        if not is_live_version(n):
            out["skipped_not_live"].append(n)
            continue
        out["candidates"].append(n)
    return out


def latest_valid(before_day: str, exclude_day: str | None = None,
                 window_days: int | None = None) -> "str | None":
    """严格早于 `before_day` 的最近一个**实盘且可用**版本日（可排除某日, 受限窗口）。

    三重约束: 早于当日 / 在回退窗口内 / 是实盘版本且结构性可用。
    """
    for n in scan_versions(before_day, exclude_day=exclude_day,
                           window_days=window_days)["candidates"]:
        if version_usable(n):
            return n
    return None


# --------------------------------------------------------------------- 指针

def load_pointer() -> dict:
    try:
        with open(pointer_path(), encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_pointer(day: str, source: str, level: int = LEVEL_OK) -> None:
    """记录"当前部署模型"指针 **+ 当前降级级别**。写失败不得影响主链路。

    为什么要存 `level`: "是否处于降级状态"必须能**直接读到**, 而不能靠"原指针版本的
    文件还在不在"去猜 —— 后者是弱代理: L1 保留旧模型时文件明明完好, 却确实处于降级,
    于是"从降级恢复"这件事会被漏记（可逆性轨迹断裂）。故每次 `resolve()` 都写回级别。
    """
    try:
        p = pointer_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"day": str(day or "").replace("-", ""),
                       "level": int(level),
                       "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "source": source}, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------- 账本

def record_event(level: int, reason: str, action: str, day: str,
                 source_day: str | None = None, extra: dict | None = None) -> dict:
    """写一条降级/恢复事件。**绝不抛异常**（主链路安全）。"""
    rec = {
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "day": str(day or "").replace("-", ""),
        "level": int(level),
        "level_name": LEVEL_NAME.get(int(level), str(level)),
        "severity": LEVEL_SEVERITY.get(int(level), "INFO"),
        "trigger": reason,
        "action": action,
        "effective_source_day": source_day,
        "blocked_plan": int(level) >= LEVEL_HALT,
    }
    if extra:
        rec.update(extra)
    try:
        p = event_ledger_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return rec


def record_validation(day: str, metrics: dict) -> dict:
    """记录一轮验证的**实际数值** —— **不判定通过与否**（阈值待标定, 见模块 docstring）。

    这是"验证不通过"这个触发条件的**数据准备**：先积累 1-2 个月，再定阈值。
    """
    rec = {
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "day": str(day or "").replace("-", ""),
        "kind": "validation_metrics",
        "threshold_applied": False,      # 明确: 本批次不施加任何阈值
        "note": "阈值待标定(METHOD-1: 须基于下游表现而非分布范围)",
        "metrics": {k: (float(v) if isinstance(v, (int, float)) else v)
                    for k, v in (metrics or {}).items()},
    }
    try:
        p = validation_ledger_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return rec


# --------------------------------------------------------------------- 主流程

def resolve(day: str, train_ok: bool, final_weights: "dict | None" = None,
            out_dir: str | None = None, fail_reason: "str | None" = None) -> dict:
    """按四级链决定当日**生效的因子权重**。返回可直接挂 `train_meta["degrade"]` 的 dict。

    注意: **不使用**任何统计阈值触发降级（触发条件全是结构性的: 训练失败 / 文件不可用 /
    无有效版本）—— 故 METHOD-1 在此不直接适用。

    Args:
        out_dir: **预留未用** —— 本模块所有路径都由 `config.DATA_DIR` 派生
            （故沙箱/测试可整体改指, 见各 `*_path()` 的 env 覆盖）。
        fail_reason: 上游失败的具体原因（如"数据不足 (<15 日)" / "未捕获异常: ..."）。
            仅用于**留痕**, 不改变分级逻辑。经 `drl_train._degrade_on_failure()` 传入。
    """
    day8 = str(day or "").replace("-", "")
    fr = (fail_reason or "").strip()[:200]
    try:
        # ---- Level 0: 训练成功 -> 部署新模型（若此前处于降级, 记一次"恢复"）----
        if train_ok and final_weights:
            prev = load_pointer()
            prev_day = str(prev.get("day") or "")
            prev_level = int(prev.get("level") or 0)
            # "是否刚从降级状态恢复" = 指针里**显式记着**的上一级别 > 0。
            # 不用"原版本文件是否还在"作判据: L1 保留旧模型时文件是好的, 那是弱代理, 会漏记恢复。
            recovered = prev_level > 0
            save_pointer(day8, "train_ok", level=LEVEL_OK)
            if recovered:
                rec = record_event(LEVEL_OK,
                                   f"训练成功, 从降级状态恢复(原级别 L{prev_level}, "
                                   f"原指针 {prev_day or '无'})",
                                   "部署新模型并记录恢复", day8, source_day=day8,
                                   extra={"recovered_from": prev_day or None,
                                          "recovered_from_level": prev_level})
            else:
                rec = {"level": LEVEL_OK, "level_name": LEVEL_NAME[0],
                       "severity": "INFO", "action": "部署新模型",
                       "effective_source_day": day8, "blocked_plan": False}
            return {"ok": True, "halt": False, "effective_weights": final_weights,
                    "source_day": day8, "recovered_from": prev_day or None,
                    "recovered_from_level": prev_level if recovered else None,
                    **rec}

        # ---- Level 1: 训练失败/未通过 -> **保留当前模型** ----
        cur = load_pointer()
        cur_day = str(cur.get("day") or "")
        if cur_day and version_usable(cur_day):
            w = version_weights(cur_day)
            if w:
                reason = fr or ("训练未成功(train_ok=False)" if not train_ok
                                else "训练无有效权重")
                # 保留旧模型(=day 不变), 但把级别写进指针 —— 否则"正处于降级"读不到
                save_pointer(cur_day, "retain", level=LEVEL_RETAIN)
                rec = record_event(LEVEL_RETAIN, reason, "保留当前模型, 不部署新模型",
                                   day8, source_day=cur_day)
                warn_once(LEVEL_WARN_KEY[1],
                          f"[{LEVEL_SEVERITY[1]}] DRL 降级 {LEVEL_NAME[1]}: {reason}; "
                          f"继续使用 {cur_day} 的模型; **当日不部署新模型**")
                return {"ok": True, "halt": False, "effective_weights": w,
                        "source_day": cur_day, "recovered_from": None, **rec}

        # ---- Level 2: 当前模型不可用 -> 扫描最近一个实盘且 ok=True 版本（受限窗口）----
        _scan = scan_versions(day8, exclude_day=day8)
        cand = latest_valid(day8, exclude_day=day8)
        if cand:
            w = version_weights(cand)
            if w:
                save_pointer(cand, "fallback", level=LEVEL_FALLBACK)
                _why = f"当前模型不可用(指针={cur_day or '无'})"
                if fr:
                    _why += f"; 上游失败={fr}"
                rec = record_event(LEVEL_FALLBACK, _why,
                                   f"回退到最近有效版本 {cand}", day8, source_day=cand,
                                   extra={"window_days": _scan["window_days"],
                                          "scanned": _scan["scanned"],
                                          "skipped_not_live": _scan["skipped_not_live"][:20],
                                          "skipped_out_of_window":
                                              _scan["skipped_out_of_window"][:20]})
                warn_once(LEVEL_WARN_KEY[2],
                          f"[{LEVEL_SEVERITY[2]}] DRL 降级 {LEVEL_NAME[2]}: "
                          f"当前模型不可用 -> 回退到 {cand}")
                return {"ok": True, "halt": False, "effective_weights": w,
                        "source_day": cand, "recovered_from": None, **rec}

        # ---- Level 3: 无任何有效版本 -> 暂停交易（不生成当日 plan）----
        _why = (f"无任何有效版本(指针={cur_day or '无'}, 扫描 {_scan['scanned']} 个目录: "
                f"非实盘遗留 {len(_scan['skipped_not_live'])} 个, "
                f"窗口外 {len(_scan['skipped_out_of_window'])} 个)")
        if fr:
            _why += f"; 上游失败={fr}"
        save_pointer(cur_day, "halt", level=LEVEL_HALT)
        rec = record_event(LEVEL_HALT, _why,
                           "**暂停交易: 不生成当日 plan**, 需人工介入", day8,
                           extra={"window_days": _scan["window_days"],
                                  "scanned": _scan["scanned"],
                                  "skipped_not_live": _scan["skipped_not_live"][:20],
                                  "skipped_out_of_window":
                                      _scan["skipped_out_of_window"][:20]})
        warn_once(LEVEL_WARN_KEY[3],
                  f"[{LEVEL_SEVERITY[3]}] DRL 降级 {LEVEL_NAME[3]}: 无有效模型 -> "
                  f"**阻断当日 plan**; 需人工介入")
        return {"ok": True, "halt": True, "effective_weights": None,
                "source_day": None, "recovered_from": None, **rec}
    except Exception as e:  # noqa: BLE001  绝不影响训练主链路
        return {"ok": False, "halt": False, "effective_weights": final_weights,
                "source_day": day8, "error": f"{type(e).__name__}: {e}"}


__all__ = ["LEVEL_OK", "LEVEL_RETAIN", "LEVEL_FALLBACK", "LEVEL_HALT",
           "LEVEL_NAME", "LEVEL_SEVERITY", "EVENT_LEDGER_NAME",
           "LIVE_MARKER_NAME", "FALLBACK_LOOKBACK_DAYS",
           "event_ledger_path", "validation_ledger_path", "pointer_path",
           "is_live_version", "version_usable", "version_weights",
           "scan_versions", "latest_valid",
           "load_pointer", "save_pointer", "record_event", "record_validation",
           "resolve"]
