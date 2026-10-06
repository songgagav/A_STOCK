"""Pure, auditable Alpha shadow comparison helpers.

This module deliberately has no production selector, broker, or data-source
side effects. Callers provide one normalized PIT cross-section and named score
columns; the module only ranks the same rows through diagnostic arms and
records an experiment identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence


ARM_NAMES = (
    "control_prod",
    "legacy_signal",
    "fusion_A",
    "selector_score",
    "fusion_rank_on",
)

_DEFAULT_SCORE_FIELDS = {
    "control_prod": "score",
    "legacy_signal": "signal",
    "fusion_A": "fusion_A",
    "selector_score": "selector_score",
    "fusion_rank_on": "fusion_rank_on",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def build_experiment_manifest(
    *,
    code_sha: str,
    input_hashes: Mapping[str, str],
    universe_symbols: Sequence[str],
    factor_version: str,
    direction_version: str,
    weight_version: str,
    selector_variant: str,
    env_flags: Mapping[str, str],
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """Build a stable cache identity for one shadow experiment."""

    symbols = sorted({str(symbol) for symbol in universe_symbols})
    try:
        normalized_cost_bps = float(cost_bps)
    except (TypeError, ValueError) as exc:
        raise ValueError("cost_bps must be a finite non-negative number") from exc
    if not math.isfinite(normalized_cost_bps) or normalized_cost_bps < 0:
        raise ValueError("cost_bps must be a finite non-negative number")
    config = {
        "schema_version": 1,
        "arms": list(ARM_NAMES),
        "factor_version": str(factor_version),
        "direction_version": str(direction_version),
        "weight_version": str(weight_version),
        "selector_variant": str(selector_variant),
        "env_flags": dict(sorted((str(k), str(v)) for k, v in env_flags.items())),
        "cost_bps": normalized_cost_bps,
    }
    manifest = {
        "schema_version": 1,
        "code_sha": str(code_sha),
        "input_hashes": dict(sorted((str(k), str(v)) for k, v in input_hashes.items())),
        "universe_hash": _sha256(symbols),
        "universe_size": len(symbols),
        "factor_version": config["factor_version"],
        "direction_version": config["direction_version"],
        "weight_version": config["weight_version"],
        "selector_variant": config["selector_variant"],
        "env_flags": config["env_flags"],
        "cost_bps": config["cost_bps"],
        "arms": list(ARM_NAMES),
        "experiment_config_hash": _sha256(config),
    }
    manifest["experiment_hash"] = _sha256(manifest)
    return manifest


def _score_field(arm: str, score_fields: Mapping[str, str] | None) -> str:
    if arm not in ARM_NAMES:
        raise ValueError(f"unknown shadow arm: {arm}")
    fields = dict(_DEFAULT_SCORE_FIELDS)
    if score_fields:
        fields.update(score_fields)
    field = fields.get(arm)
    if not field:
        raise ValueError(f"missing score field for shadow arm: {arm}")
    return field


def rank_arm(
    rows: Sequence[Mapping[str, Any]],
    arm: str,
    *,
    score_fields: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Rank one arm using only the supplied normalized rows."""

    field = _score_field(arm, score_fields)
    ranked: list[tuple[float, str, Mapping[str, Any]]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        scores = row.get("scores")
        raw_score = scores.get(field) if isinstance(scores, Mapping) else None
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = math.nan
        if not symbol or not math.isfinite(score):
            raise ValueError(f"invalid {field} score for symbol {symbol!r}")
        ranked.append((score, symbol, row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [dict(row) for _, _, row in ranked]


def _rank_correlation(reference: Sequence[str], candidate: Sequence[str]) -> float | None:
    ref_pos = {symbol: index + 1 for index, symbol in enumerate(reference)}
    cand_pos = {symbol: index + 1 for index, symbol in enumerate(candidate)}
    common = sorted(set(ref_pos) & set(cand_pos))
    n = len(common)
    if n < 2:
        return None
    d_squared = sum((ref_pos[symbol] - cand_pos[symbol]) ** 2 for symbol in common)
    return 1.0 - (6.0 * d_squared) / (n * (n * n - 1))


def _top_n_jaccard(reference: Sequence[str], candidate: Sequence[str], top_n: int) -> float | None:
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    reference_set = set(reference[:top_n])
    candidate_set = set(candidate[:top_n])
    union = reference_set | candidate_set
    if not union:
        return None
    return len(reference_set & candidate_set) / len(union)


def _forward_value(row: Mapping[str, Any], horizon: int) -> float | None:
    values = row.get("forward_returns")
    if not isinstance(values, Mapping):
        return None
    raw = values.get(str(horizon), values.get(horizon))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _turnover(selected: Sequence[str], previous: Sequence[str] | None) -> float | None:
    if previous is None:
        return None
    selected_set = set(selected)
    previous_set = {str(symbol) for symbol in previous}
    denominator = max(len(selected_set), len(previous_set))
    if denominator == 0:
        return None
    return 1.0 - len(selected_set & previous_set) / denominator


def _rank_ic(
    arm_rows: Sequence[Mapping[str, Any]],
    horizon: int,
) -> float | None:
    valid = [
        (str(row["symbol"]), value)
        for row in arm_rows
        if (value := _forward_value(row, horizon)) is not None
    ]
    if len(valid) < 2:
        return None
    score_order = [symbol for symbol, _ in valid]
    return_order = [symbol for symbol, _ in sorted(valid, key=lambda item: (-item[1], item[0]))]
    return _rank_correlation(score_order, return_order)


def _forward_return_mean(
    arm_rows: Sequence[Mapping[str, Any]],
    horizon: int,
) -> float | None:
    values = [
        value
        for row in arm_rows
        if (value := _forward_value(row, horizon)) is not None
    ]
    return mean(values) if values else None


def _benchmark_value(
    benchmark_returns: Mapping[Any, Any] | None,
    horizon: int,
) -> float | None:
    if benchmark_returns is None:
        return None
    raw = benchmark_returns.get(str(horizon), benchmark_returns.get(horizon))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _normalize_benchmark_returns(
    benchmark_returns: Mapping[Any, Any] | None,
) -> dict[str, float] | None:
    if benchmark_returns is None:
        return None
    if not isinstance(benchmark_returns, Mapping):
        raise ValueError("benchmark_returns must be an object")
    normalized: dict[str, float] = {}
    for horizon, raw in benchmark_returns.items():
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid benchmark return for horizon {horizon!r}") from exc
        if not math.isfinite(value):
            raise ValueError(f"non-finite benchmark return for horizon {horizon!r}")
        normalized[str(horizon)] = value
    return normalized


def _excess_return_mean(
    arm_rows: Sequence[Mapping[str, Any]],
    horizon: int,
    benchmark_returns: Mapping[Any, Any] | None,
) -> float | None:
    gross = _forward_return_mean(arm_rows, horizon)
    benchmark = _benchmark_value(benchmark_returns, horizon)
    if gross is None or benchmark is None:
        return None
    return gross - benchmark


def _cost_adjusted_forward_return_mean(
    arm_rows: Sequence[Mapping[str, Any]],
    selected_symbols: Sequence[str],
    previous_symbols: Sequence[str] | None,
    horizon: int,
    cost_bps: float,
) -> float | None:
    gross = _forward_return_mean(arm_rows, horizon)
    turnover = _turnover(selected_symbols, previous_symbols)
    if gross is None or turnover is None:
        return None
    # This is a sensitivity proxy, not a realized A-share transaction-cost
    # calculation. The assumption is recorded in the batch report.
    return gross - turnover * cost_bps / 10000.0


def evaluate_shadow_arms(
    rows: Sequence[Mapping[str, Any]],
    *,
    top_n: int = 10,
    score_fields: Mapping[str, str] | None = None,
    previous_symbols: Sequence[str] | None = None,
    forward_horizons: Sequence[int] = (1, 5, 10, 20, 60, 120),
    benchmark_returns: Mapping[Any, Any] | None = None,
    benchmark_name: str | None = None,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """Rank all diagnostic arms and compare each with ``control_prod``."""

    try:
        normalized_cost_bps = float(cost_bps)
    except (TypeError, ValueError) as exc:
        raise ValueError("cost_bps must be a finite non-negative number") from exc
    if not math.isfinite(normalized_cost_bps) or normalized_cost_bps < 0:
        raise ValueError("cost_bps must be a finite non-negative number")
    benchmark_returns = _normalize_benchmark_returns(benchmark_returns)

    ranked = {
        arm: rank_arm(rows, arm, score_fields=score_fields)
        for arm in ARM_NAMES
    }
    ranked_symbols = {
        arm: [str(row["symbol"]) for row in arm_rows]
        for arm, arm_rows in ranked.items()
    }
    comparisons = {}
    for arm in ARM_NAMES:
        if arm == "control_prod":
            continue
        comparisons[arm] = {
            "common_symbols": len(
                set(ranked_symbols["control_prod"]) & set(ranked_symbols[arm])
            ),
            "top_n_jaccard": _top_n_jaccard(
                ranked_symbols["control_prod"], ranked_symbols[arm], top_n
            ),
            "rank_correlation": _rank_correlation(
                ranked_symbols["control_prod"], ranked_symbols[arm]
            ),
        }
    result = {
        "schema_version": 1,
        "row_count": len(rows),
        "top_n": top_n,
        "cost_bps": normalized_cost_bps,
        "arms": {
            arm: {
                "symbols": ranked_symbols[arm][:top_n],
                "ranked_symbols": ranked_symbols[arm],
                "field": _score_field(arm, score_fields),
                "forward_return_mean": {
                    str(horizon): _forward_return_mean(ranked[arm][:top_n], horizon)
                    for horizon in forward_horizons
                },
                "rank_ic": {
                    str(horizon): _rank_ic(ranked[arm], horizon)
                    for horizon in forward_horizons
                },
                "excess_return_mean": {
                    str(horizon): _excess_return_mean(
                        ranked[arm][:top_n], horizon, benchmark_returns
                    )
                    for horizon in forward_horizons
                },
                "cost_adjusted_forward_return_mean": {
                    str(horizon): _cost_adjusted_forward_return_mean(
                        ranked[arm][:top_n],
                        ranked_symbols[arm][:top_n],
                        previous_symbols,
                        horizon,
                        normalized_cost_bps,
                    )
                    for horizon in forward_horizons
                },
                "turnover": _turnover(ranked_symbols[arm][:top_n], previous_symbols),
            }
            for arm in ARM_NAMES
        },
        "comparisons": comparisons,
    }
    if benchmark_returns is not None:
        benchmark = {
            "name": str(benchmark_name or "explicit"),
            "returns": {
                str(horizon): value
                for horizon in forward_horizons
                if (value := _benchmark_value(benchmark_returns, horizon)) is not None
            },
        }
        if benchmark["returns"]:
            result["benchmark"] = benchmark
    return result


def _atomic_write_json(target: Path, payload: Mapping[str, Any], prefix: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=prefix,
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = handle.name
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    return target


def write_manifest(cache_root: str | os.PathLike[str], manifest: Mapping[str, Any]) -> Path:
    """Atomically write ``manifest.json`` under its experiment hash directory."""

    experiment_hash = str(manifest.get("experiment_hash") or "")
    if len(experiment_hash) != 64:
        raise ValueError("manifest must contain a SHA-256 experiment_hash")
    target = Path(cache_root) / experiment_hash / "manifest.json"
    return _atomic_write_json(target, manifest, "manifest.")


def write_result(
    cache_root: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Path:
    """Atomically write one shadow result beside its manifest."""

    experiment_hash = str(manifest.get("experiment_hash") or "")
    if len(experiment_hash) != 64:
        raise ValueError("manifest must contain a SHA-256 experiment_hash")
    if result.get("experiment_hash") != experiment_hash:
        raise ValueError("result experiment_hash does not match manifest")
    target = Path(cache_root) / experiment_hash / "result.json"
    return _atomic_write_json(target, result, "result.")
