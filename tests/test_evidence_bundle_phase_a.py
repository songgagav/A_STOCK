# -*- coding: utf-8 -*-
"""Phase A contracts for offline Evidence Bundle construction.

These tests intentionally exercise only the new side-channel evidence layer.
They must not import selector, PaperBook, realtime, broker, or DRL modules.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evidence_bundle import (
    ArtifactInput,
    BundleBuildError,
    EvidenceBundleRequest,
    build_bundle,
    verify_bundle,
)
from evidence_cost import replay_costs
from evidence_turnover import compute_turnover


def _write_json(path: Path, value) -> str:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(path: Path, name: str, value) -> ArtifactInput:
    digest = _write_json(path, value)
    return ArtifactInput(
        name=name,
        source_path=path,
        expected_sha256=digest,
        format="json",
    )


def _fill(*, side="buy", fill_price=10.01, stamp_tax=0.0, embedded=True):
    return {
        "order_id": "o-1",
        "fill_id": "f-1",
        "symbol": "600000",
        "side": side,
        "decision_ts": "2026-10-08T09:25:00+08:00",
        "decision_price": 10.0,
        "order_ts": "2026-10-08T09:26:00+08:00",
        "order_price": 10.0,
        "fill_ts": "2026-10-08T09:27:00+08:00",
        "fill_price": fill_price,
        "qty": 100,
        "commission": 0.25,
        "stamp_tax": stamp_tax,
        "transfer_fee": 0.01,
        "slippage": 0.50,
        "market_impact": 0.50,
        "source_artifact": "fills.json",
        "realized_fill": False,
        "component_provenance": {
            "commission": {
                "embedded_in_fill_price": False,
                "source": "fill-ledger",
            },
            "stamp_tax": {
                "embedded_in_fill_price": False,
                "source": "fill-ledger",
            },
            "transfer_fee": {
                "embedded_in_fill_price": False,
                "source": "fill-ledger",
            },
            "slippage": {
                "embedded_in_fill_price": embedded,
                "source": "paper-fill-price" if embedded else "fill-ledger",
            },
            "market_impact": {
                "embedded_in_fill_price": embedded,
                "source": "paper-fill-price" if embedded else "fill-ledger",
            },
        },
        "opportunity_benchmark": {
            "type": "arrival_price",
            "price": 10.0,
            "timestamp": "2026-10-08T09:26:00+08:00",
        },
    }


def _request(tmp_path: Path, *, run_id="run-1", fills=None, statuses=None):
    raw = tmp_path / "input"
    raw.mkdir(exist_ok=True)
    artifacts = {
        "market_data": _artifact(raw / "market.json", "market_data", {"day": "2026-10-08"}),
        "positions_before": _artifact(
            raw / "before.json",
            "positions_before",
            {"600000": 0.40, "CASH": 0.60},
        ),
        "positions_after": _artifact(
            raw / "after.json",
            "positions_after",
            {"600000": 0.35, "600001": 0.25, "CASH": 0.40},
        ),
        "target_positions": _artifact(
            raw / "target.json",
            "target_positions",
            {"600000": 0.35, "600001": 0.25, "CASH": 0.40},
        ),
        "orders": _artifact(
            raw / "orders.json",
            "orders",
            [{"order_id": "o-1", "symbol": "600001", "side": "buy", "qty": 100, "order_price": 10.0}],
        ),
        "fills": _artifact(raw / "fills.json", "fills", fills if fills is not None else [_fill()]),
    }
    snapshot = _artifact(raw / "snapshot.json", "snapshot", {"targets": ["600000", "600001"]})
    return EvidenceBundleRequest(
        output_root=tmp_path / "bundles",
        trade_day="2026-10-08",
        generated_at="2026-10-08T16:00:00+08:00",
        run_id=run_id,
        code_sha="code-sha-1",
        data_identity={"data_sha": "data-sha-1", "source": "fixture"},
        config_identity={"config_sha": "config-sha-1", "version": "paper-v1"},
        snapshot=snapshot,
        artifacts=artifacts,
        experiment_identity={"experiment_hash": "experiment-sha-1", "name": "fixture"},
        production_state={
            "RANK_BY_FUSION": "0",
            "alpha_evidence_status": "not_promotable",
            "drl_plan_mode_contract": "not_implemented",
        },
        reference_equity=100_000.0,
        reference_timestamp="2026-10-08T09:25:00+08:00",
        cost_evidence_level="simulated",
        observation_statuses=statuses or {},
    )


def test_bundle_is_deterministic_and_contains_raw_and_derived_evidence(tmp_path):
    request = _request(tmp_path)

    first = build_bundle(request)
    second = build_bundle(request)

    assert first.bundle_id == second.bundle_id
    assert first.path == second.path
    assert first.manifest["schema_version"] == 1
    assert first.manifest["bundle_id"] == first.bundle_id
    assert first.manifest["evidence_status"] == "available"
    assert first.manifest["snapshot_hash"] == request.snapshot.expected_sha256
    assert first.manifest["production_state"]["RANK_BY_FUSION"] == "0"
    assert first.manifest["production_state"]["alpha_evidence_status"] == "not_promotable"
    assert first.manifest["production_state"]["drl_plan_mode_contract"] == "not_implemented"
    assert (first.path / "manifest.json").is_file()
    assert (first.path / "raw" / "snapshot.json").is_file()
    assert (first.path / "derived" / "turnover.json").is_file()
    assert (first.path / "derived" / "cost_records.jsonl").is_file()
    assert verify_bundle(first.path)["bundle_id"] == first.bundle_id


def test_manifest_constituent_tamper_is_detected_and_finalized_bundle_is_not_overwritten(tmp_path):
    request = _request(tmp_path)
    result = build_bundle(request)
    raw_file = result.path / "raw" / "fills.json"
    raw_file.write_text("[]\n", encoding="utf-8")

    with pytest.raises(BundleBuildError) as excinfo:
        verify_bundle(result.path)
    assert excinfo.value.status == "tampered"

    with pytest.raises(BundleBuildError) as excinfo:
        build_bundle(request)
    assert excinfo.value.status == "tampered"


def test_manifest_commit_marker_tamper_is_detected(tmp_path):
    result = build_bundle(_request(tmp_path))
    manifest_path = result.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence_status"] = "invalid"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(BundleBuildError) as excinfo:
        verify_bundle(result.path)
    assert excinfo.value.status == "tampered"


def test_changed_constituent_gets_a_new_bundle_id_and_does_not_replace_old_bundle(tmp_path):
    request = _request(tmp_path, run_id="run-1")
    first = build_bundle(request)

    fills_path = request.artifacts["fills"].source_path
    new_fills = [_fill(fill_price=10.02)]
    new_hash = _write_json(fills_path, new_fills)
    changed_artifacts = dict(request.artifacts)
    changed_artifacts["fills"] = ArtifactInput(
        name="fills",
        source_path=fills_path,
        expected_sha256=new_hash,
        format="json",
    )
    changed = request.__class__(**{**request.__dict__, "artifacts": changed_artifacts})
    second = build_bundle(changed)

    assert second.bundle_id != first.bundle_id
    assert first.path.exists()
    assert second.path.exists()


def test_pending_maturity_is_not_failed(tmp_path):
    result = build_bundle(
        _request(tmp_path, statuses={"forward_return_120d": "pending_maturity"})
    )

    assert result.manifest["evidence_status"] == "pending_maturity"
    assert result.manifest["field_statuses"]["forward_return_120d"] == "pending_maturity"
    assert result.manifest["blocked_reasons"] == []


def test_cash_is_part_of_target_weight_turnover_and_denominator_is_fixed():
    result = compute_turnover(
        previous_actual_weights={"600000": 0.80, "CASH": 0.20},
        target_weights={"600000": 0.40, "CASH": 0.60},
        planned_orders=[],
        fills=[],
        reference_equity=100_000.0,
        reference_timestamp="2026-10-08T09:25:00+08:00",
    )

    metric = result["metrics"]["target_weight_turnover"]
    assert metric["status"] == "available"
    assert metric["value"] == pytest.approx(0.40)
    assert metric["denominator_type"] == "reference_equity_weight_l1"
    assert metric["reference_equity"] == 100_000.0
    assert metric["reference_timestamp"] == "2026-10-08T09:25:00+08:00"


def test_missing_cash_is_invalid_instead_of_being_filled_with_zero():
    result = compute_turnover(
        previous_actual_weights={"600000": 1.0},
        target_weights={"600000": 0.5, "CASH": 0.5},
        planned_orders=[],
        fills=[],
        reference_equity=100_000.0,
        reference_timestamp="2026-10-08T09:25:00+08:00",
    )

    assert result["metrics"]["target_weight_turnover"]["status"] == "invalid"
    assert result["metrics"]["target_weight_turnover"]["value"] is None


def test_executed_turnover_is_not_zero_without_fills():
    result = compute_turnover(
        previous_actual_weights={"600000": 0.5, "CASH": 0.5},
        target_weights={"600000": 0.5, "CASH": 0.5},
        planned_orders=[],
        fills=[],
        reference_equity=100_000.0,
        reference_timestamp="2026-10-08T09:25:00+08:00",
    )

    metric = result["metrics"]["executed_turnover"]
    assert metric["status"] == "not_applicable"
    assert metric["value"] is None


def test_turnover_uses_fixed_reference_equity_for_planned_and_executed():
    result = compute_turnover(
        previous_actual_weights={"600000": 0.5, "CASH": 0.5},
        target_weights={"600000": 0.5, "CASH": 0.5},
        planned_orders=[{"qty": 100, "order_price": 10.0}],
        fills=[{"qty": 50, "fill_price": 10.0}],
        reference_equity=10_000.0,
        reference_timestamp="2026-10-08T09:25:00+08:00",
    )

    assert result["metrics"]["planned_turnover"]["value"] == pytest.approx(0.10)
    assert result["metrics"]["executed_turnover"]["value"] == pytest.approx(0.05)
    assert result["metrics"]["planned_turnover"]["reference_equity"] == 10_000.0
    assert result["metrics"]["executed_turnover"]["denominator_type"] == "reference_equity_notional"


def test_realized_cost_is_blocked_without_explicit_real_fill():
    result = replay_costs([_fill()], evidence_level="realized")

    assert result["status"] == "blocked"
    assert result["summary"]["total_cost"] is None
    assert result["records"] == []


def test_sell_stamp_tax_is_counted_but_buy_stamp_tax_is_zero():
    rows = [
        _fill(side="buy", stamp_tax=0.0, embedded=False),
        {
            **_fill(side="sell", fill_price=9.99, stamp_tax=0.50, embedded=False),
            "order_id": "o-2",
            "fill_id": "f-2",
        },
    ]
    result = replay_costs(rows, evidence_level="simulated")

    assert result["status"] == "available"
    assert result["summary"]["components"]["stamp_tax"] == pytest.approx(0.50)
    assert result["records"][0]["stamp_tax"] == 0.0
    assert result["records"][1]["stamp_tax"] == 0.50


def test_embedded_slippage_and_impact_are_not_double_counted():
    result = replay_costs([_fill(embedded=True)], evidence_level="simulated")
    record = result["records"][0]

    # Buy-side price moved 0.01 * 100 = 1.00; embedded slippage/impact are
    # represented by that single execution-price delta, not added again.
    assert record["execution_price_cost"] == pytest.approx(1.0)
    assert record["total_cost"] == pytest.approx(1.0 + 0.25 + 0.01)
    assert record["component_provenance"]["slippage"]["embedded_in_fill_price"] is True
    assert record["component_provenance"]["market_impact"]["embedded_in_fill_price"] is True


def test_opportunity_cost_is_null_without_a_defined_benchmark():
    row = _fill()
    row.pop("opportunity_benchmark")
    result = replay_costs([row], evidence_level="simulated")
    record = result["records"][0]

    assert record["opportunity_cost"] is None
    assert record["opportunity_cost_reason"] == "benchmark_undefined"


def test_missing_cost_component_is_invalid_not_zero():
    row = _fill()
    row.pop("commission")
    result = replay_costs([row], evidence_level="simulated")

    assert result["status"] == "invalid"
    assert result["records"] == []
    assert any("commission" in error for error in result["errors"])


def test_builder_has_no_production_side_effects(tmp_path):
    result = build_bundle(_request(tmp_path))

    assert not (tmp_path / "data" / "target_plan.json").exists()
    assert not (tmp_path / "data" / "state.json").exists()
    assert result.path.parent == tmp_path / "bundles"
