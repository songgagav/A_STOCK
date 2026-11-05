"""Explicit-input CLI for auxiliary research-data collection."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from auxiliary_data import fetch_source_records, run_auxiliary_collection


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _validate_live_orderbook_window(
    trade_day: str,
    observed_at: str,
    *,
    now: datetime,
) -> None:
    observed = datetime.fromisoformat(observed_at)
    if observed.tzinfo is None or now.tzinfo is None:
        raise ValueError("live orderbook window requires timezone-aware timestamps")
    observed_local = observed.astimezone(_SHANGHAI)
    now_local = now.astimezone(_SHANGHAI)
    within_window = (
        time(14, 50) <= now_local.time() < time(15, 0)
        and time(14, 50) <= observed_local.time() < time(15, 0)
    )
    same_day = observed_local.date().isoformat() == trade_day == now_local.date().isoformat()
    fresh = abs((now_local - observed_local).total_seconds()) <= 60
    if not (within_window and same_day and fresh):
        raise ValueError(
            "live orderbook window requires same-day fresh 14:50-15:00 Asia/Shanghai capture"
        )


def _symbols_from_file(path: str | None) -> list[str] | None:
    if not path:
        return None
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict) and isinstance(value.get("symbols"), list):
        return [str(item) for item in value["symbols"]]
    raise ValueError("symbols file must be a JSON list or an object with symbols")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        required=True,
        choices=("stock_news", "orderbook_snapshot", "lhb", "block_trade"),
    )
    parser.add_argument("--trade-day", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--symbols-json")
    return parser


def main(argv: Sequence[str] | None = None, *, fetcher: Any | None = None,
         clock: Any | None = None) -> int:
    args = _parser().parse_args(argv)
    symbols = _symbols_from_file(args.symbols_json)

    if args.source == "orderbook_snapshot":
        now = clock() if clock is not None else datetime.now(tz=_SHANGHAI)
        _validate_live_orderbook_window(args.trade_day, args.observed_at, now=now)

    if fetcher is None:
        def fetcher(**kwargs):
            return fetch_source_records(
                kwargs["source"], kwargs["trade_day"], symbols=symbols,
            )

    result = run_auxiliary_collection(
        output_root=args.output_root,
        source=args.source,
        trade_day=args.trade_day,
        observed_at=args.observed_at,
        fetcher=fetcher,
    )
    payload = {
        "bundle_id": result.bundle_id,
        "bundle_path": str(result.path),
        "source": result.manifest["source"],
        "trade_day": result.manifest["trade_day"],
        "evidence_status": result.manifest["evidence_status"],
        "reasons": result.manifest["reasons"],
        "record_counts": result.manifest["record_counts"],
        "coverage": result.manifest["coverage"],
        "authoritative": result.manifest["authoritative"],
        "affects_strategy": result.manifest["affects_strategy"],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["evidence_status"] in {"available", "not_applicable"} else 2


if __name__ == "__main__":
    raise SystemExit(main())

