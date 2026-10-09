"""Build and run a multi-day Alpha shadow comparison from PIT artifacts.

The adapter is intentionally separate from the offline runner.  It reads
materialized ``xsec``/``fusion_x`` parquet files and an h5i database in
read-only mode, writes only an explicit shadow-input/cache directory, and
never writes selection, PaperBook, signal-freeze, or broker state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from alpha_shadow import _atomic_write_json  # noqa: E402
from alpha_shadow_compare import run_shadow_file  # noqa: E402
from alpha_shadow_input import (  # noqa: E402
    aggregate_shadow_results,
    benchmark_from_forward_returns,
    build_normalized_payload,
    forward_returns_from_bars,
)


FACTOR_FIELDS = ("pb_inv", "ep", "ocf_ps", "roe_yy_chg")
DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.parquet$")


def load_factor_config(path: str | Path) -> tuple[dict[str, float], dict[str, int]]:
    """Load the factor weights/directions used by the research artifact."""

    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read factor config {path}: {exc}") from exc
    weights = payload.get("factor_weights")
    directions = payload.get("directions")
    if not isinstance(weights, Mapping) or not isinstance(directions, Mapping):
        raise ValueError("factor config must contain factor_weights and directions objects")
    missing = [field for field in FACTOR_FIELDS if field not in weights or field not in directions]
    if missing:
        raise ValueError(f"factor config missing fields: {', '.join(missing)}")
    return (
        {field: float(weights[field]) for field in FACTOR_FIELDS},
        {field: int(directions[field]) for field in FACTOR_FIELDS},
    )


def _parquet_days(path: Path) -> set[str]:
    return {match.group(1) for item in path.glob("*.parquet") if (match := DAY_RE.match(item.name))}


def select_common_days(xsec_dir: str | Path, fusion_dir: str | Path) -> list[str]:
    """Return only dates with both PIT artifacts present."""

    xsec_days = _parquet_days(Path(xsec_dir))
    fusion_days = _parquet_days(Path(fusion_dir))
    days = sorted(xsec_days & fusion_days)
    if not days:
        raise ValueError("no common xsec/fusion_x PIT dates found")
    return days


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd

        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot read PIT parquet {path}: {exc}") from exc
    return frame.to_dict(orient="records")


def load_h5i_forward_returns(
    bar_db: str | Path,
    *,
    trade_day: str,
    symbols: Sequence[str],
    horizons: Sequence[int],
) -> dict[str, dict[str, float]]:
    """Read forward bars from h5i without importing production data paths."""

    try:
        import h5i_db
    except ImportError as exc:
        raise RuntimeError("h5i_db is required for the real shadow adapter") from exc

    from alpha_shadow_input import _sym6

    normalized_symbols = sorted({_sym6(symbol) for symbol in symbols})
    if not normalized_symbols:
        return {}
    db = h5i_db.Database(str(bar_db))
    try:
        calendar_frame = db.sql(
            "SELECT DISTINCT CAST(ts AS DATE) AS d FROM daily_bars ORDER BY d"
        ).to_pandas()
        calendar = [str(value)[:10] for value in calendar_frame["d"].tolist()]
        if trade_day not in calendar:
            raise ValueError(f"trade_day is not present in h5i calendar: {trade_day}")
        start = calendar.index(trade_day)
        valid_horizons = [int(horizon) for horizon in horizons if int(horizon) > 0]
        if not valid_horizons:
            raise ValueError("horizons must contain positive integers")
        last_index = min(start + max(valid_horizons), len(calendar) - 1)
        end_day = calendar[last_index]
        quoted = ",".join(f"'{symbol}'" for symbol in normalized_symbols)
        frame = db.sql(
            "SELECT CAST(ts AS DATE) AS d, symbol, change_pct FROM daily_bars "
            f"WHERE CAST(ts AS DATE) > DATE '{trade_day}' "
            f"AND CAST(ts AS DATE) <= DATE '{end_day}' "
            f"AND symbol IN ({quoted}) AND change_pct IS NOT NULL "
            "ORDER BY d, symbol"
        ).to_pandas()
        bars = frame.to_dict(orient="records")
        return forward_returns_from_bars(
            trading_days=calendar,
            bars=bars,
            trade_day=trade_day,
            symbols=normalized_symbols,
            horizons=valid_horizons,
        )
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


def _selector_weights(as_of: str) -> dict[str, float]:
    """Read historical selector weights without changing runtime state."""

    from factor_library import selector_weights

    return {str(key): float(value) for key, value in selector_weights(as_of=as_of).items()}


def _write_input(path: Path, payload: Mapping[str, Any]) -> Path:
    return _atomic_write_json(path, payload, "shadow-input.")


def run_batch(
    *,
    xsec_dir: str | Path,
    fusion_dir: str | Path,
    bar_db: str | Path,
    factor_config: str | Path,
    input_root: str | Path,
    cache_root: str | Path,
    report_path: str | Path,
    code_sha: str,
    factor_version: str,
    direction_version: str,
    weight_version: str,
    selector_variant: str = "production-default",
    top_n: int = 10,
    horizons: Sequence[int] = (1, 5, 10, 20, 60, 120),
    limit: int | None = None,
    env_flags: Mapping[str, str] | None = None,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    factor_weights, factor_directions = load_factor_config(factor_config)
    days = select_common_days(xsec_dir, fusion_dir)
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        days = days[:limit]
    input_root = Path(input_root)
    input_root.mkdir(parents=True, exist_ok=True)
    results = []
    experiments = []
    previous_symbols: list[str] = []
    flags = dict(env_flags or {})
    flags.setdefault("RANK_BY_FUSION", "0")
    for day in days:
        xsec_rows = _read_parquet_rows(Path(xsec_dir) / f"{day}.parquet")
        fusion_rows = _read_parquet_rows(Path(fusion_dir) / f"{day}.parquet")
        symbols = [str(row.get("canon", row.get("symbol", ""))) for row in xsec_rows]
        forward_returns = load_h5i_forward_returns(
            bar_db,
            trade_day=day,
            symbols=symbols,
            horizons=horizons,
        )
        benchmark = benchmark_from_forward_returns(
            forward_returns,
            horizons=horizons,
        )
        payload = build_normalized_payload(
            trade_day=day,
            xsec_rows=xsec_rows,
            fusion_rows=fusion_rows,
            forward_returns=forward_returns,
            selector_weights=_selector_weights(day),
            factor_weights=factor_weights,
            factor_directions=factor_directions,
            previous_symbols=previous_symbols,
            strict_fusion=False,
            benchmark=benchmark,
        )
        input_path = _write_input(input_root / f"{day}.json", payload)
        output = run_shadow_file(
            input_path,
            cache_root,
            code_sha=code_sha,
            factor_version=factor_version,
            direction_version=direction_version,
            weight_version=weight_version,
            selector_variant=selector_variant,
            env_flags=flags,
            top_n=top_n,
            forward_horizons=tuple(horizons),
            cost_bps=cost_bps,
        )
        result = output["result"]
        results.append(
            {
                "trade_day": day,
                "result": result,
                "coverage": payload["coverage"],
            }
        )
        experiments.append(
            {
                "trade_day": day,
                "experiment_hash": output["experiment_hash"],
                "input_path": str(input_path),
                "manifest_path": str(output["manifest_path"]),
                "result_path": str(output["result_path"]),
            }
        )
        previous_symbols = list(result["arms"]["control_prod"]["symbols"])

    report = aggregate_shadow_results(results, horizons=horizons)
    report["source"] = {
        "xsec_dir": str(Path(xsec_dir)),
        "fusion_dir": str(Path(fusion_dir)),
        "bar_db": str(Path(bar_db)),
        "factor_config": str(Path(factor_config)),
        "turnover_basis": "previous shadow control_prod top_n",
        "cost_bps": float(cost_bps),
        "cost_model": "turnover proxy * cost_bps / 10000; sensitivity only",
    }
    report["experiments"] = experiments
    report_path = _atomic_write_json(Path(report_path), report, "shadow-report.")
    report["report_path"] = str(report_path)
    return report


def _parse_env_flags(values: Sequence[str]) -> dict[str, str]:
    flags = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--env-flag must use NAME=VALUE: {value}")
        key, flag_value = value.split("=", 1)
        if not key:
            raise ValueError(f"--env-flag name cannot be empty: {value}")
        flags[key] = flag_value
    return flags


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xsec-dir", type=Path, required=True)
    parser.add_argument("--fusion-dir", type=Path, required=True)
    parser.add_argument("--bar-db", type=Path, required=True)
    parser.add_argument("--factor-config", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--report", dest="report_path", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--factor-version", required=True)
    parser.add_argument("--direction-version", required=True)
    parser.add_argument("--weight-version", required=True)
    parser.add_argument("--selector-variant", default="production-default")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--horizon", dest="horizons", type=int, action="append")
    parser.add_argument("--env-flag", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        report = run_batch(
            xsec_dir=args.xsec_dir,
            fusion_dir=args.fusion_dir,
            bar_db=args.bar_db,
            factor_config=args.factor_config,
            input_root=args.input_root,
            cache_root=args.cache_root,
            report_path=args.report_path,
            code_sha=args.code_sha,
            factor_version=args.factor_version,
            direction_version=args.direction_version,
            weight_version=args.weight_version,
            selector_variant=args.selector_variant,
            top_n=args.top_n,
            horizons=tuple(args.horizons or (1, 5, 10, 20, 60, 120)),
            limit=args.limit,
            env_flags=_parse_env_flags(args.env_flag),
            cost_bps=args.cost_bps,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"n_days": report["n_days"], "report_path": report["report_path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
