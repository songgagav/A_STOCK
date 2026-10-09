# -*- coding: utf-8 -*-
"""Transaction-level cost replay for the offline Evidence Bundle path.

The replay never mutates a broker or portfolio ledger.  It consumes explicit
fill evidence and keeps raw component provenance so embedded execution costs
cannot be charged a second time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


EVIDENCE_LEVELS = {"estimated", "simulated", "realized"}
STATUSES = {
    "available",
    "pending_maturity",
    "not_applicable",
    "missing",
    "blocked",
    "invalid",
    "tampered",
}
_COMPONENTS = ("commission", "stamp_tax", "transfer_fee", "slippage", "market_impact")
_BENCHMARK_TYPES = {
    "decision_price",
    "arrival_price",
    "vwap",
    "twap",
    "close",
    "execution_window_mark",
}
_REQUIRED_FIELDS = (
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
    *_COMPONENTS,
    "source_artifact",
    "component_provenance",
)
_SUMMARY_COMPONENTS = (*_COMPONENTS, "execution_price_cost", "opportunity_cost")


def _number(value: Any, field: str, *, positive: bool = False) -> tuple[float | None, str | None]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"invalid_number:{field}"
    if not math.isfinite(number) or (positive and number <= 0) or (not positive and number < 0):
        return None, f"invalid_number:{field}"
    return number, None


def _benchmark_cost(row: Mapping[str, Any], fill_price: float, qty: float, side: str) -> tuple[float | None, str | None, str | None]:
    benchmark = row.get("opportunity_benchmark")
    if benchmark is None:
        return None, "benchmark_undefined", None
    if not isinstance(benchmark, Mapping):
        return None, "benchmark_invalid", None
    benchmark_type = str(benchmark.get("type") or "")
    if benchmark_type not in _BENCHMARK_TYPES:
        return None, "benchmark_type_invalid", benchmark_type or None
    raw_price = benchmark.get("price")
    if raw_price is None and benchmark_type == "decision_price":
        raw_price = row.get("decision_price")
    benchmark_price, error = _number(raw_price, "benchmark.price", positive=True)
    if error:
        return None, "benchmark_price_missing", benchmark_type
    timestamp = benchmark.get("timestamp")
    if benchmark_type == "decision_price" and timestamp is None:
        timestamp = row.get("decision_ts")
    if not str(timestamp or ""):
        return None, "benchmark_window_missing", benchmark_type
    if side == "buy":
        cost = max(fill_price - benchmark_price, 0.0) * qty
    else:
        cost = max(benchmark_price - fill_price, 0.0) * qty
    return cost, None, benchmark_type


def _invalid_result(status: str, errors: list[str], evidence_level: str | None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "evidence_level": evidence_level,
        "records": [],
        "errors": errors,
        "summary": {
            "status": status,
            "evidence_level": evidence_level,
            "total_cost": None,
            "components": {component: None for component in _SUMMARY_COMPONENTS},
            "counted_components": {},
        },
    }


def _validate_row(row: Mapping[str, Any], index: int, evidence_level: str) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    for field in _REQUIRED_FIELDS:
        if field not in row or row[field] is None or row[field] == "":
            errors.append(f"row[{index}] missing:{field}")
    if errors:
        return None, errors

    side = str(row["side"]).lower()
    if side not in {"buy", "sell"}:
        errors.append(f"row[{index}] invalid:side")
    numeric: dict[str, float] = {}
    for field in ("decision_price", "order_price", "fill_price", "qty", *_COMPONENTS):
        number, error = _number(row[field], field, positive=field in {"decision_price", "order_price", "fill_price", "qty"})
        if error:
            errors.append(f"row[{index}] {error}")
        else:
            numeric[field] = number
    if side == "buy" and numeric.get("stamp_tax", 0.0) > 1e-12:
        errors.append(f"row[{index}] buy_stamp_tax_must_be_zero")

    if "notional" in row and row["notional"] is not None:
        supplied, error = _number(row["notional"], "notional", positive=True)
        if error or abs(supplied - numeric.get("fill_price", 0) * numeric.get("qty", 0)) > 1e-8:
            errors.append(f"row[{index}] notional_mismatch")

    provenance = row.get("component_provenance")
    if not isinstance(provenance, Mapping):
        errors.append(f"row[{index}] invalid:component_provenance")
    else:
        for component in _COMPONENTS:
            item = provenance.get(component)
            if not isinstance(item, Mapping) or not isinstance(item.get("embedded_in_fill_price"), bool):
                errors.append(f"row[{index}] provenance_missing:{component}")
            elif not str(item.get("source") or ""):
                errors.append(f"row[{index}] provenance_source_missing:{component}")

    if errors:
        return None, errors

    fill_price = numeric["fill_price"]
    qty = numeric["qty"]
    decision_price = numeric["decision_price"]
    embedded_execution = any(
        bool(provenance[component]["embedded_in_fill_price"])
        for component in ("slippage", "market_impact")
    )
    if embedded_execution:
        adverse_delta = (fill_price - decision_price) if side == "buy" else (decision_price - fill_price)
        execution_price_cost = max(adverse_delta, 0.0) * qty
    else:
        execution_price_cost = 0.0

    opportunity_cost, opportunity_reason, benchmark_type = _benchmark_cost(row, fill_price, qty, side)
    counted_components: dict[str, float] = {}
    for component in _COMPONENTS:
        if provenance[component]["embedded_in_fill_price"]:
            counted_components[component] = 0.0
        else:
            counted_components[component] = numeric[component]
    counted_components["execution_price_cost"] = execution_price_cost
    # Opportunity cost is an attribution metric, not a transaction fee.  It
    # is retained in the record and summary but is deliberately excluded from
    # ``total_cost`` so it cannot be confused with execution cost or charged
    # twice when the fill price already embeds slippage/impact.
    counted_components["opportunity_cost"] = opportunity_cost or 0.0
    total_cost = sum(
        value for key, value in counted_components.items()
        if key != "opportunity_cost"
    )

    record = {
        "order_id": str(row["order_id"]),
        "fill_id": str(row["fill_id"]),
        "symbol": str(row["symbol"]),
        "side": side,
        "decision_ts": str(row["decision_ts"]),
        "decision_price": decision_price,
        "order_ts": str(row["order_ts"]),
        "order_price": numeric["order_price"],
        "fill_ts": str(row["fill_ts"]),
        "fill_price": fill_price,
        "qty": qty,
        "notional": fill_price * qty,
        "commission": numeric["commission"],
        "stamp_tax": numeric["stamp_tax"],
        "transfer_fee": numeric["transfer_fee"],
        "slippage": numeric["slippage"],
        "market_impact": numeric["market_impact"],
        "execution_price_cost": execution_price_cost,
        "opportunity_cost": opportunity_cost,
        "opportunity_cost_reason": opportunity_reason,
        "opportunity_benchmark_type": benchmark_type,
        "total_cost": total_cost,
        "evidence_level": evidence_level,
        "source_artifact": str(row["source_artifact"]),
        "component_provenance": {
            component: {
                "embedded_in_fill_price": bool(provenance[component]["embedded_in_fill_price"]),
                "source": str(provenance[component]["source"]),
            }
            for component in _COMPONENTS
        },
        "counted_components": counted_components,
    }
    return record, []


def replay_costs(
    fills: Sequence[Mapping[str, Any]] | None,
    *,
    evidence_level: str,
) -> dict[str, Any]:
    """Replay explicit fill costs without inventing missing evidence."""

    level = str(evidence_level or "")
    if level not in EVIDENCE_LEVELS:
        return _invalid_result("invalid", [f"invalid:evidence_level:{level}"], level or None)
    if fills is None:
        return _invalid_result("missing", ["fills_missing"], level)
    if not isinstance(fills, Sequence) or isinstance(fills, (str, bytes, bytearray)):
        return _invalid_result("invalid", ["fills_not_sequence"], level)
    if not fills:
        return {
            "schema_version": 1,
            "status": "not_applicable",
            "evidence_level": level,
            "records": [],
            "errors": [],
            "summary": {
                "status": "not_applicable",
                "evidence_level": level,
                "total_cost": None,
                "components": {component: None for component in _SUMMARY_COMPONENTS},
                "counted_components": {},
            },
        }

    if level == "realized" and any(not bool(row.get("realized_fill")) for row in fills if isinstance(row, Mapping)):
        return _invalid_result("blocked", ["realized_requires_explicit_real_fill"], level)

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, row in enumerate(fills):
        if not isinstance(row, Mapping):
            errors.append(f"row[{index}] not_mapping")
            continue
        record, row_errors = _validate_row(row, index, level)
        if row_errors:
            errors.extend(row_errors)
        elif record is not None:
            records.append(record)
    if errors:
        return _invalid_result("invalid", errors, level)

    raw_components = {
        component: sum(record[component] for record in records)
        for component in _COMPONENTS
    }
    raw_components["execution_price_cost"] = sum(
        record["execution_price_cost"] for record in records
    )
    raw_components["opportunity_cost"] = sum(
        record["opportunity_cost"] or 0.0 for record in records
    )
    counted_components = {
        component: sum(record["counted_components"].get(component, 0.0) for record in records)
        for component in _COMPONENTS
    }
    counted_components["execution_price_cost"] = sum(
        record["counted_components"]["execution_price_cost"] for record in records
    )
    counted_components["opportunity_cost"] = sum(
        record["counted_components"]["opportunity_cost"] for record in records
    )
    total_cost = sum(counted_components.values())
    return {
        "schema_version": 1,
        "status": "available",
        "evidence_level": level,
        "records": records,
        "errors": [],
        "summary": {
            "status": "available",
            "evidence_level": level,
            "total_cost": total_cost,
            "components": raw_components,
            "counted_components": counted_components,
        },
    }
