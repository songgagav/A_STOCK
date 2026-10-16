"""Frozen selector identity guards target reuse and target-driven execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import realtime_engine as re
import selector_weight_policy as policy


IDENTITY = {"mode": "frozen", "snapshot_sha256": "c" * 64}
MATCHING_SELECTION = {
    "top_n": [{"canon": "600000.SH", "price": 10.0}],
    "selector_weight_identity": IDENTITY,
}


def _target_environment(monkeypatch, tmp_path, *, drl_mode="shadow"):
    daily = tmp_path / "daily"
    daily.mkdir(parents=True)
    monkeypatch.setattr(re, "DAILY_DIR", str(daily))
    monkeypatch.setattr(re, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setenv("SELECTOR_WEIGHT_SNAPSHOT_SHA256", IDENTITY["snapshot_sha256"])
    monkeypatch.setenv("DRL_PLAN_MODE", drl_mode)
    monkeypatch.setattr(re, "selector_weight_mode", lambda: "frozen", raising=False)
    monkeypatch.setattr(
        re, "resolve_selector_weight_context",
        lambda: policy.SelectorWeightContext({"signal": 1.0}, IDENTITY),
        raising=False,
    )
    monkeypatch.setattr(re, "_trace_targets", lambda *_args, **_kwargs: None)
    return daily


def _write_selection(daily: Path, day: str, payload: dict):
    folder = daily / day
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "selection.json").write_text(json.dumps(payload), encoding="utf-8")


def test_matching_frozen_selection_identity_is_reusable(monkeypatch, tmp_path):
    daily = _target_environment(monkeypatch, tmp_path, drl_mode="enforce")
    _write_selection(daily, "20261016", MATCHING_SELECTION)
    monkeypatch.setattr(
        re, "_load_daily_plan_for_consume_day",
        lambda *_args: pytest.fail("unbound DRL plan must not substitute in frozen mode"),
    )

    targets, selection, source_day = re.load_targets("2026-10-16")

    assert [item["canon"] for item in targets] == ["600000.SH"]
    assert source_day == "20261016"
    assert selection["selector_weight_identity"] == IDENTITY
    assert selection["selector_weight_policy"]["status"] == "ready"


def test_invalid_frozen_identity_rejects_cached_selection(monkeypatch, tmp_path):
    daily = _target_environment(monkeypatch, tmp_path)
    _write_selection(daily, "20261016", MATCHING_SELECTION)
    monkeypatch.setattr(
        re, "resolve_selector_weight_context",
        lambda: (_ for _ in ()).throw(policy.SelectorWeightPolicyError("invalid snapshot")),
        raising=False,
    )
    monkeypatch.setattr(
        re, "RotationSelector",
        lambda *_args, **_kwargs: pytest.fail("invalid policy must not reach live selection"),
    )

    targets, selection, _source_day = re.load_targets("2026-10-16")

    assert targets == []
    assert selection["selector_weight_policy"]["status"] == "blocked"
    assert "invalid snapshot" in selection["selector_weight_policy"]["reason"]


@pytest.mark.parametrize("artifact_identity", [None, {"mode": "frozen", "snapshot_sha256": "d" * 64}])
def test_legacy_or_mismatched_selection_identity_is_skipped(
    monkeypatch, tmp_path, artifact_identity,
):
    daily = _target_environment(monkeypatch, tmp_path)
    stale = {"top_n": [{"canon": "600000.SH", "price": 10.0}]}
    if artifact_identity is not None:
        stale["selector_weight_identity"] = artifact_identity
    _write_selection(daily, "20261016", stale)
    observed = []

    class FakeSelector:
        def __init__(self, *_args, **_kwargs):
            pass

        def select(self):
            observed.append("fresh")
            return {**MATCHING_SELECTION, "top_n": [{"canon": "000001.SZ", "price": 12.0}]}

    class FakeDB:
        def close(self):
            pass

    monkeypatch.setattr(re, "StockDB", FakeDB)
    monkeypatch.setattr(re, "RotationSelector", FakeSelector)
    monkeypatch.setattr(re, "save_selection", lambda *_args: "unused")

    targets, selection, _source_day = re.load_targets("2026-10-16")

    assert observed == ["fresh"]
    assert targets[0]["canon"] == "000001.SZ"
    assert selection["source"] == "live_select"


def test_weight_gate_is_independent_of_signal_freeze_mode(monkeypatch):
    engine = object.__new__(re.RealtimeEngine)
    engine._selector_weight_initial_mode = "frozen"
    engine._selector_weight_expected_identity = IDENTITY
    engine._selector_weight_failure_latched = False
    engine.selector_weight_policy_status = {"mode": "frozen", "status": "ready", "identity": IDENTITY}
    engine.sel = {"selector_weight_identity": IDENTITY}
    monkeypatch.setattr(re, "selector_weight_mode", lambda: "frozen", raising=False)
    monkeypatch.setattr(
        re, "resolve_selector_weight_context",
        lambda: policy.SelectorWeightContext({"signal": 1.0}, IDENTITY), raising=False,
    )

    assert re.RealtimeEngine._snapshot_allows_rebalance("shadow", "missing") is True
    assert re.RealtimeEngine._selector_weight_target_rebalance_ready(engine) is True

    engine.selector_weight_policy_status = {"mode": "frozen", "status": "blocked", "reason": "bad identity"}
    engine._selector_weight_failure_latched = True
    assert re.RealtimeEngine._selector_weight_target_rebalance_ready(engine) is False

    engine.pb = SimpleNamespace(trade_date="2026-10-16")
    monkeypatch.setattr(
        re, "load_targets",
        lambda _day: ([{"canon": "600000.SH", "price": 10.0}], {
            "selector_weight_identity": IDENTITY,
            "selector_weight_policy": {"mode": "frozen", "status": "ready", "identity": IDENTITY},
        }, "20261016"),
    )
    preflight = re.RealtimeEngine.preflight_selector_weight_identity(engine)
    assert preflight["status"] == "ready"
    assert re.RealtimeEngine._selector_weight_target_rebalance_ready(engine) is True


def test_successful_preflight_refreshes_observation_epoch_receipt(monkeypatch):
    monkeypatch.setenv("OBSERVATION_EPOCH_ID", "e" * 64)
    engine = object.__new__(re.RealtimeEngine)
    engine.pb = SimpleNamespace(trade_date="2026-10-16")
    engine._selector_weight_initial_mode = "frozen"
    engine._selector_weight_expected_identity = IDENTITY
    engine._selector_weight_failure_latched = True
    engine.selector_weight_policy_status = {"mode": "frozen", "status": "blocked"}
    engine.sel = {"selector_weight_identity": {"mode": "frozen", "snapshot_sha256": "d" * 64}}
    engine.observation_epoch_receipt = {"status": "blocked", "epoch_id": "e" * 64}
    refreshed = {
        "status": "ready", "epoch_id": "e" * 64,
        "experiment_identity": {"selector_weight_mode": "frozen"},
        "observation_epoch": {"epoch_id": "e" * 64},
    }
    monkeypatch.setattr(re, "selector_weight_mode", lambda: "frozen", raising=False)
    monkeypatch.setattr(
        re, "resolve_selector_weight_context",
        lambda: policy.SelectorWeightContext({"signal": 1.0}, IDENTITY), raising=False,
    )
    monkeypatch.setattr(
        re, "load_targets",
        lambda _day: ([{"canon": "600000.SH"}], {
            "selector_weight_identity": IDENTITY,
            "selector_weight_policy": {"status": "ready"},
        }, "20261016"),
    )
    receipt_calls = []

    def _receipt(_root, *, selector_weight_identity=None):
        receipt_calls.append(selector_weight_identity)
        return refreshed

    monkeypatch.setattr(re, "observation_epoch_runtime_receipt", _receipt)

    result = re.RealtimeEngine.preflight_selector_weight_identity(engine)

    assert result["status"] == "ready"
    assert receipt_calls == [IDENTITY]
    assert engine.observation_epoch_receipt == refreshed


def test_runtime_epoch_receipt_rechecks_snapshot_after_startup(monkeypatch):
    epoch_id = "e" * 64
    monkeypatch.setenv("OBSERVATION_EPOCH_ID", epoch_id)
    monkeypatch.setattr(re, "selector_weight_mode", lambda: "frozen", raising=False)
    monkeypatch.setattr(
        re, "resolve_selector_weight_context",
        lambda: (_ for _ in ()).throw(policy.SelectorWeightPolicyError("snapshot disappeared")),
        raising=False,
    )
    engine = object.__new__(re.RealtimeEngine)
    engine._selector_weight_initial_mode = "frozen"
    engine._selector_weight_expected_identity = IDENTITY
    engine._selector_weight_failure_latched = False
    engine.selector_weight_policy_status = {"mode": "frozen", "status": "ready"}
    engine.sel = {"selector_weight_identity": IDENTITY}
    engine.observation_epoch_receipt = {"status": "ready", "epoch_id": epoch_id}

    receipt = re.RealtimeEngine._current_observation_epoch_receipt(engine)

    assert receipt["status"] == "blocked"
    assert receipt["epoch_id"] == epoch_id
    assert "snapshot disappeared" in receipt["reason"]


def test_invalid_frozen_identity_blocks_new_rebalance_and_preserves_book(monkeypatch):
    logs = []
    monkeypatch.setattr(re, "log", logs.append)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setattr(re, "selector_weight_mode", lambda: "frozen", raising=False)
    monkeypatch.setattr(
        re, "resolve_selector_weight_context",
        lambda: (_ for _ in ()).throw(policy.SelectorWeightPolicyError("invalid snapshot")),
        raising=False,
    )
    risk_check = Mock(return_value={
        "stopped": [], "trimmed": [], "state": "NORMAL", "drawdown_pct": 0.0,
    })
    emergency_check = Mock(return_value={"blocked": False, "layers": [], "reasons": []})
    position = {"qty": 100, "avg_cost": 10.0, "buy_date": "2026-10-15", "locked_qty": 0}

    class FakeBook:
        positions = {"600000.SH": dict(position)}
        cash = 5000.0
        state = "NORMAL"
        trade_date = "2026-10-16"

        def market_value(self):
            return 1000.0

        def market_price(self, _canon):
            return 10.0

        def apply_risk_controls(self):
            return risk_check()

        def sell(self, *_args, **_kwargs):
            pytest.fail("invalid identity must not trigger target-driven exit")

        def buy(self, *_args, **_kwargs):
            pytest.fail("invalid identity must not trigger target-driven buy")

    engine = object.__new__(re.RealtimeEngine)
    engine.pb = FakeBook()
    engine.targets = [{"canon": "000001.SZ", "target_weight": 1.0}]
    engine._broker_logged = True
    engine._to_day = engine.pb.trade_date
    engine._to_used = 0.0
    engine._cooled = set()
    engine._last_rebal_day = None
    engine.feed = SimpleNamespace(last_error="", quotes={})
    engine._ks_last_sig = None
    engine._compute_gate = lambda _equity: {
        "exposure_mult": 1.0, "interval_days": 0, "freeze_new_buys": False,
    }
    engine._tradable = lambda _canon: (True, "ok")
    engine.selector_weight_policy_status = {"mode": "frozen", "status": "ready", "identity": IDENTITY}
    engine._selector_weight_initial_mode = "frozen"
    engine._selector_weight_expected_identity = IDENTITY
    engine._selector_weight_failure_latched = False
    engine.sel = {"selector_weight_identity": IDENTITY}
    monkeypatch.setitem(
        __import__("sys").modules, "live_gates",
        SimpleNamespace(apply_to_engine=lambda *_args, **_kwargs: {}),
    )
    monkeypatch.setitem(
        __import__("sys").modules, "kill_switch",
        SimpleNamespace(verdict=lambda **_kwargs: emergency_check()),
    )

    before_qty = {symbol: position["qty"] for symbol, position in engine.pb.positions.items()}
    before_cash = engine.pb.cash
    ready = re.RealtimeEngine._selector_weight_target_rebalance_ready(engine)
    re.RealtimeEngine._rebalance(engine, {"600000.SH": 10.0}, allow_target_rebalance=ready)

    assert ready is False
    assert {symbol: position["qty"] for symbol, position in engine.pb.positions.items()} == before_qty
    assert engine.pb.cash == before_cash
    risk_check.assert_called_once()
    emergency_check.assert_called_once()
    assert any("selector-weight" in line.lower() or "权重" in line for line in logs)


def test_frozen_policy_failure_is_visible_in_health_and_logs(monkeypatch):
    logs = []
    monkeypatch.setattr(re, "log", logs.append)
    engine = object.__new__(re.RealtimeEngine)
    engine._selector_weight_initial_mode = "frozen"
    engine._selector_weight_expected_identity = IDENTITY
    engine._selector_weight_failure_latched = False
    engine.selector_weight_policy_status = {"mode": "frozen", "status": "ready", "identity": IDENTITY}
    engine.sel = {}
    monkeypatch.setattr(re, "selector_weight_mode", lambda: "frozen", raising=False)
    monkeypatch.setattr(
        re, "resolve_selector_weight_context",
        lambda: policy.SelectorWeightContext({"signal": 1.0}, IDENTITY), raising=False,
    )

    ready = re.RealtimeEngine._selector_weight_target_rebalance_ready(engine)

    assert ready is False
    assert engine.selector_weight_policy_status["status"] == "blocked"
    assert engine._selector_weight_policy_health_receipt()["reason"]
    assert len(logs) == 1
