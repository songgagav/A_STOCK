"""Pure in-memory source-tier switching decisions.

The router decides and applies state transitions only. It does not connect to
sources, write staging data, or grant trading permission.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from .metadata import SourceTier


DEFAULT_COVERAGE_THRESHOLD = 0.99
DEFAULT_PRIMARY_RECOVERY_DAYS = 2
DEFAULT_SHADOW_VALIDATION_DAYS = 5


def _coerce_tier(value: Any) -> SourceTier:
    if isinstance(value, SourceTier):
        return value
    if isinstance(value, str):
        try:
            return SourceTier(value)
        except ValueError as exc:
            raise ValueError(f"invalid current source tier: {value!r}") from exc
    raise ValueError("current source tier must be primary, backup, or shadow")


def _validate_threshold(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be between 0 and 1")
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return float(value)


def _validate_days(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _quality_failure(signal: Mapping[str, Any], coverage_threshold: float) -> str | None:
    if signal.get("connection_ok") is not True:
        return "connection_failure"

    if signal.get("data_stale") is True:
        return "stale_data"
    data_day = signal.get("data_day")
    expected_day = signal.get("expected_day")
    if not isinstance(data_day, str) or not isinstance(expected_day, str):
        return "stale_data"
    if data_day < expected_day:
        return "stale_data"

    coverage = signal.get("coverage")
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, (int, float))
        or not math.isfinite(coverage)
        or coverage < coverage_threshold
    ):
        return "insufficient_coverage"

    if signal.get("critical_fields_ok") is not True:
        return "critical_fields_empty"
    if signal.get("critical_fields") == []:
        return "critical_fields_empty"

    sampling_ok = signal.get("sampling_check_ok", signal.get("sampling_ok"))
    if sampling_ok is not True:
        return "sampling_check_failed"
    return None


def _decision(
    action: str, reason: str, current: SourceTier, target: SourceTier
) -> dict[str, str]:
    return {
        "action": action,
        "reason": reason,
        "from_tier": current.value,
        "to_tier": target.value,
    }


def decide_switch(
    state: Mapping[str, Any],
    signal: Mapping[str, Any],
    *,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    primary_recovery_days: int = DEFAULT_PRIMARY_RECOVERY_DAYS,
    shadow_validation_days: int = DEFAULT_SHADOW_VALIDATION_DAYS,
) -> dict[str, str]:
    """Return an auditable source-tier decision without mutating ``state``."""
    if not isinstance(state, Mapping) or not isinstance(signal, Mapping):
        raise ValueError("state and signal must be mappings")
    threshold = _validate_threshold(coverage_threshold, "coverage_threshold")
    recovery_days = _validate_days(primary_recovery_days, "primary_recovery_days")
    validation_days = _validate_days(shadow_validation_days, "shadow_validation_days")
    current = _coerce_tier(state.get("current"))

    failure = _quality_failure(signal, threshold)
    if current is SourceTier.PRIMARY:
        if failure is not None:
            return _decision("switch_to_backup", failure, current, SourceTier.BACKUP)
        return _decision("stay", "all_checks_pass", current, current)

    if current is SourceTier.BACKUP:
        primary_healthy = signal.get("primary_health_ok") is True
        healthy_days = signal.get("primary_healthy_days", 0)
        if primary_healthy and isinstance(healthy_days, int) and not isinstance(healthy_days, bool):
            if healthy_days >= recovery_days:
                return _decision("switch_to_primary", "primary_recovered", current, SourceTier.PRIMARY)
            return _decision("stay", "primary_recovery_wait", current, current)
        return _decision("stay", "backup_active", current, current)

    shadow_approved = signal.get("shadow_approved") is True
    validation_passed = signal.get("shadow_validation_passed") is True
    observed_days = signal.get("shadow_validation_days", 0)
    if (
        shadow_approved
        and validation_passed
        and isinstance(observed_days, int)
        and not isinstance(observed_days, bool)
        and observed_days >= validation_days
    ):
        return _decision("switch_to_backup", "shadow_approved", current, SourceTier.BACKUP)
    return _decision("stay", "shadow_validation_pending", current, current)


def apply_decision(state: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    """Apply one decision to a copied state, rejecting stale decisions."""
    if not isinstance(state, Mapping) or not isinstance(decision, Mapping):
        raise ValueError("state and decision must be mappings")
    current = _coerce_tier(state.get("current"))
    if decision.get("from_tier") != current.value:
        raise ValueError("decision from_tier does not match current state")

    action = decision.get("action")
    target = _coerce_tier(decision.get("to_tier"))
    if action == "stay":
        if target is not current:
            raise ValueError("stay decision must retain the current tier")
        return dict(state)
    if action not in {"switch_to_backup", "switch_to_primary"}:
        raise ValueError(f"unknown switch action: {action!r}")

    expected_target = (
        SourceTier.BACKUP if action == "switch_to_backup" else SourceTier.PRIMARY
    )
    if target is not expected_target:
        raise ValueError("decision action and target tier are inconsistent")

    updated = dict(state)
    updated["current"] = target
    return updated
