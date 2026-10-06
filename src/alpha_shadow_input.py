"""Build explicit, normalized PIT inputs for the offline Alpha shadow runner.

This module is research-only.  It receives already materialized PIT rows and
forward-return observations, then produces the JSON contract consumed by
``alpha_shadow_compare.py``.  It does not import selector, PaperBook, signal
freeze, broker, or live execution state.
"""

from __future__ import annotations

import math
from statistics import mean
from typing import Any, Mapping, Sequence


XSEC_FIELDS = ("signal", "trend", "govern", "liquidity", "vol", "mom_rev")
FACTOR_FIELDS = ("pb_inv", "ep", "ocf_ps", "roe_yy_chg")


def _number(value: Any, *, field: str, symbol: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field} for symbol {symbol!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite {field} for symbol {symbol!r}")
    return parsed


def _symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if "." in raw:
        code, market = raw.split(".", 1)
        code = code.zfill(6)
        return f"{code}.{market}"
    code = raw.zfill(6)
    if not code.isdigit():
        raise ValueError(f"invalid symbol: {value!r}")
    if code.startswith(("60", "68", "90")):
        market = "SH"
    elif code.startswith(("0", "3")):
        market = "SZ"
    elif code.startswith(("4", "8")):
        market = "BSE"
    else:
        market = "SZ"
    return f"{code}.{market}"


def _sym6(value: Any) -> str:
    return _symbol(value).split(".", 1)[0]


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a quantile of an empty sequence")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _zscore(values: Mapping[str, float | None]) -> dict[str, float]:
    finite = [float(value) for value in values.values() if value is not None and math.isfinite(value)]
    if not finite:
        return {symbol: 0.0 for symbol in values}
    lo = _quantile(finite, 0.01)
    hi = _quantile(finite, 0.99)
    clipped = {symbol: min(max(float(value), lo), hi) if value is not None and math.isfinite(value) else None
               for symbol, value in values.items()}
    usable = [value for value in clipped.values() if value is not None]
    center = mean(usable)
    variance = sum((value - center) ** 2 for value in usable) / max(len(usable) - 1, 1)
    deviation = math.sqrt(variance)
    if not math.isfinite(deviation) or deviation <= 1e-12:
        return {symbol: 0.0 for symbol in values}
    return {
        symbol: (value - center) / deviation if value is not None else 0.0
        for symbol, value in clipped.items()
    }


def _old_selector_score(row: Mapping[str, Any], weights: Mapping[str, Any], symbol: str) -> float:
    signal = _number(row.get("signal"), field="signal", symbol=symbol)
    trend = _number(row.get("trend"), field="trend", symbol=symbol)
    govern = _number(row.get("govern"), field="govern", symbol=symbol)
    liquidity = _number(row.get("liquidity"), field="liquidity", symbol=symbol)
    vol = _number(row.get("vol"), field="vol", symbol=symbol)
    mom_rev = _number(row.get("mom_rev"), field="mom_rev", symbol=symbol)
    signal_part = min(max(0.5 + 2.0 * signal, 0.0), 1.0)
    trend_part = (trend + 1.0) / 2.0
    return sum(
        float(weights.get(field, 0.0)) * value
        for field, value in (
            ("signal", signal_part),
            ("trend", trend_part),
            ("govern", govern),
            ("liquidity", liquidity),
            ("vol", vol),
            ("mom_rev", mom_rev),
        )
    )


def _forward_map(values: Mapping[Any, Any] | None, symbol: str) -> dict[str, float]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError(f"forward_returns must be an object for symbol {symbol!r}")
    result = {}
    for horizon, value in values.items():
        result[str(horizon)] = _number(value, field=f"forward_returns[{horizon}]", symbol=symbol)
    return result


def build_normalized_payload(
    *,
    trade_day: str,
    xsec_rows: Sequence[Mapping[str, Any]],
    fusion_rows: Sequence[Mapping[str, Any]],
    forward_returns: Mapping[str, Mapping[Any, Any]],
    selector_weights: Mapping[str, Any],
    factor_weights: Mapping[str, Any],
    factor_directions: Mapping[str, Any],
    previous_symbols: Sequence[str] = (),
    strict_fusion: bool = True,
) -> dict[str, Any]:
    """Merge materialized PIT rows into the normalized shadow input contract."""

    if not xsec_rows:
        raise ValueError("xsec_rows cannot be empty")
    if not isinstance(forward_returns, Mapping):
        raise ValueError("forward_returns must be an object")
    for raw in xsec_rows:
        symbol = _symbol(raw.get("canon", raw.get("symbol")))
        missing = [field for field in XSEC_FIELDS if field not in raw]
        if missing:
            raise ValueError(f"missing xsec fields for {symbol!r}: {', '.join(missing)}")
    factor_maps: dict[str, dict[str, float | None]] = {field: {} for field in FACTOR_FIELDS}
    fusion_by_sym6: dict[str, Mapping[str, Any]] = {}
    for raw in fusion_rows:
        sym6 = _sym6(raw.get("symbol", raw.get("canon")))
        if sym6 in fusion_by_sym6:
            raise ValueError(f"duplicate fusion symbol: {sym6}")
        fusion_by_sym6[sym6] = raw
        for field in FACTOR_FIELDS:
            value = raw.get(field)
            if value is None:
                factor_maps[field][sym6] = None
            else:
                try:
                    parsed = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid {field} for symbol {sym6!r}") from exc
                factor_maps[field][sym6] = parsed if math.isfinite(parsed) else None

    z_maps = {field: _zscore(values) for field, values in factor_maps.items()}
    for field in FACTOR_FIELDS:
        if field not in factor_weights or field not in factor_directions:
            raise ValueError(f"missing fusion configuration for {field}")

    rows: list[dict[str, Any]] = []
    excluded_missing_fusion: list[str] = []
    seen: set[str] = set()
    for raw in xsec_rows:
        symbol = _symbol(raw.get("canon", raw.get("symbol")))
        sym6 = symbol.split(".", 1)[0]
        if sym6 in seen:
            raise ValueError(f"duplicate xsec symbol: {symbol}")
        seen.add(sym6)
        if sym6 not in fusion_by_sym6:
            if strict_fusion:
                raise ValueError(f"fusion row missing for symbol: {symbol}")
            excluded_missing_fusion.append(symbol)
            continue
        old_score = _old_selector_score(raw, selector_weights, symbol)
        fusion_score = 0.0
        for field in FACTOR_FIELDS:
            fusion_score += (
                float(factor_weights[field])
                * float(factor_directions[field])
                * z_maps[field].get(sym6, 0.0)
            )
        rows.append(
            {
                "symbol": symbol,
                "scores": {
                    "score": old_score,
                    "control_prod": old_score,
                    "signal": _number(raw.get("signal"), field="signal", symbol=symbol),
                    "fusion_A": fusion_score,
                    "selector_score": old_score,
                    # RANK_BY_FUSION=1 with the documented alpha=1.0 is pure fusion.
                    "fusion_rank_on": fusion_score,
                },
                "forward_returns": _forward_map(forward_returns.get(sym6), symbol),
            }
        )

    previous = []
    seen_previous: set[str] = set()
    for raw_symbol in previous_symbols:
        symbol = _symbol(raw_symbol)
        if symbol not in seen_previous:
            previous.append(symbol)
            seen_previous.add(symbol)
    return {
        "schema_version": 1,
        "trade_day": str(trade_day),
        "rows": rows,
        "previous_symbols": previous,
        "coverage": {
            "xsec_rows": len(xsec_rows),
            "included_rows": len(rows),
            "excluded_missing_fusion": excluded_missing_fusion,
        },
        "score_metadata": {
            "selector_weights": {str(k): float(v) for k, v in selector_weights.items()},
            "factor_weights": {str(k): float(v) for k, v in factor_weights.items()},
            "factor_directions": {str(k): int(v) for k, v in factor_directions.items()},
            "fusion_rank_on": "pure_fusion_alpha_1.0",
        },
    }


def forward_returns_from_bars(
    *,
    trading_days: Sequence[str],
    bars: Sequence[Mapping[str, Any]],
    trade_day: str,
    symbols: Sequence[str],
    horizons: Sequence[int],
) -> dict[str, dict[str, float]]:
    """Compound ``change_pct`` over mature forward windows.

    Missing or immature windows are omitted instead of being converted to zero.
    The caller may therefore distinguish an unavailable observation from a flat
    return.  ``bars`` is expected to contain rows from the same canonical bar
    source and is intentionally a plain sequence for easy fixture injection.
    """

    calendar = [str(day)[:10] for day in trading_days]
    try:
        start_index = calendar.index(str(trade_day)[:10])
    except ValueError as exc:
        raise ValueError(f"trade_day is not in the trading calendar: {trade_day}") from exc
    grouped: dict[str, list[tuple[str, float]]] = {}
    for raw in bars:
        day = str(raw.get("d", raw.get("date", "")))[:10]
        symbol = _sym6(raw.get("symbol", raw.get("canon")))
        value = _number(raw.get("change_pct"), field="change_pct", symbol=symbol)
        grouped.setdefault(symbol, []).append((day, value))

    output: dict[str, dict[str, float]] = {}
    for raw_symbol in symbols:
        symbol = _sym6(raw_symbol)
        observations = grouped.get(symbol, [])
        values: dict[str, float] = {}
        for horizon in horizons:
            horizon = int(horizon)
            if horizon <= 0:
                raise ValueError("forward horizons must be positive")
            end_index = start_index + horizon
            if end_index >= len(calendar):
                continue
            end_day = calendar[end_index]
            product = 1.0
            count = 0
            for day, change_pct in observations:
                if str(trade_day)[:10] < day <= end_day:
                    product *= 1.0 + change_pct / 100.0
                    count += 1
            if count:
                values[str(horizon)] = product - 1.0
        output[symbol] = values
    return output


def _finite_values(values: Sequence[Any]) -> list[float]:
    result = []
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            result.append(parsed)
    return result


def _summary(values: Sequence[Any]) -> dict[str, Any]:
    finite = _finite_values(values)
    return {"mean": mean(finite), "n": len(finite)} if finite else {"mean": None, "n": 0}


def aggregate_shadow_results(
    results: Sequence[Mapping[str, Any]],
    *,
    horizons: Sequence[int] = (1, 5, 10, 20, 60, 120),
) -> dict[str, Any]:
    """Aggregate per-day results without converting missing observations to zero."""

    from alpha_shadow import ARM_NAMES

    report: dict[str, Any] = {
        "schema_version": 1,
        "trade_days": [str(item.get("trade_day")) for item in results],
        "n_days": len(results),
        "arms": {},
        "comparisons": {},
    }
    for arm in ARM_NAMES:
        report["arms"][arm] = {
            "rank_ic": {
                str(horizon): _summary(
                    [
                        ((item.get("result") or {}).get("arms") or {}).get(arm, {}).get("rank_ic", {}).get(str(horizon))
                        for item in results
                    ]
                )
                for horizon in horizons
            },
            "forward_return_mean": {
                str(horizon): _summary(
                    [
                        ((item.get("result") or {}).get("arms") or {}).get(arm, {}).get("forward_return_mean", {}).get(str(horizon))
                        for item in results
                    ]
                )
                for horizon in horizons
            },
            "turnover": _summary(
                [
                    ((item.get("result") or {}).get("arms") or {}).get(arm, {}).get("turnover")
                    for item in results
                ]
            ),
        }
    comparison_names = set()
    for item in results:
        comparison_names.update(((item.get("result") or {}).get("comparisons") or {}).keys())
    for name in sorted(comparison_names):
        report["comparisons"][name] = {
            metric: _summary(
                [
                    (((item.get("result") or {}).get("comparisons") or {}).get(name) or {}).get(metric)
                    for item in results
                ]
            )
            for metric in ("top_n_jaccard", "rank_correlation")
        }
    return report
