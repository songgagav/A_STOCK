# -*- coding: utf-8 -*-
"""纯函数：计算每日行情自动补全计划。

这个模块只负责把已观测的引擎/H5i/日历状态交给既有回填判据，
不读取网络、不写数据库，也不启动任何子进程。
"""
from __future__ import annotations

from typing import Any

import backfill_trigger


def plan_daily_completion(*, engine_day: str | None,
                          h5i_watermark: str | None,
                          expected_day: str | None,
                          calendar_strength: str = "official",
                          enabled: bool = False,
                          today: str | None = None,
                          lookback_days: int | None = None) -> dict[str, Any]:
    """返回可审计的每日补全计划，不执行补全。

    A/B 判据和官方日历缺口计算集中在 ``backfill_trigger``，避免守护进程
    重新实现一套可能漂移的生产语义。
    """
    return backfill_trigger.decide(
        engine_day=engine_day,
        h5i_watermark=h5i_watermark,
        expected_day=expected_day,
        calendar_strength=calendar_strength,
        enabled=enabled,
        today=today,
        lookback=lookback_days,
    )


def run_daily_completion(*, primary_runner, fallback_runner, enabled: bool,
                         lookback_days: int, today: str | None = None) -> dict[str, Any]:
    """先执行主链，再按计划执行备用回补，并验证完整结果。

    ``primary_runner`` 和 ``fallback_runner`` 都是可注入的，以便测试时不
    触碰真实 StockDB、网络或 H5i。备用 runner 接收规范化的 ``YYYYMMDD``
    日期列表，并必须返回 ``written`` 与 ``verify.ok``。
    """
    out: dict[str, Any] = {
        "ok": False,
        "retryable": True,
        "primary": None,
        "decision": None,
        "fallback": None,
        "verification": None,
        "error": None,
    }
    try:
        primary = primary_runner()
        if not isinstance(primary, dict):
            raise TypeError("primary_runner 必须返回 dict")
        out["primary"] = primary
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"primary_runner: {type(exc).__name__}: {str(exc)[:200]}"
        return out

    engine_day = primary.get("engine_last_day") or primary.get("engine_day")
    h5i_day = primary.get("h5i_max_after") or primary.get("h5i_max_before")
    expected_day = primary.get("expected_day")
    strength = primary.get("calendar_strength") or "official"
    decision = plan_daily_completion(
        engine_day=engine_day,
        h5i_watermark=h5i_day,
        expected_day=expected_day,
        calendar_strength=strength,
        enabled=enabled,
        today=today,
        lookback_days=lookback_days,
    )
    out["decision"] = decision
    primary_ok = bool(primary.get("ok", True))

    if decision.get("action") in ("disabled", "no_gap"):
        out["ok"] = primary_ok
        out["retryable"] = not out["ok"]
        return out

    days = [str(day) for day in (decision.get("missing_days") or []) if day]
    if decision.get("action") != "trigger" or not days:
        out["error"] = "daily completion cannot decide a concrete missing-day set"
        return out

    if not primary_ok:
        out["error"] = "primary sync failed; fallback is not authorized"
        return out

    try:
        fallback = fallback_runner(days)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"fallback_runner: {type(exc).__name__}: {str(exc)[:200]}"
        return out
    if not isinstance(fallback, dict):
        out["error"] = "fallback_runner 必须返回 dict"
        return out
    out["fallback"] = fallback

    written = {str(day).replace("-", "") for day in (fallback.get("written") or [])}
    requested = {str(day).replace("-", "") for day in days}
    if not requested.issubset(written):
        out["error"] = ("fallback written 不完整: "
                         f"requested={sorted(requested)} written={sorted(written)}")
        return out

    verification = fallback.get("verify")
    out["verification"] = verification
    if not isinstance(verification, dict) or verification.get("ok") is not True:
        out["error"] = "fallback verify.ok 不是 true"
        return out
    if fallback.get("ok") is not True:
        out["error"] = "fallback 返回 ok=false"
        return out

    out["ok"] = True
    out["retryable"] = False
    return out
