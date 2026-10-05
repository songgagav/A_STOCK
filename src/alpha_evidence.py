"""可审计的 Alpha 证据汇总。

本模块只读取研究产物并做结构/口径检查，不计算新因子、不改权重，也不
把结果接入交易链。缺少输入、前视窗口或 fallback 引擎都必须显式暴露。
"""

from __future__ import annotations

import math
from datetime import date
from statistics import mean
from typing import Any, Mapping, Sequence


DEFAULT_HORIZONS = (1, 5, 10, 20, 60, 120)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _status(name: str, value: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": value, **extra}


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def summarize_ic_term_structure(
    payload: Mapping[str, Any] | None,
    *,
    required_horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_ic_mean: float = 0.02,
    min_positive_ratio: float = 0.50,
    min_days: int = 10,
) -> dict[str, Any]:
    """检查 IC 期限结构是否具备足够的正向、跨期限证据。"""

    if payload is None:
        return _status("ic_term_structure", "unavailable", reasons=["artifact_missing"])
    if not isinstance(payload, Mapping) or not isinstance(payload.get("summary"), Mapping):
        return _status("ic_term_structure", "invalid", reasons=["summary_missing"])

    summary = payload["summary"]
    horizons: list[dict[str, Any]] = []
    failed: list[int] = []
    passed: list[int] = []
    structural_errors: list[str] = []

    for horizon in required_horizons:
        raw = summary.get(str(horizon), summary.get(horizon))
        if not isinstance(raw, Mapping):
            structural_errors.append(f"horizon_{horizon}_missing")
            continue
        ic_mean = _as_number(raw.get("mean"))
        positive = _as_number(raw.get("pos"))
        n_days = _as_number(raw.get("n"))
        if ic_mean is None or positive is None or n_days is None or n_days <= 0:
            structural_errors.append(f"horizon_{horizon}_invalid_metrics")
            continue
        if positive < 0 or positive > n_days:
            structural_errors.append(f"horizon_{horizon}_invalid_positive_count")
            continue
        positive_ratio = positive / n_days
        row = {
            "horizon": int(horizon),
            "mean_ic": ic_mean,
            "positive_days": int(positive),
            "n_days": int(n_days),
            "positive_ratio": positive_ratio,
            "passed": bool(
                n_days >= min_days
                and ic_mean >= min_ic_mean
                and positive_ratio >= min_positive_ratio
            ),
        }
        horizons.append(row)
        (passed if row["passed"] else failed).append(int(horizon))

    if structural_errors:
        return _status(
            "ic_term_structure",
            "invalid",
            horizons=horizons,
            passed_horizons=passed,
            failed_horizons=failed,
            reasons=structural_errors,
        )

    ok = len(horizons) == len(required_horizons) and not failed
    return _status(
        "ic_term_structure",
        "pass" if ok else "fail",
        horizons=horizons,
        passed_horizons=passed,
        failed_horizons=failed,
        reasons=[] if ok else ["positive_ic_gate_not_met"],
    )


def summarize_forward_windows(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    min_valid_windows: int = 10,
    as_of_date: date | None = None,
    horizon_days: int | None = None,
) -> dict[str, Any]:
    """只接受真正的前向、非 fallback 窗口，并检查日期方向。

    当窗口尚未达到指定的 ``horizon_days`` 观察期时，显式标记为
    ``pending_maturity``，而不是把“未来还没有发生”误报为数据失败。
    这是保守的日历无关下界：日历日尚未达到 horizon 时，一定不可能
    已经拥有足够的交易日；达到下界后仍应由实际窗口结果决定。
    """

    if rows is None:
        return _status("forward_windows", "unavailable", reasons=["artifact_missing"])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return _status("forward_windows", "invalid", reasons=["payload_not_a_list"])

    valid: list[Mapping[str, Any]] = []
    excluded: list[dict[str, str]] = []
    pending_maturity: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            excluded.append({"index": str(index), "reason": "row_not_an_object"})
            continue
        day = str(row.get("day") or "")
        day_date = _iso_date(day)
        stats = row.get("stats")
        if row.get("ok") is not True:
            error = str(row.get("error") or row.get("reason") or "")
            if (
                not error
                and as_of_date is not None
                and horizon_days is not None
                and horizon_days > 0
                and day_date is not None
                and (as_of_date - day_date).days < horizon_days
            ):
                pending_maturity.append({"day": day, "reason": "pending_maturity"})
            else:
                excluded.append({"day": day, "reason": "window_not_ok"})
        elif row.get("window_mode") != "forward":
            excluded.append({"day": day, "reason": "wrong_window_mode"})
        elif bool(row.get("fallback")):
            excluded.append({"day": day, "reason": "fallback_engine"})
        elif not day or not isinstance(stats, Mapping):
            excluded.append({"day": day, "reason": "missing_window_metadata"})
        else:
            start = str(stats.get("start_date") or "")
            day_date = _iso_date(day)
            start_date = _iso_date(start)
            end = str(stats.get("end_date") or "")
            end_date = _iso_date(end) if end else None
            if not start:
                excluded.append({"day": day, "reason": "missing_start_date"})
            elif day_date is None or start_date is None or (end and end_date is None):
                excluded.append({"day": day, "reason": "invalid_date"})
            elif start_date < day_date:
                excluded.append({"day": day, "reason": "window_starts_before_decision_day"})
            elif end_date is not None and end_date < start_date:
                excluded.append({"day": day, "reason": "window_end_before_start"})
            else:
                valid.append(row)

    numeric_returns = [
        value
        for row in valid
        for value in [_as_number((row.get("stats") or {}).get("total_return"))]
        if value is not None
    ]
    numeric_sharpe = [
        value
        for row in valid
        for value in [_as_number((row.get("stats") or {}).get("sharpe_ratio"))]
        if value is not None
    ]
    numeric_drawdown = [
        value
        for row in valid
        for value in [_as_number((row.get("stats") or {}).get("max_ddpercent"))]
        if value is not None
    ]
    passed = len(valid) >= min_valid_windows
    return _status(
        "forward_windows",
        "pass" if passed else "fail",
        valid_window_count=len(valid),
        excluded_count=len(excluded),
        excluded=excluded,
        pending_maturity_count=len(pending_maturity),
        pending_maturity=pending_maturity,
        mean_total_return=mean(numeric_returns) if numeric_returns else None,
        mean_sharpe=mean(numeric_sharpe) if numeric_sharpe else None,
        worst_drawdown=min(numeric_drawdown) if numeric_drawdown else None,
        reasons=[] if passed else ["insufficient_valid_forward_windows"],
    )


def summarize_attribution(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    min_rows: int = 5,
) -> dict[str, Any]:
    """汇总归因产物；归因不足时不能伪装成完整证据。"""

    if rows is None:
        return _status("attribution", "unavailable", reasons=["artifact_missing"])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return _status("attribution", "invalid", reasons=["payload_not_a_list"])
    if len(rows) < min_rows:
        return _status(
            "attribution", "fail", row_count=len(rows), reasons=["insufficient_attribution_rows"]
        )

    def values(key: str) -> list[float]:
        return [
            number
            for row in rows
            if isinstance(row, Mapping)
            for number in [_as_number(row.get(key))]
            if number is not None
        ]

    return _status(
        "attribution",
        "pass",
        row_count=len(rows),
        mean_win_rate=mean(values("win_rate")) if values("win_rate") else None,
        mean_turnover=mean(values("to_ratio")) if values("to_ratio") else None,
        mean_profit_loss_ratio=mean(values("pl_ratio")) if values("pl_ratio") else None,
        mean_basket_return=mean(values("basket")) if values("basket") else None,
        mean_backtest_return=mean(values("bt")) if values("bt") else None,
        reasons=[],
    )


def evaluate_alpha_evidence(
    ic_payload: Mapping[str, Any] | None,
    forward_windows: Sequence[Mapping[str, Any]] | None,
    attribution: Sequence[Mapping[str, Any]] | None,
    *,
    min_ic_mean: float = 0.02,
    min_positive_ratio: float = 0.50,
    min_ic_days: int = 10,
    min_forward_windows: int = 10,
    min_attribution_rows: int = 5,
    as_of_date: date | None = None,
    forward_horizon_days: int | None = None,
) -> dict[str, Any]:
    """构建 Alpha 证据报告；``evidence_ready`` 只代表可人工复核。"""

    checks = {
        "ic_term_structure": summarize_ic_term_structure(
            ic_payload,
            min_ic_mean=min_ic_mean,
            min_positive_ratio=min_positive_ratio,
            min_days=min_ic_days,
        ),
        "forward_windows": summarize_forward_windows(
            forward_windows,
            min_valid_windows=min_forward_windows,
            as_of_date=as_of_date,
            horizon_days=forward_horizon_days,
        ),
        "attribution": summarize_attribution(attribution, min_rows=min_attribution_rows),
    }
    statuses = {check["status"] for check in checks.values()}
    if "unavailable" in statuses:
        status = "unavailable"
        next_action = "collect_missing_artifacts"
    elif "invalid" in statuses:
        status = "invalid"
        next_action = "inspect_artifacts"
    elif any(value != "pass" for value in statuses):
        status = "not_promotable"
        next_action = "inspect_signal"
    else:
        status = "evidence_ready"
        next_action = "human_review"

    return {
        "schema_version": 1,
        "status": status,
        "next_action": next_action,
        "execution_change": False,
        "thresholds": {
            "min_ic_mean": min_ic_mean,
            "min_positive_ratio": min_positive_ratio,
            "min_ic_days": min_ic_days,
            "min_forward_windows": min_forward_windows,
            "min_attribution_rows": min_attribution_rows,
            "forward_horizon_days": forward_horizon_days,
        },
        "checks": checks,
    }
