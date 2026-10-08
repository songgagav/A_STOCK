# -*- coding: utf-8 -*-
"""Contracts for explicit daily holdings and fill-ledger evidence replay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from daily_execution_evidence import (
    DailyExecutionRequest,
    build_daily_execution_evidence,
)
from evidence_bundle import ArtifactInput


def _artifact(tmp_path: Path, name: str, payload) -> ArtifactInput:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return ArtifactInput(
        name=name,
        source_path=path,
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _request(tmp_path: Path, *, fills=None, trades=None) -> DailyExecutionRequest:
    paper_before = {
        "date": "2026-10-07",
        "equity": 100000.0,
        "cash": 20000.0,
        "positions": {
            "600000.SH": {"qty": 8000, "last_price": 10.0},
        },
    }
    paper_after = {
        "date": "2026-10-08",
        "equity": 100000.0,
        "cash": 60000.0,
        "positions": {
            "600000.SH": {"qty": 4000, "last_price": 10.0},
        },
    }
    target_plan = {
        "consume_day": "2026-10-08",
        "top_n": [{"canon": "600000.SH", "target_weight": 0.4}],
    }
    orders = [{"order_id": "o-1", "symbol": "600000.SH", "side": "sell", "qty": 4000, "order_price": 10.0}]
    fill_rows = fills if fills is not None else []
    artifact_values = {
        "positions_before": paper_before,
        "positions_after": paper_after,
        "target_positions": target_plan,
        "orders": orders,
        "fills": fill_rows if trades is None else trades,
        "snapshot": {"trade_day": "2026-10-08"},
    }
    artifacts = {name: _artifact(tmp_path, name, value) for name, value in artifact_values.items()}
    return DailyExecutionRequest(
        trade_day="2026-10-08",
        reference_equity=100000.0,
        reference_timestamp="2026-10-08T09:25:00+08:00",
        cost_evidence_level="simulated",
        artifacts=artifacts,
        source_identity={"data_sha": "data-sha-1"},
    )


def test_explicit_paperbook_and_target_plan_include_cash_in_daily_turnover(tmp_path):
    result = build_daily_execution_evidence(_request(tmp_path))

    metric = result["turnover"]["metrics"]["target_weight_turnover"]
    assert metric["status"] == "available"
    assert metric["value"] == pytest.approx(0.4)
    assert result["normalized_weights"]["target_positions"]["CASH"] == pytest.approx(0.6)
    assert result["turnover"]["metrics"]["executed_turnover"]["status"] == "not_applicable"
    assert result["turnover"]["metrics"]["executed_turnover"]["value"] is None
    assert result["source_artifacts"]["positions_before"]["sha256"]


def test_legacy_trade_rows_are_blocked_without_canonical_fill_fields(tmp_path):
    legacy_trade = [{
        "type": "buy",
        "canon": "600000.SH",
        "qty": 100,
        "price": 10.0,
        "fee": 0.25,
        "date": "2026-10-08",
        "time": "09:30:00",
    }]
    result = build_daily_execution_evidence(_request(tmp_path, trades=legacy_trade))

    assert result["cost_replay"]["status"] == "blocked"
    assert result["cost_replay"]["summary"]["total_cost"] is None
    assert "canonical_fill_fields_missing" in result["cost_replay"]["errors"][0]
    assert result["raw_evidence"]["fills"][0]["canon"] == "600000.SH"


def test_incomplete_current_day_is_explicitly_blocked_not_inferred(tmp_path):
    request = _request(tmp_path)
    request = DailyExecutionRequest(
        trade_day="2026-10-09",
        reference_equity=request.reference_equity,
        reference_timestamp=request.reference_timestamp,
        cost_evidence_level=request.cost_evidence_level,
        artifacts=request.artifacts,
        source_identity=request.source_identity,
    )
    result = build_daily_execution_evidence(request)

    assert result["status"] == "blocked"
    assert "artifact_trade_day_mismatch" in result["errors"]
