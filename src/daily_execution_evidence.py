# -*- coding: utf-8 -*-
"""Pure adapter for explicit daily holdings and transaction evidence.

The project has several historical artifact shapes.  This adapter accepts only
caller-provided artifact paths and hashes, normalizes PaperBook/target-plan
weights into the Phase A contracts, and preserves incomplete legacy trade rows
as blocked evidence.  It never updates the source artifacts or any production
ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evidence_bundle import ArtifactInput
from evidence_cost import EVIDENCE_LEVELS, replay_costs
from evidence_turnover import compute_turnover


_REQUIRED_ARTIFACTS = {"positions_before", "positions_after", "target_positions", "orders", "fills", "snapshot"}
_CANONICAL_FILL_FIELDS = {
    "order_id",
    "fill_id",
    "symbol",
    "side",
    "decision_ts",
    "decision_price",
    "order_ts",
    "order_price",
    "fill_ts",
    "fill_price",
    "qty",
    "commission",
    "stamp_tax",
    "transfer_fee",
    "slippage",
    "market_impact",
    "source_artifact",
    "component_provenance",
}


@dataclass(frozen=True)
class DailyExecutionRequest:
    trade_day: str
    reference_equity: float
    reference_timestamp: str
    cost_evidence_level: str
    artifacts: dict[str, ArtifactInput]
    source_identity: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", dict(self.artifacts))
        object.__setattr__(self, "source_identity", dict(self.source_identity))
        if not str(self.trade_day):
            raise ValueError("trade_day is required")
        if not str(self.reference_timestamp):
            raise ValueError("reference_timestamp is required")
        try:
            equity = float(self.reference_equity)
        except (TypeError, ValueError) as exc:
            raise ValueError("reference_equity must be finite and positive") from exc
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("reference_equity must be finite and positive")
        if self.cost_evidence_level not in EVIDENCE_LEVELS:
            raise ValueError(f"invalid cost_evidence_level: {self.cost_evidence_level!r}")
        if not str(self.source_identity.get("data_sha") or ""):
            raise ValueError("source_identity.data_sha is required")
        missing = sorted(_REQUIRED_ARTIFACTS - set(self.artifacts))
        if missing:
            raise ValueError(f"required explicit artifacts missing: {', '.join(missing)}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_artifact(artifact: ArtifactInput) -> tuple[Any, dict[str, Any]]:
    if not artifact.source_path.is_file():
        raise ValueError(f"artifact_missing:{artifact.name}")
    actual = _file_sha256(artifact.source_path)
    if actual.lower() != artifact.expected_sha256.lower():
        raise ValueError(f"artifact_hash_mismatch:{artifact.name}")
    try:
        value = json.loads(artifact.source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact_invalid_json:{artifact.name}") from exc
    return value, {
        "path": str(artifact.source_path),
        "sha256": actual,
        "format": artifact.format,
    }


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_number:{field}") from exc
    if not math.isfinite(number) or (positive and number <= 0) or (not positive and number < 0):
        raise ValueError(f"invalid_number:{field}")
    return number


def _validate_weights(weights: dict[str, float], field: str) -> dict[str, float]:
    if "CASH" not in weights:
        raise ValueError(f"{field}:cash_weight_required")
    if any(not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ValueError(f"{field}:invalid_weight")
    if not math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise ValueError(f"{field}:weights_must_sum_to_one")
    return weights


def _positions_to_weights(payload: Any, field: str) -> dict[str, float]:
    if not isinstance(payload, dict):
        raise ValueError(f"{field}:must_be_object")
    if "positions" not in payload:
        return _validate_weights({str(key): _number(value, f"{field}.{key}") for key, value in payload.items()}, field)
    equity = _number(payload.get("equity"), f"{field}.equity", positive=True)
    cash = _number(payload.get("cash"), f"{field}.cash")
    positions = payload.get("positions")
    if not isinstance(positions, dict):
        raise ValueError(f"{field}:positions_must_be_object")
    weights = {"CASH": cash / equity}
    for symbol, position in positions.items():
        if not isinstance(position, dict):
            raise ValueError(f"{field}:position_not_object:{symbol}")
        qty = _number(position.get("qty"), f"{field}.{symbol}.qty")
        price = _number(position.get("last_price"), f"{field}.{symbol}.last_price", positive=True)
        weights[str(symbol)] = qty * price / equity
    return _validate_weights(weights, field)


def _target_to_weights(payload: Any) -> dict[str, float]:
    if not isinstance(payload, dict):
        raise ValueError("target_positions:must_be_object")
    if "top_n" not in payload:
        return _validate_weights({str(key): _number(value, f"target_positions.{key}") for key, value in payload.items()}, "target_positions")
    consume_day = payload.get("consume_day") or payload.get("trade_day") or payload.get("date") or payload.get("day")
    if consume_day is None:
        raise ValueError("target_positions:explicit_target_day_required")
    rows = payload.get("top_n")
    if not isinstance(rows, list):
        raise ValueError("target_positions:top_n_must_be_array")
    weights: dict[str, float] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"target_positions:row_not_object:{index}")
        symbol = row.get("canon") or row.get("symbol")
        if not symbol:
            raise ValueError(f"target_positions:symbol_missing:{index}")
        weight = _number(row.get("target_weight"), f"target_positions.{symbol}.target_weight")
        weights[str(symbol)] = weights.get(str(symbol), 0.0) + weight
    total = sum(weights.values())
    if total > 1.0 + 1e-8:
        raise ValueError("target_positions:weights_exceed_one")
    weights["CASH"] = max(0.0, 1.0 - total)
    return _validate_weights(weights, "target_positions")


def _rows(payload: Any, field: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    value = payload
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                value = payload[key]
                break
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{field}:rows_must_be_object_array")
    return value


def _blocked_cost_result(errors: list[str], level: str) -> dict[str, Any]:
    components = {
        key: None
        for key in ("commission", "stamp_tax", "transfer_fee", "slippage", "market_impact", "execution_price_cost", "opportunity_cost")
    }
    return {
        "schema_version": 1,
        "status": "blocked",
        "evidence_level": level,
        "records": [],
        "errors": errors,
        "summary": {
            "status": "blocked",
            "evidence_level": level,
            "total_cost": None,
            "components": components,
            "counted_components": {},
        },
    }


def _cost_replay(fills: list[dict[str, Any]], level: str) -> dict[str, Any]:
    if fills:
        missing = sorted(_CANONICAL_FILL_FIELDS - set(fills[0]))
        if missing:
            return _blocked_cost_result([f"canonical_fill_fields_missing:{','.join(missing)}"], level)
    return replay_costs(fills, evidence_level=level)


def _status(statuses: list[str]) -> str:
    order = {"available": 0, "not_applicable": 0, "pending_maturity": 1, "missing": 2, "blocked": 3, "invalid": 4, "tampered": 5}
    return max(statuses, key=lambda item: order[item]) if statuses else "available"


def build_daily_execution_evidence(request: DailyExecutionRequest) -> dict[str, Any]:
    """Load explicit artifacts and calculate daily evidence without side effects."""

    raw_evidence: dict[str, Any] = {}
    source_artifacts: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    try:
        for name in sorted(request.artifacts):
            value, metadata = _load_artifact(request.artifacts[name])
            raw_evidence[name] = value
            source_artifacts[name] = metadata
    except ValueError as exc:
        return {
            "schema_version": 1,
            "trade_day": request.trade_day,
            "status": "tampered" if "hash_mismatch" in str(exc) else "missing",
            "errors": [str(exc)],
            "source_artifacts": source_artifacts,
            "raw_evidence": raw_evidence,
            "turnover": None,
            "cost_replay": None,
        }

    snapshot = raw_evidence["snapshot"]
    snapshot_day = snapshot.get("trade_day") or snapshot.get("date") or snapshot.get("day") if isinstance(snapshot, dict) else None
    if snapshot_day not in {None, request.trade_day}:
        errors.append("artifact_trade_day_mismatch")
    target_payload = raw_evidence["target_positions"]
    target_day = (
        target_payload.get("consume_day")
        or target_payload.get("trade_day")
        or target_payload.get("date")
        or target_payload.get("day")
        if isinstance(target_payload, dict)
        else None
    )
    if target_day not in {None, request.trade_day}:
        errors.append("artifact_trade_day_mismatch")
    positions_after = raw_evidence["positions_after"]
    after_day = positions_after.get("trade_day") or positions_after.get("date") or positions_after.get("day") if isinstance(positions_after, dict) else None
    if after_day not in {None, request.trade_day}:
        errors.append("artifact_trade_day_mismatch")
    if errors:
        return {
            "schema_version": 1,
            "trade_day": request.trade_day,
            "status": "blocked",
            "errors": sorted(set(errors)),
            "source_artifacts": source_artifacts,
            "raw_evidence": raw_evidence,
            "turnover": None,
            "cost_replay": None,
        }

    try:
        previous_weights = _positions_to_weights(raw_evidence["positions_before"], "positions_before")
        _positions_to_weights(raw_evidence["positions_after"], "positions_after")
        target_weights = _target_to_weights(target_payload)
        planned_orders = _rows(raw_evidence["orders"], "orders", ("orders", "planned_orders"))
        fills = _rows(raw_evidence["fills"], "fills", ("fills", "trades"))
        turnover = compute_turnover(
            previous_actual_weights=previous_weights,
            target_weights=target_weights,
            planned_orders=planned_orders,
            fills=fills,
            reference_equity=request.reference_equity,
            reference_timestamp=request.reference_timestamp,
        )
        cost_replay = _cost_replay(fills, request.cost_evidence_level)
    except ValueError as exc:
        errors.append(str(exc))
        return {
            "schema_version": 1,
            "trade_day": request.trade_day,
            "status": "invalid",
            "errors": errors,
            "source_artifacts": source_artifacts,
            "raw_evidence": raw_evidence,
            "turnover": None,
            "cost_replay": None,
        }

    overall = _status([
        turnover["metrics"][name]["status"] for name in turnover["metrics"]
    ] + [cost_replay["status"]])
    return {
        "schema_version": 1,
        "trade_day": request.trade_day,
        "status": overall,
        "errors": errors + list(cost_replay.get("errors", [])),
        "source_artifacts": source_artifacts,
        "raw_evidence": raw_evidence,
        "normalized_weights": {
            "positions_before": previous_weights,
            "positions_after": _positions_to_weights(raw_evidence["positions_after"], "positions_after"),
            "target_positions": target_weights,
        },
        "turnover": turnover,
        "cost_replay": cost_replay,
    }
