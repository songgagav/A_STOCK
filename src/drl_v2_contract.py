# -*- coding: utf-8 -*-
"""Small, dependency-light contracts shared by the Phase C DRL path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class TemporalSplit:
    """Position-based split for a time-ordered training sequence.

    Indices are decision/reward timestamps.  ``purge`` is deliberately
    reported separately so callers cannot accidentally treat it as training
    data or silently fold it into validation.
    """

    n_rows: int
    lookback: int
    val_days: int
    purge_days: int
    train: tuple[int, ...]
    purge: tuple[int, ...]
    validation: tuple[int, ...]

    @property
    def train_end(self) -> int:
        """Exclusive end index for an environment used during learning."""
        return (self.train[-1] + 1) if self.train else self.lookback

    def as_dict(self) -> dict:
        return {
            "n_rows": self.n_rows,
            "lookback": self.lookback,
            "val_days": self.val_days,
            "purge_days": self.purge_days,
            "train": list(self.train),
            "purge": list(self.purge),
            "validation": list(self.validation),
            "train_end": self.train_end,
        }


def build_temporal_split(
    n_rows: int,
    *,
    lookback: int,
    val_days: int,
    purge_days: int,
) -> TemporalSplit:
    """Build a strict chronological train/purge/validation split.

    The validation tail is never part of training.  Purge rows immediately
    before validation are also never part of training, which leaves a visible
    embargo instead of relying on a comment or a caller convention.
    """
    n = int(n_rows)
    lb = int(lookback)
    val = int(val_days)
    purge = int(purge_days)
    if n < 0 or lb < 0 or val < 0 or purge < 0:
        raise ValueError("n_rows/lookback/val_days/purge_days must be non-negative")
    if lb >= n:
        return TemporalSplit(n, lb, val, purge, (), (), ())

    scored = list(range(lb, n))
    if val == 0:
        return TemporalSplit(n, lb, 0, purge, tuple(scored), (), ())
    if len(scored) <= val:
        return TemporalSplit(n, lb, val, purge, (), (), ())

    val_start = n - val
    purge_start = max(lb, val_start - purge)
    train = tuple(range(lb, purge_start))
    purged = tuple(range(purge_start, val_start))
    validation = tuple(range(val_start, n))
    if not train:
        return TemporalSplit(n, lb, val, purge, (), purged, validation)
    return TemporalSplit(n, lb, val, purge, train, purged, validation)


def combine_reward_components(
    components: Mapping[str, Sequence[float]],
    weights: Mapping[str, float],
    *,
    expected_length: int,
) -> np.ndarray:
    """Combine explicitly aligned per-step reward components.

    Aggregate after-the-fact scores are intentionally not accepted here:
    every component must have one finite value for every time step.  The
    caller can still record an aggregate score as metadata, but it cannot
    accidentally inject a future summary into PPO training.
    """
    length = int(expected_length)
    if length < 0:
        raise ValueError("expected_length must be non-negative")
    if not components:
        if weights:
            raise ValueError("reward weights supplied without components")
        return np.zeros(length, dtype=np.float32)
    if set(weights) - set(components):
        raise ValueError("reward weights reference missing components")
    values: list[np.ndarray] = []
    raw_weights = []
    for name, raw in components.items():
        arr = np.asarray(raw, dtype=np.float64)
        if arr.ndim != 1 or len(arr) != length:
            raise ValueError(f"reward component {name!r} must have length {length}")
        if not np.all(np.isfinite(arr)):
            raise ValueError(f"reward component {name!r} contains non-finite values")
        raw_weight = weights.get(name, 0.0)
        if isinstance(raw_weight, bool):
            raise ValueError(f"reward weight {name!r} must be numeric, not boolean")
        weight = float(raw_weight)
        if not np.isfinite(weight) or weight < 0:
            raise ValueError(f"reward weight {name!r} must be finite and non-negative")
        raw_weights.append(weight)
        values.append(arr)
    scale = max(raw_weights)
    if scale <= 0:
        raise ValueError("reward weights must contain a positive total")
    scaled = np.asarray(raw_weights, dtype=np.float64) / scale
    normalized = scaled / scaled.sum()
    return np.sum([arr * weight for arr, weight in zip(values, normalized)], axis=0).astype(np.float32)


def _average_rank(values: np.ndarray) -> np.ndarray:
    """Return one-based average ranks for a finite one-dimensional array."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError("values must be one-dimensional")
    order = np.argsort(arr, kind="mergesort")
    ranked = np.empty(len(arr), dtype=np.float64)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and arr[order[j]] == arr[order[i]]:
            j += 1
        ranked[order[i:j]] = (i + 1 + j) / 2.0
        i = j
    return ranked


def cross_sectional_rank_ic(
    scores: np.ndarray,
    forward_returns: np.ndarray,
    *,
    min_samples: int = 5,
) -> float:
    """Compute Spearman rank IC with average ranks and explicit missingness."""
    x = np.asarray(scores, dtype=np.float64)
    y = np.asarray(forward_returns, dtype=np.float64)
    if x.ndim != 1 or y.ndim != 1 or len(x) != len(y):
        raise ValueError("scores and forward_returns must be aligned vectors")
    mask = np.isfinite(x) & np.isfinite(y)
    if int(mask.sum()) < int(min_samples):
        return float("nan")
    rx = _average_rank(x[mask])
    ry = _average_rank(y[mask])
    rx -= rx.mean()
    ry -= ry.mean()
    denom = float(np.sqrt(np.dot(rx, rx) * np.dot(ry, ry)))
    if denom <= 0.0 or not np.isfinite(denom):
        return float("nan")
    out = float(np.dot(rx, ry) / denom)
    return out if np.isfinite(out) else float("nan")


def factor_long_short_return(
    scores: np.ndarray,
    forward_returns: np.ndarray,
    *,
    direction: int = 1,
    quantile: float = 0.2,
    min_samples: int = 5,
) -> float:
    """Return top-minus-bottom factor return for one aligned cross-section.

    ``direction`` is applied to scores only. Thus a negative-direction factor
    still reports a positive return when its economically preferred side wins.
    """
    x = np.asarray(scores, dtype=np.float64)
    y = np.asarray(forward_returns, dtype=np.float64)
    if x.ndim != 1 or y.ndim != 1 or len(x) != len(y):
        raise ValueError("scores and forward_returns must be aligned vectors")
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    if not 0.0 < float(quantile) <= 0.5:
        raise ValueError("quantile must be in (0, 0.5]")
    mask = np.isfinite(x) & np.isfinite(y)
    if int(mask.sum()) < int(min_samples):
        return float("nan")
    x = x[mask] * float(direction)
    y = y[mask]
    n_bucket = max(1, int(np.floor(len(x) * float(quantile))))
    order = np.argsort(x, kind="mergesort")
    bottom = y[order[:n_bucket]]
    top = y[order[-n_bucket:]]
    out = float(np.mean(top) - np.mean(bottom))
    return out if np.isfinite(out) else float("nan")


__all__ = [
    "TemporalSplit",
    "build_temporal_split",
    "combine_reward_components",
    "cross_sectional_rank_ic",
    "factor_long_short_return",
]
