# -*- coding: utf-8 -*-
"""Observation mode must collect runtime evidence without mutating PaperBook."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import realtime_engine as RE  # noqa: E402


def _engine(mode: str) -> RE.RealtimeEngine:
    engine = object.__new__(RE.RealtimeEngine)
    engine.execution_mode = mode
    engine.pb = SimpleNamespace(trade_date="2026-10-30")
    engine._ca_applied_day = None
    engine.snapshot_status = "ready"
    engine._apply_corporate_actions = Mock()
    engine._snapshot_allows_rebalance = Mock(return_value=True)
    engine._rebalance = Mock()
    return engine


def test_execution_mode_defaults_to_observe(monkeypatch):
    monkeypatch.delenv("PAPER_EXECUTION_MODE", raising=False)

    assert RE._resolve_execution_mode() == "observe"


@pytest.mark.parametrize("value", ["", "disabled", "shadow", "OBSERVE_ONLY"])
def test_unknown_execution_mode_fails_closed(value):
    with pytest.raises(ValueError, match="PAPER_EXECUTION_MODE"):
        RE._resolve_execution_mode(value)


def test_observe_mode_skips_corporate_actions_and_all_rebalance_paths():
    engine = _engine("observe")

    engine._run_paper_execution_phase(
        {"600000.SH": 10.0},
        session=True,
        selector_weight_ready=False,
        mode="enforce",
    )

    engine._apply_corporate_actions.assert_not_called()
    engine._rebalance.assert_not_called()


def test_paper_mode_keeps_existing_execution_path():
    engine = _engine("paper")
    engine._ca_applied_day = engine.pb.trade_date
    latest = {"600000.SH": 10.0}

    engine._run_paper_execution_phase(
        latest,
        session=True,
        selector_weight_ready=False,
        mode="enforce",
    )

    engine._apply_corporate_actions.assert_not_called()
    engine._rebalance.assert_called_once_with(latest, allow_target_rebalance=False)


def test_observe_mode_never_persists_paper_state(monkeypatch):
    writes = []
    monkeypatch.setattr(RE, "_atomic_write_json", lambda path, payload: writes.append((path, payload)))
    engine = _engine("observe")

    engine._persist_paper_state({"cash": 100000.0})

    assert writes == []


def test_paper_mode_persists_paper_state(monkeypatch):
    writes = []
    monkeypatch.setattr(RE, "_atomic_write_json", lambda path, payload: writes.append((path, payload)))
    engine = _engine("paper")
    payload = {"cash": 100000.0}

    engine._persist_paper_state(payload)

    assert writes == [(RE.STATE_FILE, payload)]
