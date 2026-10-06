"""Run one auditable Alpha shadow comparison from a normalized PIT artifact.

The input is deliberately explicit JSON. This runner does not import the
production selector, enable ``RANK_BY_FUSION``, read PaperBook state, or write
any production decision file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from alpha_shadow import (  # noqa: E402
    build_experiment_manifest,
    evaluate_shadow_arms,
    write_manifest,
    write_result,
)


DEFAULT_HORIZONS = (1, 5, 10, 20, 60, 120)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read normalized shadow input: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("normalized shadow input must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported normalized shadow input schema_version")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("normalized shadow input must contain a rows list")
    if not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("every normalized shadow row must be an object")
    previous_symbols = payload.get("previous_symbols", [])
    if not isinstance(previous_symbols, list):
        raise ValueError("previous_symbols must be a list")
    return payload


def _validate_env_flags(env_flags: Mapping[str, Any] | None) -> dict[str, str]:
    if env_flags is None:
        return {}
    if not isinstance(env_flags, Mapping):
        raise ValueError("env_flags must be an object")
    return {str(key): str(value) for key, value in env_flags.items()}


def run_shadow_file(
    input_path: str | Path,
    cache_root: str | Path,
    *,
    code_sha: str,
    factor_version: str,
    direction_version: str,
    weight_version: str,
    selector_variant: str,
    env_flags: Mapping[str, Any] | None = None,
    top_n: int = 10,
    forward_horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> dict[str, Any]:
    """Evaluate one normalized artifact and atomically cache its evidence."""

    input_path = Path(input_path)
    payload = _load_payload(input_path)
    rows = payload["rows"]
    input_hashes = payload.get("input_hashes", {})
    if not isinstance(input_hashes, Mapping):
        raise ValueError("input_hashes must be an object")
    hashes = {str(key): str(value) for key, value in input_hashes.items()}
    hashes["normalized_input"] = _sha256_file(input_path)

    manifest = build_experiment_manifest(
        code_sha=code_sha,
        input_hashes=hashes,
        universe_symbols=[str(row.get("symbol") or "") for row in rows],
        factor_version=factor_version,
        direction_version=direction_version,
        weight_version=weight_version,
        selector_variant=selector_variant,
        env_flags=_validate_env_flags(env_flags),
    )
    result = evaluate_shadow_arms(
        rows,
        top_n=top_n,
        previous_symbols=payload["previous_symbols"],
        forward_horizons=forward_horizons,
    )
    result["trade_day"] = payload.get("trade_day")
    result["experiment_hash"] = manifest["experiment_hash"]
    result["input_hashes"] = hashes

    manifest_path = write_manifest(cache_root, manifest)
    result_path = write_result(cache_root, manifest, result)
    return {
        "trade_day": payload.get("trade_day"),
        "experiment_hash": manifest["experiment_hash"],
        "manifest_path": manifest_path,
        "result_path": result_path,
        "result": result,
    }


def _parse_env_flags(values: Sequence[str]) -> dict[str, str]:
    flags: dict[str, str] = {}
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=ROOT / "data" / "shadow_alpha")
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--factor-version", required=True)
    parser.add_argument("--direction-version", required=True)
    parser.add_argument("--weight-version", required=True)
    parser.add_argument("--selector-variant", required=True)
    parser.add_argument("--env-flag", action="append", default=[])
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument(
        "--horizon",
        dest="horizons",
        type=int,
        action="append",
        help="前向收益 horizon；可重复指定，默认 1/5/10/20/60/120",
    )
    args = parser.parse_args(argv)
    try:
        output = run_shadow_file(
            args.input,
            args.cache_root,
            code_sha=args.code_sha,
            factor_version=args.factor_version,
            direction_version=args.direction_version,
            weight_version=args.weight_version,
            selector_variant=args.selector_variant,
            env_flags=_parse_env_flags(args.env_flag),
            top_n=args.top_n,
            forward_horizons=tuple(args.horizons or DEFAULT_HORIZONS),
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "trade_day": output["trade_day"],
                "experiment_hash": output["experiment_hash"],
                **{
                    key: str(value)
                    for key, value in output.items()
                    if key.endswith("_path")
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
