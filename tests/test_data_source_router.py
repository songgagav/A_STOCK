from __future__ import annotations

import pytest

from data_sources.metadata import SourceTier
from data_sources.router import (
    DEFAULT_COVERAGE_THRESHOLD,
    DEFAULT_PRIMARY_RECOVERY_DAYS,
    DEFAULT_SHADOW_VALIDATION_DAYS,
    apply_decision,
    decide_switch,
)


def healthy_signal(**overrides):
    signal = {
        "connection_ok": True,
        "data_day": "2026-09-30",
        "expected_day": "2026-09-30",
        "coverage": 1.0,
        "critical_fields_ok": True,
        "sampling_check_ok": True,
    }
    signal.update(overrides)
    return signal


def test_switch_trigger_connection_failure():
    state = {"current": SourceTier.PRIMARY}
    signal = {"connection_ok": False}

    decision = decide_switch(state, signal)

    assert decision["action"] == "switch_to_backup"
    assert decision["reason"] == "connection_failure"
    assert decision["to_tier"] == SourceTier.BACKUP.value


def test_missing_connection_health_does_not_pass_primary():
    decision = decide_switch({"current": SourceTier.PRIMARY}, {})

    assert decision["action"] == "switch_to_backup"
    assert decision["reason"] == "connection_failure"


def test_incomplete_health_signal_does_not_pass_primary():
    decision = decide_switch({"current": SourceTier.PRIMARY}, {"connection_ok": True})

    assert decision["action"] == "switch_to_backup"
    assert decision["reason"] == "stale_data"


@pytest.mark.parametrize(
    ("signal", "reason"),
    [
        (healthy_signal(data_day="2026-09-29"), "stale_data"),
        (healthy_signal(coverage=0.98), "insufficient_coverage"),
        (healthy_signal(critical_fields_ok=False), "critical_fields_empty"),
        (healthy_signal(sampling_check_ok=False), "sampling_check_failed"),
    ],
)
def test_quality_failure_switches_primary_to_backup(signal, reason):
    decision = decide_switch({"current": SourceTier.PRIMARY}, signal)

    assert decision["action"] == "switch_to_backup"
    assert decision["reason"] == reason


@pytest.mark.parametrize(
    ("coverage", "should_switch"),
    [(0.98, True), (DEFAULT_COVERAGE_THRESHOLD, False), (0.995, False), (float("nan"), True)],
)
def test_coverage_threshold_is_strictly_less_than_099(coverage, should_switch):
    decision = decide_switch(
        {"current": SourceTier.PRIMARY}, healthy_signal(coverage=coverage)
    )

    assert (decision["action"] == "switch_to_backup") is should_switch


def test_all_primary_checks_pass_without_switching():
    decision = decide_switch({"current": SourceTier.PRIMARY}, healthy_signal())

    assert decision == {
        "action": "stay",
        "reason": "all_checks_pass",
        "from_tier": "primary",
        "to_tier": "primary",
    }


def test_backup_returns_to_primary_after_two_healthy_days():
    signal = healthy_signal(
        primary_health_ok=True,
        primary_healthy_days=DEFAULT_PRIMARY_RECOVERY_DAYS,
    )

    decision = decide_switch({"current": SourceTier.BACKUP}, signal)

    assert decision["action"] == "switch_to_primary"
    assert decision["reason"] == "primary_recovered"
    assert decision["to_tier"] == "primary"


def test_backup_does_not_return_to_primary_after_one_healthy_day():
    signal = healthy_signal(primary_health_ok=True, primary_healthy_days=1)

    decision = decide_switch({"current": SourceTier.BACKUP}, signal)

    assert decision["action"] == "stay"
    assert decision["reason"] == "primary_recovery_wait"


def test_shadow_requires_human_approval_and_five_day_validation():
    signal = healthy_signal(
        shadow_approved=True,
        shadow_validation_passed=True,
        shadow_validation_days=DEFAULT_SHADOW_VALIDATION_DAYS,
    )

    decision = decide_switch({"current": SourceTier.SHADOW}, signal)

    assert decision["action"] == "switch_to_backup"
    assert decision["reason"] == "shadow_approved"


@pytest.mark.parametrize(
    "overrides",
    [
        {"shadow_approved": False, "shadow_validation_passed": True, "shadow_validation_days": 5},
        {"shadow_approved": True, "shadow_validation_passed": False, "shadow_validation_days": 5},
        {"shadow_approved": True, "shadow_validation_passed": True, "shadow_validation_days": 4},
    ],
)
def test_shadow_without_complete_approval_stays_shadow(overrides):
    decision = decide_switch({"current": SourceTier.SHADOW}, healthy_signal(**overrides))

    assert decision["action"] == "stay"
    assert decision["reason"] == "shadow_validation_pending"
    assert decision["to_tier"] == SourceTier.SHADOW.value


def test_apply_decision_changes_only_current_tier():
    state = {"current": SourceTier.PRIMARY, "execution_allowed": False}
    decision = decide_switch(state, {"connection_ok": False})

    updated = apply_decision(state, decision)

    assert updated == {"current": SourceTier.BACKUP, "execution_allowed": False}
    assert state["current"] is SourceTier.PRIMARY


def test_apply_stay_decision_keeps_current_tier():
    state = {"current": SourceTier.PRIMARY, "marker": "unchanged"}
    decision = decide_switch(state, healthy_signal())

    assert apply_decision(state, decision) == state


def test_apply_decision_rejects_stale_from_tier():
    decision = decide_switch({"current": SourceTier.PRIMARY}, {"connection_ok": False})

    with pytest.raises(ValueError, match="from_tier"):
        apply_decision({"current": SourceTier.BACKUP}, decision)


@pytest.mark.parametrize(
    ("action", "to_tier"),
    [("switch_to_backup", "primary"), ("switch_to_primary", "backup")],
)
def test_apply_decision_rejects_inconsistent_action_target(action, to_tier):
    decision = {
        "action": action,
        "reason": "test",
        "from_tier": "primary",
        "to_tier": to_tier,
    }

    with pytest.raises(ValueError, match="target"):
        apply_decision({"current": SourceTier.PRIMARY}, decision)


def test_unknown_current_tier_is_rejected():
    with pytest.raises(ValueError, match="current"):
        decide_switch({"current": "unknown"}, healthy_signal())


def test_coverage_threshold_must_be_between_zero_and_one():
    with pytest.raises(ValueError, match="coverage_threshold"):
        decide_switch(
            {"current": SourceTier.PRIMARY},
            healthy_signal(),
            coverage_threshold=1.1,
        )
