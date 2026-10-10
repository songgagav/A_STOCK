# -*- coding: utf-8 -*-
"""Red-green contracts for the explicit continuous OOS dataset builder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evidence_bundle import ArtifactInput, EvidenceBundleRequest, build_bundle, verify_bundle
from oos_dataset import (
    OOSDatasetRequest,
    OOSDayInput,
    OOSDatasetBuildError,
    build_oos_dataset,
    verify_oos_dataset,
)


def _json_artifact(path: Path, name: str, value) -> ArtifactInput:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return ArtifactInput(
        name=name,
        source_path=path,
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _fill() -> dict:
    return {
        "order_id": "o-1",
        "fill_id": "f-1",
        "symbol": "600000.SH",
        "side": "buy",
        "decision_ts": "2026-10-07T09:25:00+08:00",
        "decision_price": 10.0,
        "order_ts": "2026-10-07T09:26:00+08:00",
        "order_price": 10.0,
        "fill_ts": "2026-10-07T09:27:00+08:00",
        "fill_price": 10.01,
        "qty": 100,
        "commission": 0.25,
        "stamp_tax": 0.0,
        "transfer_fee": 0.01,
        "slippage": 0.50,
        "market_impact": 0.50,
        "source_artifact": "fills.json",
        "realized_fill": False,
        "component_provenance": {
            component: {
                "embedded_in_fill_price": component in {"slippage", "market_impact"},
                "source": "paper-fill-price" if component in {"slippage", "market_impact"} else "fill-ledger",
            }
            for component in ("commission", "stamp_tax", "transfer_fee", "slippage", "market_impact")
        },
        "opportunity_benchmark": {
            "type": "arrival_price",
            "price": 10.0,
            "timestamp": "2026-10-07T09:26:00+08:00",
        },
    }


def _lineage():
    return {
        "source": "h5i", "schema": "v1", "routing": "h5i-primary",
        "universe": "a-share-v1", "calendar_source": "official-fixture",
        "calendar_version": "2026-v1", "lineage_sha": "lineage-sha-1",
    }


def _bundle(tmp_path: Path, trade_day: str) -> Path:
    raw = tmp_path / f"raw-{trade_day}"
    raw.mkdir()
    artifacts = {
        "market_data": _json_artifact(raw / "market.json", "market_data", {"trade_day": trade_day}),
        "positions_before": _json_artifact(raw / "before.json", "positions_before", {"600000.SH": 0.4, "CASH": 0.6}),
        "positions_after": _json_artifact(raw / "after.json", "positions_after", {"600000.SH": 0.4, "CASH": 0.6}),
        "target_positions": _json_artifact(raw / "target.json", "target_positions", {"600000.SH": 0.4, "CASH": 0.6}),
        "orders": _json_artifact(raw / "orders.json", "orders", [{"order_id": "o-1", "qty": 100, "order_price": 10.0}]),
        "fills": _json_artifact(raw / "fills.json", "fills", [_fill()]),
    }
    snapshot = _json_artifact(raw / "snapshot.json", "snapshot", {"trade_day": trade_day})
    result = build_bundle(
        EvidenceBundleRequest(
            output_root=tmp_path / "bundles",
            trade_day=trade_day,
            generated_at=f"{trade_day}T16:00:00+08:00",
            run_id=f"run-{trade_day}",
            code_sha="code-sha-1",
            data_identity={"data_sha": "daily-data-" + trade_day},
            data_lineage_identity=_lineage(),
            config_identity={"config_sha": "config-sha-1"},
            snapshot=snapshot,
            artifacts=artifacts,
            experiment_identity={"experiment_hash": "experiment-sha-1"},
            production_state={
                "RANK_BY_FUSION": "0",
                "DRL_PLAN_MODE": "shadow",
                "FUSION_WEIGHT_MODE": "shadow",
                "TRADE_BROKER": "paper",
                "alpha_evidence_status": "not_promotable",
                "drl_plan_mode_contract": "implemented_default_shadow",
            },
            reference_equity=100_000.0,
            reference_timestamp=f"{trade_day}T09:25:00+08:00",
            cost_evidence_level="simulated",
        )
    )
    return result.path


def _request(
    tmp_path: Path,
    days: tuple[OOSDayInput, ...],
    trade_days: tuple[str, ...] | None = None,
    production_state=None,
    code_sha="code-sha-1",
):
    return OOSDatasetRequest(
        output_root=tmp_path / "oos",
        run_id="oos-run-1",
        generated_at="2026-10-08T20:00:00+08:00",
        code_sha=code_sha,
        data_identity={"data_sha": "data-sha-1"},
        data_lineage_identity=_lineage(),
        config_identity={"config_sha": "config-sha-1"},
        experiment_identity={"experiment_hash": "experiment-sha-1"},
        calendar_identity={"calendar_sha": "calendar-sha-1", "trade_days_are_explicit": True},
        production_state=production_state or {
            "RANK_BY_FUSION": "0",
            "DRL_PLAN_MODE": "shadow",
            "FUSION_WEIGHT_MODE": "shadow",
            "TRADE_BROKER": "paper",
            "alpha_evidence_status": "not_promotable",
            "drl_plan_mode_contract": "implemented_default_shadow",
        },
        trade_days=trade_days if trade_days is not None else tuple(day.trade_day for day in days),
        days=days,
    )


def _day(path: Path, trade_day: str) -> OOSDayInput:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    return OOSDayInput(
        trade_day=trade_day,
        bundle_path=path,
        expected_manifest_sha256=hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest(),
        bundle_id=manifest["bundle_id"],
        status="available",
    )


def test_same_explicit_days_are_deterministic_and_include_provenance(tmp_path):
    first_bundle = _bundle(tmp_path, "2026-10-07")
    second_bundle = _bundle(tmp_path, "2026-10-08")
    first_daily_manifest = verify_bundle(first_bundle)
    second_daily_manifest = verify_bundle(second_bundle)
    assert first_daily_manifest["data_sha"] != second_daily_manifest["data_sha"]
    assert first_daily_manifest["snapshot_hash"] != second_daily_manifest["snapshot_hash"]
    assert first_daily_manifest["bundle_identity"]["source_artifact_hashes"]["market_data"] != second_daily_manifest["bundle_identity"]["source_artifact_hashes"]["market_data"]
    assert first_daily_manifest["observation_epoch"] == second_daily_manifest["observation_epoch"]
    days = (_day(first_bundle, "2026-10-07"), _day(second_bundle, "2026-10-08"))

    first = build_oos_dataset(_request(tmp_path, days))
    second = build_oos_dataset(_request(tmp_path, days))

    assert first.dataset_id == second.dataset_id
    assert first.path == second.path
    assert first.manifest["trade_days"] == ["2026-10-07", "2026-10-08"]
    assert first.manifest["daily_available"] == 2
    assert first.manifest["observation_epoch"]["epoch_id"] == first.manifest["dataset_identity"]["observation_epoch"]["epoch_id"]
    assert first.manifest["observation_epoch"]["identity"]["code_sha"] == "code-sha-1"
    bundle_manifest = json.loads((first_bundle / "manifest.json").read_text(encoding="utf-8"))
    assert first.manifest["observation_epoch"] == bundle_manifest["observation_epoch"]
    assert first.manifest["constituent_artifact_hashes"]
    assert (first.path / "raw" / "day_index.jsonl").is_file()
    assert (first.path / "derived" / "daily_metrics.jsonl").is_file()
    assert verify_oos_dataset(first.path)["dataset_id"] == first.dataset_id


def test_oos_requires_explicit_shadow_runtime_state(tmp_path):
    day = OOSDayInput(
        trade_day="2026-10-07",
        bundle_path=None,
        expected_manifest_sha256=None,
        bundle_id=None,
        status="pending_maturity",
        reason="fixture",
    )
    with pytest.raises(ValueError, match="DRL_PLAN_MODE"):
        _request(tmp_path, (day,), production_state={
            "RANK_BY_FUSION": "0",
            "alpha_evidence_status": "not_promotable",
            "drl_plan_mode_contract": "not_implemented",
        })


def test_oos_rejects_bundle_from_a_different_observation_epoch(tmp_path):
    bundle = _bundle(tmp_path, "2026-10-07")
    day = _day(bundle, "2026-10-07")

    with pytest.raises(OOSDatasetBuildError, match="observation_epoch_mismatch"):
        build_oos_dataset(_request(tmp_path, (day,), code_sha="code-sha-2"))


def test_missing_day_is_preserved_and_not_converted_to_zero(tmp_path):
    bundle = _bundle(tmp_path, "2026-10-07")
    days = (
        _day(bundle, "2026-10-07"),
        OOSDayInput(
            trade_day="2026-10-08",
            bundle_path=None,
            expected_manifest_sha256=None,
            bundle_id=None,
            status="pending_maturity",
            reason="forward evidence is not mature",
        ),
    )

    result = build_oos_dataset(_request(tmp_path, days))

    assert result.manifest["evidence_status"] == "pending_maturity"
    rows = [json.loads(line) for line in (result.path / "derived" / "daily_metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[1]["status"] == "pending_maturity"
    assert rows[1]["turnover"] is None
    assert rows[1]["cost_summary"] is None


def test_available_day_requires_explicit_bundle_hash_and_does_not_find_latest(tmp_path):
    bundle = _bundle(tmp_path, "2026-10-07")
    bundle_id = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["bundle_id"]
    day = OOSDayInput(
        trade_day="2026-10-07",
        bundle_path=bundle,
        expected_manifest_sha256="0" * 64,
        bundle_id=bundle_id,
        status="available",
    )

    with pytest.raises(OOSDatasetBuildError) as excinfo:
        build_oos_dataset(_request(tmp_path, (day,)))

    assert excinfo.value.status == "tampered"
    assert "manifest_hash_mismatch" in excinfo.value.reasons[0]


def test_finalized_dataset_cannot_be_overwritten_after_constituent_tamper(tmp_path):
    bundle = _bundle(tmp_path, "2026-10-07")
    result = build_oos_dataset(_request(tmp_path, (_day(bundle, "2026-10-07"),)))
    metrics = result.path / "derived" / "daily_metrics.jsonl"
    metrics.write_text(metrics.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")

    with pytest.raises(OOSDatasetBuildError) as excinfo:
        build_oos_dataset(_request(tmp_path, (_day(bundle, "2026-10-07"),)))

    assert excinfo.value.status == "tampered"


def test_trade_days_must_be_explicit_sorted_and_complete(tmp_path):
    bundle = _bundle(tmp_path, "2026-10-07")
    day = _day(bundle, "2026-10-07")

    with pytest.raises(ValueError, match="trade_days must be sorted"):
        _request(tmp_path, (day,), trade_days=("2026-10-08", "2026-10-07"))

    with pytest.raises(ValueError, match="one day input per trade_day"):
        _request(tmp_path, (day,), trade_days=("2026-10-07", "2026-10-08"))
