# -*- coding: utf-8 -*-
"""Deterministic, side-effect-free turnover calculations for Evidence Bundles.

This module deliberately knows nothing about selectors, brokers, PaperBook, or
live state.  Callers must provide every position/order/fill input explicitly.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


TURNOVER_STATUSES = {
    "available",
    "pending_maturity",
    "not_applicable",
    "missing",
    "blocked",
    "invalid",
    "tampered",
}


def _metric(
    *,
    value: float | None,
    status: str,
    denominator_type: str,
    reference_equity: float,
    reference_timestamp: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    if status not in TURNOVER_STATUSES:
        raise ValueError(f"unknown turnover status: {status}")
    return {
        "value": value,
        "status": status,
        "denominator_type": denominator_type,
        "reference_equity": reference_equity,
        "reference_timestamp": reference_timestamp,
        "reason_code": reason_code,
    }

def _valid_reference(reference_equity: Any, reference_timestamp: Any) -> tuple[float, str]:
    try:
        equity = float(reference_equity)
    except (TypeError, ValueError) as exc:
        raise ValueError("reference_equity must be a finite positive number") from exc
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("reference_equity must be a finite positive number")
    timestamp = str(reference_timestamp or "")
    if not timestamp:
        raise ValueError("reference_timestamp is required")
    return equity, timestamp


def _weights(value: Mapping[str, Any] | None) -> tuple[dict[str, float] | None, str | None]:
    if value is None:
        return None, "missing_weights"
    if not isinstance(value, Mapping):
        return None, "weights_not_mapping"
    if "CASH" not in value:
        return None, "cash_weight_required"
    normalized: dict[str, float] = {}
    for symbol, raw in value.items():
        key = str(symbol)
        try:
            weight = float(raw)
        except (TypeError, ValueError):
            return None, f"invalid_weight:{key}"
        if not math.isfinite(weight) or weight < 0:
            return None, f"invalid_weight:{key}"
        normalized[key] = weight
    if not math.isclose(sum(normalized.values()), 1.0, rel_tol=0.0, abs_tol=1e-8):
        return None, "weights_must_sum_to_one"
    return normalized, None


def _notional_rows(rows: Sequence[Mapping[str, Any]] | None, price_key: str) -> tuple[float | None, str | None]:
    if rows is None:
        return None, "missing_rows"
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return None, "rows_not_sequence"
    if not rows:
        return None, "no_rows"
    total = 0.0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return None, f"row_not_mapping:{index}"
        if "qty" not in row or price_key not in row:
            return None, f"required_field_missing:{index}"
        try:
            qty = float(row["qty"])
            price = float(row[price_key])
        except (TypeError, ValueError):
            return None, f"invalid_notional:{index}"
        if not math.isfinite(qty) or not math.isfinite(price) or qty <= 0 or price <= 0:
            return None, f"invalid_notional:{index}"
        total += abs(qty * price)
    return total, None


def compute_turnover(
    *,
    previous_actual_weights: Mapping[str, Any] | None,
    target_weights: Mapping[str, Any] | None,
    planned_orders: Sequence[Mapping[str, Any]] | None,
    fills: Sequence[Mapping[str, Any]] | None,
    reference_equity: float,
    reference_timestamp: str,
) -> dict[str, Any]:
    """Compute all four fixed-turnover definitions.

    ``None`` means the source artifact is absent; an empty list means the
    artifact exists but no order/fill occurred.  Neither is converted to zero.
    Both weight vectors must explicitly include ``CASH``.
    """

    equity, timestamp = _valid_reference(reference_equity, reference_timestamp)
    previous, previous_error = _weights(previous_actual_weights)
    target, target_error = _weights(target_weights)

    metrics: dict[str, dict[str, Any]] = {}

    if previous_error == "missing_weights" or target_error == "missing_weights":
        metrics["target_weight_turnover"] = _metric(
            value=None,
            status="missing",
            denominator_type="reference_equity_weight_l1",
            reference_equity=equity,
            reference_timestamp=timestamp,
            reason_code="weights_missing",
        )
    elif previous is None or target is None:
        metrics["target_weight_turnover"] = _metric(
            value=None,
            status="invalid",
            denominator_type="reference_equity_weight_l1",
            reference_equity=equity,
            reference_timestamp=timestamp,
            reason_code=previous_error or target_error,
        )
    else:
        symbols = set(previous) | set(target)
        value = 0.5 * sum(abs(previous.get(symbol, 0.0) - target.get(symbol, 0.0)) for symbol in symbols)
        metrics["target_weight_turnover"] = _metric(
            value=value,
            status="available",
            denominator_type="reference_equity_weight_l1",
            reference_equity=equity,
            reference_timestamp=timestamp,
        )

    if previous_error == "missing_weights" or target_error == "missing_weights":
        metrics["name_turnover"] = _metric(
            value=None,
            status="missing",
            denominator_type="max_member_count",
            reference_equity=equity,
            reference_timestamp=timestamp,
            reason_code="weights_missing",
        )
    elif previous is None or target is None:
        metrics["name_turnover"] = _metric(
            value=None,
            status="invalid",
            denominator_type="max_member_count",
            reference_equity=equity,
            reference_timestamp=timestamp,
            reason_code=previous_error or target_error,
        )
    else:
        before = {symbol for symbol, weight in previous.items() if symbol != "CASH" and weight > 0}
        after = {symbol for symbol, weight in target.items() if symbol != "CASH" and weight > 0}
        denominator = max(len(before), len(after))
        if denominator == 0:
            metrics["name_turnover"] = _metric(
                value=None,
                status="not_applicable",
                denominator_type="max_member_count",
                reference_equity=equity,
                reference_timestamp=timestamp,
                reason_code="no_stock_members",
            )
        else:
            metrics["name_turnover"] = _metric(
                value=1.0 - len(before & after) / denominator,
                status="available",
                denominator_type="max_member_count",
                reference_equity=equity,
                reference_timestamp=timestamp,
            )

    planned_total, planned_error = _notional_rows(planned_orders, "order_price")
    if planned_error == "missing_rows":
        metrics["planned_turnover"] = _metric(
            value=None,
            status="missing",
            denominator_type="reference_equity_notional",
            reference_equity=equity,
            reference_timestamp=timestamp,
            reason_code="orders_missing",
        )
    elif planned_error == "no_rows":
        metrics["planned_turnover"] = _metric(
            value=None,
            status="not_applicable",
            denominator_type="reference_equity_notional",
            reference_equity=equity,
            reference_timestamp=timestamp,
            reason_code="no_planned_orders",
        )
    elif planned_error:
        metrics["planned_turnover"] = _metric(
            value=None,
            status="invalid",
            denominator_type="reference_equity_notional",
            reference_equity=equity,
            reference_timestamp=timestamp,
            reason_code=planned_error,
        )
    else:
        metrics["planned_turnover"] = _metric(
            value=planned_total / equity,
            status="available",
            denominator_type="reference_equity_notional",
            reference_equity=equity,
            reference_timestamp=timestamp,
        )

    executed_total, executed_error = _notional_rows(fills, "fill_price")
    if executed_error == "missing_rows":
        executed_status = "missing"
        reason = "fills_missing"
    elif executed_error == "no_rows":
        executed_status = "not_applicable"
        reason = "no_fills"
    elif executed_error:
        executed_status = "invalid"
        reason = executed_error
    else:
        executed_status = "available"
        reason = None
    metrics["executed_turnover"] = _metric(
        value=(executed_total / equity if executed_status == "available" else None),
        status=executed_status,
        denominator_type="reference_equity_notional",
        reference_equity=equity,
        reference_timestamp=timestamp,
        reason_code=reason,
    )

    return {
        "schema_version": 1,
        "metrics": metrics,
        "definitions": {
            "name_turnover": "member replacement only; not realized trading turnover",
            "target_weight_turnover": "0.5 * L1(previous actual weights - new target weights), including CASH",
            "planned_turnover": "sum absolute planned order notional / fixed reference equity",
            "executed_turnover": "sum absolute fill notional / fixed reference equity; fills only",
        },
    }
