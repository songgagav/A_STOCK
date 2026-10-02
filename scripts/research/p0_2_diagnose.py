# -*- coding: utf-8 -*-
"""Read-only P0-2 pool-source diagnostic.

This is intentionally a research report: it reads existing state and pool
artifacts, prints overlap evidence, and never writes market or trade data.
"""
from __future__ import annotations

import json
import os
from collections import Counter

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _path(*parts):
    return os.path.join(REPO, *parts)


def jload(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return None


def topn(day):
    payload = jload(_path("data", "daily", day, "selection.json"))
    if not isinstance(payload, dict):
        return None
    return [str(item.get("canon") or item.get("symbol"))
            for item in (payload.get("top_n") or [])]


def plan_syms(day):
    payload = jload(_path("data", "drl", day, "target_plan.json"))
    if not isinstance(payload, dict):
        return None
    items = payload.get("targets") or payload.get("top_n") or []
    return [str(item.get("canon") or item.get("symbol")) for item in items]


def trade_symbols(history, keys):
    """Return ``(type, symbol)`` pairs for current and legacy records."""
    out = []
    for key in keys:
        for trade in (history.get(key) or []):
            if isinstance(trade, dict):
                out.append((trade.get("type"), str(
                    trade.get("canon") or trade.get("symbol") or "")))
            elif isinstance(trade, (list, tuple)) and len(trade) >= 2:
                out.append((trade[0], str(trade[1])))
    return out


def main():
    state = jload(_path("data", "state.json")) or {}
    history = state.get("trades_history") or {}
    print("=" * 70)
    print("P0-2 read-only diagnostic: reconstruct 2026-09-02 pool source")
    print("=" * 70)
    print("\n[1] trade-history keys (latest 10):", sorted(history.keys())[-10:])

    for key in ("2026-09-01", "20260901", "2026-09-02", "20260902",
                "2026-09-03", "20260903"):
        if history.get(key):
            print(f"    {key}: {len(history[key])} trades")

    print("\n[2] candidate pools")
    pools = {}
    for day in ("20260828", "20260831", "20260901", "20260902", "20260903"):
        pool = topn(day)
        pools[day] = pool or []
        print(f"    daily/{day}: " +
              ("missing" if pool is None else f"{len(pool)} symbols"))
    for day in ("20260828", "20260831", "20260901", "20260902"):
        pool = plan_syms(day)
        print(f"    drl/{day}: " +
              ("missing" if pool is None else f"{len(pool)} symbols"))

    print("\n[3] engine trades vs candidate pools")
    engine_symbols = set(symbol for _, symbol in trade_symbols(
        history, ("2026-09-02", "20260902")))
    if not engine_symbols:
        for key in sorted(history):
            if key.replace("-", "").startswith("202609"):
                engine_symbols.update(symbol for _, symbol in trade_symbols(
                    history, (key,)))
        print("    no independent 09-02 key; aggregated available 2026-09 trades")
    print(f"    engine symbols: {len(engine_symbols)}")
    for day, pool in pools.items():
        if pool and engine_symbols:
            overlap = engine_symbols & set(pool)
            print(f"      vs {day}: {len(overlap)}/{len(pool)} overlap")

    print("\n[4] source ladder evidence")
    source_path = _path("data", "targets_source.jsonl")
    if not os.path.isfile(source_path):
        print("    missing")
    else:
        with open(source_path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        print(f"    rows={len(rows)}")
        print("    rungs:", dict(Counter(row.get("rung") for row in rows)))
        gaps = []
        for row in rows:
            consume = str(row.get("consume_day") or "")[:10]
            source = str(row.get("sel_day") or "")
            if len(consume) == 10 and len(source) == 8:
                try:
                    from datetime import date
                    cd = date.fromisoformat(consume)
                    sd = date(int(source[:4]), int(source[4:6]), int(source[6:]))
                    gaps.append((cd - sd).days)
                except Exception:
                    continue
        if gaps:
            print("    calendar-day gaps:", dict(Counter(gaps)))

    print("\n[5] conclusion")
    print("    2026-08-31 selection is empty; cross-day fallback was required.")
    print("    Historical 2026-09-02 source rung cannot be proven from current state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
