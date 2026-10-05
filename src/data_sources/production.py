"""Opt-in production orchestration for network source adapters.

The default mode is ``shadow`` and no daemon call is active unless
``DATA_SOURCE_ROUTER_ENABLED=1``.  Shadow mode fetches, normalizes, quality
checks, and stages data without writing h5i.  Enforce mode additionally
requires ``DATA_SOURCE_ROUTER_CONFIRM=I_UNDERSTAND`` and uses the existing
guarded h5i sink/probe.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
from pathlib import Path
from typing import Any

from .batch_hash import content_hash
from .commit import commit_staged
from .h5i import H5ICommitSink, H5IContentProbe
from .metadata import QualityStatus, SourceTier, build_metadata
from .network import OptionalDependencyError, build_network_adapters
from .normalize import normalize_source_batch
from .quality import check_quality
from .staging import stage_batch


SOURCE_TIERS = {
    "baostock": SourceTier.BACKUP,
    "mootdx": SourceTier.SHADOW,
    "zzshare": SourceTier.SHADOW,
}
_ENFORCE_CONFIRM = "I_UNDERSTAND"


def _load_symbols(root: str | Path) -> list[str]:
    inline = os.environ.get("DATA_SOURCE_ROUTER_SYMBOLS", "")
    if inline.strip():
        return sorted({item.strip() for item in inline.split(",") if item.strip()})
    configured = os.environ.get("DATA_SOURCE_ROUTER_SYMBOLS_FILE")
    if not configured:
        return []
    path = Path(configured)
    if not path.is_absolute():
        path = Path(root).resolve() / path
    if not path.exists():
        raise FileNotFoundError(f"source-router symbols file not found: {path}")
    return sorted({line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()})


def _source_names(source_names: Iterable[str] | None) -> list[str]:
    if source_names is not None:
        return [str(name).strip() for name in source_names if str(name).strip()]
    configured = os.environ.get("DATA_SOURCE_ROUTER_SOURCES", "baostock,mootdx,zzshare")
    return [item.strip() for item in configured.split(",") if item.strip()]


def run_daily_source_router(
    root: str | Path,
    trade_day: str,
    *,
    symbols: list[str] | None = None,
    source_names: Iterable[str] | None = None,
    mode: str | None = None,
    adapters: Mapping[str, Any] | None = None,
    sink: Any | None = None,
    probe: Any | None = None,
) -> dict[str, Any]:
    """Attempt sources in order and return one auditable non-throwing result."""
    selected_mode = (mode or os.environ.get("DATA_SOURCE_ROUTER_MODE", "shadow")).strip().lower()
    if selected_mode not in {"shadow", "enforce"}:
        return {"status": "blocked", "reason": f"invalid mode: {selected_mode}"}
    if selected_mode == "enforce" and os.environ.get("DATA_SOURCE_ROUTER_CONFIRM") != _ENFORCE_CONFIRM:
        return {"status": "blocked", "reason": "enforce requires explicit confirmation"}

    try:
        requested_symbols = symbols if symbols is not None else _load_symbols(root)
    except Exception as exc:  # noqa: BLE001 - scheduler must report, not crash
        return {"status": "blocked", "reason": str(exc)}
    requested_symbols = sorted({str(item) for item in requested_symbols if str(item).strip()})
    if not requested_symbols:
        return {"status": "blocked", "reason": "no symbols configured for source router"}

    available = dict(adapters or build_network_adapters())
    attempts: list[dict[str, Any]] = []
    for source in _source_names(source_names):
        adapter = available.get(source)
        if adapter is None:
            attempts.append({"source": source, "status": "unavailable", "reason": "adapter not registered"})
            continue
        try:
            raw = adapter.fetch(trade_day, requested_symbols)
            canonical = normalize_source_batch(raw)
            report = check_quality(
                canonical,
                expected_symbols=requested_symbols,
                expected_trade_day=trade_day,
            )
            if report.status is not QualityStatus.PASSED or report.coverage < 0.99:
                reasons = list(report.reasons)
                if report.coverage < 0.99:
                    seen_symbols = {
                        str(row.get("symbol"))
                        for row in canonical
                        if row.get("symbol") is not None
                    }
                    missing_symbols = sorted(set(requested_symbols) - seen_symbols)
                    reasons.append(
                        "coverage_below_expected:"
                        f"{report.coverage:.6f};missing_symbols={','.join(missing_symbols)}"
                    )
                attempts.append({
                    "source": source,
                    "status": "rejected",
                    "coverage": report.coverage,
                    "reasons": reasons,
                })
                continue
            metadata = build_metadata(
                source=source,
                source_tier=SOURCE_TIERS.get(source, SourceTier.SHADOW),
                trade_day=trade_day,
                coverage=report.coverage,
                retrieved_at=raw.retrieved_at,
                quality=report.status,
                input_hash=content_hash(canonical),
                execution_allowed=False,
            )
            staged = stage_batch(root, metadata, canonical)
            if selected_mode == "shadow":
                return {
                    "status": "shadow_staged",
                    "source": source,
                    "batch_id": staged["batch_id"],
                    "coverage": report.coverage,
                    "attempts": attempts,
                }
            result = commit_staged(
                root,
                staged["batch_id"],
                sink or H5ICommitSink(),
                probe or H5IContentProbe(),
            )
            return {"status": result["status"], "source": source, "attempts": attempts, **result}
        except OptionalDependencyError as exc:
            attempts.append({"source": source, "status": "unavailable", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - try the next source, preserve reason
            attempts.append({"source": source, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"})

    return {"status": "blocked", "reason": "all configured sources failed", "attempts": attempts}


__all__ = ["run_daily_source_router", "SOURCE_TIERS"]
