"""Finite, nonnegative reward configuration shared by producers and training."""

import math
from collections.abc import Mapping


DEFAULT_REWARD_WEIGHTS = {"vnpy_weight": 0.6, "ic_weight": 0.4, "attr_weight": 0.15}


def normalize_reward_weights(weights: Mapping) -> dict[str, float]:
    if not isinstance(weights, Mapping):
        raise ValueError("reward weights must be an object")
    values = {}
    for key in DEFAULT_REWARD_WEIGHTS:
        try:
            if isinstance(weights[key], bool):
                raise ValueError("boolean weight")
            value = float(weights[key])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"reward weight {key} must be numeric") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"reward weight {key} must be finite and nonnegative")
        values[key] = value
    scale = max(values.values())
    if scale <= 0:
        raise ValueError("reward weights must have a positive total")
    scaled = {key: value / scale for key, value in values.items()}
    total = math.fsum(scaled.values())
    return {key: value / total for key, value in scaled.items()}
