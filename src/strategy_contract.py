"""Shared strategy-mode, weight, ranking, and TargetContract primitives.

This module is deliberately dependency-light.  It is used by production
callers as a validation boundary, not as a strategy or model implementation.
"""
from __future__ import annotations

import math
import os
from typing import Any, Mapping, Sequence


MODE_VALUES = ("off", "shadow", "enforce")


class WeightContractError(ValueError):
    """Raised when a weight payload cannot be authoritative."""


class TargetContractError(ValueError):
    """Raised when a target row cannot be normalized safely."""


class FusionContractError(ValueError):
    """Raised when enforce-mode Fusion cannot produce an authoritative result."""


def _mode(name: str, default: str = "shadow", env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = str(source.get(name, default) or default).strip().lower()
    return value if value in MODE_VALUES else default


def drl_plan_mode(env: Mapping[str, str] | None = None) -> str:
    return _mode("DRL_PLAN_MODE", env=env)


def fusion_weight_mode(env: Mapping[str, str] | None = None) -> str:
    return _mode("FUSION_WEIGHT_MODE", env=env)


def fusion_missing_policy(env: Mapping[str, str] | None = None) -> str:
    """Return the one missing-score policy shared by Fusion consumers."""
    return "blocked" if fusion_weight_mode(env) == "enforce" else "neutral"


def drl_plan_is_consumable(plan: Mapping[str, Any] | None,
                           env: Mapping[str, str] | None = None) -> bool:
    """Return whether a DRL plan may enter a production target path.

    Shadow and off are intentionally non-consumable.  Enforce requires an
    explicit promotion approval in the plan, so dependency availability alone
    can never activate a plan.
    """
    if drl_plan_mode(env) != "enforce" or not isinstance(plan, Mapping):
        return False
    promotion = plan.get("promotion")
    return isinstance(promotion, Mapping) and promotion.get("approved") is True


def normalize_weight_map(weights: Mapping[str, Any], *, allow_zero: bool = False,
                          tolerance: float = 1e-6) -> dict[str, float]:
    """Validate and normalize a finite positive weight mapping."""
    if not isinstance(weights, Mapping) or not weights:
        raise WeightContractError("weights must be a non-empty mapping")
    out: dict[str, float] = {}
    for key, value in weights.items():
        if not isinstance(key, str) or not key.strip():
            raise WeightContractError("weight key must be a non-empty string")
        if isinstance(value, bool):
            raise WeightContractError(f"weight {key} is boolean")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise WeightContractError(f"weight {key} is not numeric") from exc
        if not math.isfinite(number):
            raise WeightContractError(f"weight {key} is not finite")
        if number < 0 or (number == 0 and not allow_zero):
            raise WeightContractError(f"weight {key} must be positive")
        out[key] = number
    total = sum(out.values())
    if not math.isfinite(total) or total <= tolerance:
        raise WeightContractError("weight sum must be positive and finite")
    normalized = {key: value / total for key, value in out.items()}
    if not math.isclose(sum(normalized.values()), 1.0, rel_tol=0.0,
                        abs_tol=max(tolerance, 1e-12)):
        raise WeightContractError("normalized weight sum is not one")
    return normalized


def average_rank(values: Sequence[Any], *, missing: float = 0.5) -> list[float]:
    """Return percentile ranks with average positions for ties.

    Finite values receive positions from 0 to n-1 and are scaled to 0..1.
    Missing/non-finite values receive the explicit neutral rank.
    """
    n = len(values)
    result = [float(missing)] * n
    finite = []
    for index, value in enumerate(values):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            finite.append((index, number))
    if not finite:
        return result
    denominator = max(len(finite) - 1, 1)
    ordered = sorted(finite, key=lambda item: item[1])
    pos = 0
    while pos < len(ordered):
        end = pos + 1
        value = ordered[pos][1]
        while end < len(ordered) and ordered[end][1] == value:
            end += 1
        average_position = (pos + end - 1) / 2.0
        rank = average_position / denominator
        for index, _ in ordered[pos:end]:
            result[index] = float(rank)
        pos = end
    return result


def normalize_target_contract(items: Sequence[Mapping[str, Any]], *,
                              require_weights: bool = False,
                              tolerance: float = 1e-6) -> list[dict[str, Any]]:
    """Copy target rows without dropping provenance fields."""
    if not isinstance(items, (list, tuple)):
        raise TargetContractError("targets must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    weights: list[float] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise TargetContractError("target must be an object")
        copied = dict(item)
        canon = copied.get("canon")
        if not isinstance(canon, str) or not canon.strip():
            raise TargetContractError("target missing canon")
        canon = canon.strip()
        if canon in seen:
            raise TargetContractError(f"duplicate target: {canon}")
        seen.add(canon)
        copied["canon"] = canon
        if "score" in copied and copied["score"] is not None:
            try:
                score = float(copied["score"])
            except (TypeError, ValueError) as exc:
                raise TargetContractError(f"invalid score: {canon}") from exc
            if not math.isfinite(score):
                raise TargetContractError(f"non-finite score: {canon}")
            copied["score"] = score
        if "target_weight" in copied and copied["target_weight"] is not None:
            value = copied["target_weight"]
            if isinstance(value, bool):
                raise TargetContractError(f"invalid target_weight: {canon}")
            try:
                weight = float(value)
            except (TypeError, ValueError) as exc:
                raise TargetContractError(f"invalid target_weight: {canon}") from exc
            if not math.isfinite(weight) or weight <= 0:
                raise TargetContractError(f"invalid target_weight: {canon}")
            copied["target_weight"] = weight
            weights.append(weight)
        elif require_weights:
            raise TargetContractError(f"missing target_weight: {canon}")
        result.append(copied)
    if require_weights:
        if not weights or not math.isclose(sum(weights), 1.0, rel_tol=0.0,
                                           abs_tol=tolerance):
            raise TargetContractError("target weights must sum to one")
    return result


__all__ = [
    "MODE_VALUES", "WeightContractError", "TargetContractError", "FusionContractError",
    "drl_plan_mode", "fusion_weight_mode", "fusion_missing_policy",
    "drl_plan_is_consumable",
    "normalize_weight_map", "average_rank", "normalize_target_contract",
]
