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
  DEGRADED  有任一降级原因（延迟/数据滞后/账户静态估值），但策略未被 L3 停摆
  HALTED    当日存在 L3 降级事件 —— 策略维度停摆，权重最高
"""
from __future__ import annotations

import json
import os

HEALTH_STATES = ("NORMAL", "DEGRADED", "HALTED")

# 阈值（证据见模块 docstring）
TICK_P50_MS_LIMIT = 1000.0   # 健康 p50≈7ms, 退化 p50≈12s ⇒ 取 1s
TICK_P95_MS_LIMIT = 2000.0   # 健康 p95≈20ms, 退化 p95≈14s ⇒ 取 2s


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

    if snap.get("freshness_ok") is False:
        reasons.append("引擎数据未追平最后一个已收盘交易日(厂商未发布当日数据)")

    if snap.get("live_source") == "duckdb_reference_held":
        reasons.append("持仓被静态参考价兜底(非实时估值, P2-LIVESRC)")

    l3 = int(snap.get("l3_today") or 0)
    if l3:
        reasons.append(f"当日 {l3} 个 L3 降级事件(策略停摆)")

    state = "HALTED" if l3 else ("DEGRADED" if reasons else "NORMAL")
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
    except Exception as e:  # noqa: BLE001
        snap["freshness_ok"] = None
        snap["engine_error"] = f"{type(e).__name__}: {e}"

    # 3) L3 事件 —— 降级账本
    try:
        import drl_degrade as D
        lp = D.event_ledger_path()
        if os.path.isfile(lp):
            with open(lp, encoding="utf-8-sig") as f:
                n = 0
                for ln in f:
                    if not ln.strip():
                        continue
                    r = json.loads(ln)
                    if r.get("level") == 3 and str(r.get("day", "")).endswith(
                            __import__("datetime").datetime.now().strftime("%Y%m%d")):
                        n += 1
                snap["l3_today"] = n
    except Exception:  # noqa: BLE001
        pass

    return {"observed": snap, **assemble(snap)}


if __name__ == "__main__":
    print(json.dumps(gather(), ensure_ascii=False, indent=2))
