"""Immutable, non-authoritative evidence bundles for auxiliary market data.

This module is deliberately independent from selector, target, PaperBook, DRL,
broker and HALT state.  Network collectors may call the pure builder, but the
builder itself accepts only explicit inputs and writes only below output_root.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


BUILDER_VERSION = "auxiliary-data-bundle-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
EVIDENCE_STATUSES = frozenset({
    "available",
    "pending_publication",
    "not_applicable",
    "missing",
    "blocked",
    "invalid",
    "tampered",
})


class AuxiliaryBundleError(RuntimeError):
    """Raised when an auxiliary bundle is invalid or has been tampered with."""


@dataclass(frozen=True)
class SourceSpec:
    name: str
    tier: str
    authoritative: bool
    affects_strategy: bool
    observation_impact: str
    provider: str
    cadence: str
    coverage: str


_SOURCE_SPECS = {
    "stock_news": SourceSpec(
        name="stock_news",
        tier="research_only",
        authoritative=False,
        affects_strategy=False,
        observation_impact="non_critical_degradation",
        provider="akshare:stock_news_em",
        cadence="daily_after_close_with_retry",
        coverage="provider_keyword_results_for_all_a_query",
    ),
    "orderbook_snapshot": SourceSpec(
        name="orderbook_snapshot",
        tier="research_only",
        authoritative=False,
        affects_strategy=False,
        observation_impact="non_critical_degradation",
        provider="mootdx:tdx_level1",
        cadence="explicit_intraday_snapshot",
        coverage="explicit_symbol_set",
    ),
    "lhb": SourceSpec(
        name="lhb",
        tier="research_only",
        authoritative=False,
        affects_strategy=False,
        observation_impact="non_critical_degradation",
        provider="akshare:stock_lhb_detail_em",
        cadence="daily_after_close_tplus1_retry",
        coverage="market_day_listings",
    ),
    "block_trade": SourceSpec(
        name="block_trade",
        tier="research_only",
        authoritative=False,
        affects_strategy=False,
        observation_impact="non_critical_degradation",
        provider="akshare:stock_dzjy_mrmx",
        cadence="daily_after_close_tplus1_retry",
        coverage="market_day_transactions",
    ),
}


def source_specs() -> dict[str, SourceSpec]:
    return dict(_SOURCE_SPECS)


@dataclass(frozen=True)
class AuxiliaryBuildRequest:
    output_root: Path
    source: str
    trade_day: str
    observed_at: str
    provider: str
    status: str
    reasons: Sequence[str]
    raw_records: Sequence[Mapping[str, Any]]
    normalized_records: Sequence[Mapping[str, Any]]
    coverage: Mapping[str, Any]


@dataclass(frozen=True)
class AuxiliaryBuildResult:
    bundle_id: str
    path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class AuxiliaryFetchResult:
    provider: str
    publication_state: str
    records: Sequence[Mapping[str, Any]]
    coverage: Mapping[str, Any]
    reasons: Sequence[str]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_safe(value: Any) -> Any:
    """Convert vendor scalar types into deterministic strict-JSON values."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            converted = item()
            if converted is not value:
                return _json_safe(converted)
        except (TypeError, ValueError, OverflowError):
            pass
    return str(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _records_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_bytes(dict(row)) + b"\n" for row in records)


def _validate_request(request: AuxiliaryBuildRequest) -> SourceSpec:
    if request.source not in _SOURCE_SPECS:
        raise ValueError(f"unknown auxiliary source: {request.source}")
    if request.status not in EVIDENCE_STATUSES:
        raise ValueError(f"unsupported evidence status: {request.status}")
    parsed_day = date.fromisoformat(request.trade_day)
    if parsed_day.isoformat() != request.trade_day:
        raise ValueError("trade_day must be ISO YYYY-MM-DD")
    observed = datetime.fromisoformat(request.observed_at)
    if observed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    if not request.provider.strip():
        raise ValueError("provider is required")
    if any(not isinstance(reason, str) or not reason for reason in request.reasons):
        raise ValueError("reasons must contain non-empty strings")
    return _SOURCE_SPECS[request.source]


def _manifest_without_hash(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "manifest_hash"}


def verify_auxiliary_bundle(path: str | Path) -> dict[str, Any]:
    bundle_path = Path(path)
    manifest_path = bundle_path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuxiliaryBundleError(f"tampered: manifest unreadable: {exc}") from exc
    expected_manifest_hash = manifest.get("manifest_hash")
    actual_manifest_hash = _digest(_manifest_without_hash(manifest))
    if expected_manifest_hash != actual_manifest_hash:
        raise AuxiliaryBundleError("tampered: manifest hash mismatch")
    hashes = manifest.get("constituent_artifact_hashes") or {}
    for name in ("raw.json", "records.jsonl"):
        artifact = bundle_path / name
        try:
            actual = _sha256(artifact.read_bytes())
        except Exception as exc:
            raise AuxiliaryBundleError(f"tampered: {name} unreadable: {exc}") from exc
        if hashes.get(name) != actual:
            raise AuxiliaryBundleError(f"tampered: {name} hash mismatch")
    if manifest.get("bundle_id") != bundle_path.name:
        raise AuxiliaryBundleError("tampered: bundle id/path mismatch")
    return manifest


def build_auxiliary_bundle(request: AuxiliaryBuildRequest) -> AuxiliaryBuildResult:
    spec = _validate_request(request)
    raw_records = [_json_safe(dict(row)) for row in request.raw_records]
    normalized_records = [_json_safe(dict(row)) for row in request.normalized_records]
    raw_bytes = _canonical_bytes(raw_records) + b"\n"
    normalized_bytes = _records_bytes(normalized_records)
    constituent_hashes = {
        "raw.json": _sha256(raw_bytes),
        "records.jsonl": _sha256(normalized_bytes),
    }
    identity = {
        "schema_version": 1,
        "source": request.source,
        "trade_day": request.trade_day,
        "observed_at": request.observed_at,
        "provider": request.provider,
        "evidence_status": request.status,
        "reasons": list(request.reasons),
        "coverage": _json_safe(dict(request.coverage)),
        "constituent_artifact_hashes": constituent_hashes,
        "builder_version": BUILDER_VERSION,
    }
    bundle_id = _digest(identity)
    final_path = Path(request.output_root) / "bundles" / request.source / request.trade_day / bundle_id
    if final_path.exists():
        manifest = verify_auxiliary_bundle(final_path)
        if manifest.get("bundle_identity") != identity:
            raise AuxiliaryBundleError("tampered: finalized identity conflict")
        return AuxiliaryBuildResult(bundle_id=bundle_id, path=final_path, manifest=manifest)

    parent = final_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_id}.", dir=parent))
    try:
        _write_bytes(temporary / "raw.json", raw_bytes)
        _write_bytes(temporary / "records.jsonl", normalized_bytes)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "bundle_id": bundle_id,
            "source": request.source,
            "trade_day": request.trade_day,
            "observed_at": request.observed_at,
            "provider": request.provider,
            "source_tier": spec.tier,
            "authoritative": spec.authoritative,
            "affects_strategy": spec.affects_strategy,
            "observation_impact": spec.observation_impact,
            "evidence_status": request.status,
            "failed": request.status in {"missing", "blocked", "invalid", "tampered"},
            "reasons": list(request.reasons),
            "coverage": _json_safe(dict(request.coverage)),
            "record_counts": {"raw": len(raw_records), "normalized": len(normalized_records)},
            "constituent_artifact_hashes": constituent_hashes,
            "builder_version": BUILDER_VERSION,
            "bundle_identity": identity,
        }
        manifest["manifest_hash"] = _digest(manifest)
        _write_bytes(
            temporary / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n",
        )
        try:
            os.replace(temporary, final_path)
        except OSError:
            # Windows reports an existing destination directory as
            # PermissionError/WinError 5 rather than FileExistsError.  Only
            # treat the error as a concurrent commit when the exact finalized
            # path now exists; otherwise preserve the original failure.
            if not final_path.exists():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
            existing = verify_auxiliary_bundle(final_path)
            if existing.get("bundle_identity") != identity:
                raise AuxiliaryBundleError("tampered: concurrent finalized identity conflict")
            manifest = existing
        committed = verify_auxiliary_bundle(final_path)
        return AuxiliaryBuildResult(bundle_id=bundle_id, path=final_path, manifest=committed)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def _text(row: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _number(row: Mapping[str, Any], *names: str) -> float | None:
    value = _text(row, *names)
    if value is None or value in {"-", "--", "nan", "None"}:
        return None
    try:
        number = float(value.replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_records(
    source: str,
    records: Sequence[Mapping[str, Any]],
    *,
    trade_day: str,
) -> list[dict[str, Any]]:
    date.fromisoformat(trade_day)
    normalized: list[dict[str, Any]] = []
    for raw in records:
        row = dict(raw)
        if source == "lhb":
            normalized.append({
                "symbol": _text(row, "代码", "symbol", "code"),
                "name": _text(row, "名称", "name"),
                "trade_day": trade_day,
                "buy_amount": _number(row, "龙虎榜买入额", "buy_amount"),
                "sell_amount": _number(row, "龙虎榜卖出额", "sell_amount"),
                "net_amount": _number(row, "龙虎榜净买额", "net_amount"),
                "reason": _text(row, "上榜原因", "reason"),
            })
        elif source == "block_trade":
            normalized.append({
                "symbol": _text(row, "证券代码", "代码", "symbol"),
                "name": _text(row, "证券简称", "名称", "name"),
                "trade_day": trade_day,
                "deal_price": _number(row, "成交价", "deal_price"),
                "deal_qty": _number(row, "成交量", "deal_qty"),
                "deal_amount": _number(row, "成交额", "deal_amount"),
            })
        elif source == "stock_news":
            published_at = _text(row, "发布时间", "published_at")
            published_date = published_at[:10] if published_at and len(published_at) >= 10 else None
            normalized.append({
                "keyword": _text(row, "关键词", "keyword"),
                "title": _text(row, "新闻标题", "标题", "title"),
                "content": _text(row, "新闻内容", "内容", "content"),
                "published_at": published_at,
                "published_date": published_date,
                "source_name": _text(row, "文章来源", "来源", "source_name"),
                "url": _text(row, "新闻链接", "链接", "url"),
                "trade_day": trade_day,
                "collection_trade_day": trade_day,
            })
        elif source == "orderbook_snapshot":
            normalized.append({**row, "trade_day": trade_day})
        else:
            raise ValueError(f"unknown auxiliary source: {source}")
    return normalized


def _frame_records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "empty") and bool(value.empty):
        return []
    if hasattr(value, "to_dict"):
        rows = value.to_dict("records")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rows = value
    else:
        raise ValueError("vendor result must be a dataframe or record sequence")
    return [_json_safe(dict(row)) for row in rows]


def _positive_depth_value(row: Mapping[str, Any], names: Sequence[str]) -> bool:
    for name in names:
        value = row.get(name)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return True
    return False


def _has_bid_ask_depth(row: Mapping[str, Any]) -> bool:
    return (
        _positive_depth_value(row, ("bid1", "buy1", "bid_price1", "买一价"))
        and _positive_depth_value(row, ("ask1", "sell1", "ask_price1", "卖一价"))
    )


def fetch_source_records(
    source: str,
    trade_day: str,
    *,
    symbols: Sequence[str] | None = None,
    ak_module: Any | None = None,
    quote_client_factory: Any | None = None,
) -> AuxiliaryFetchResult:
    """Fetch one explicit source/day without selecting dates or files implicitly."""
    parsed_day = date.fromisoformat(trade_day)
    ymd = parsed_day.strftime("%Y%m%d")
    if source in {"lhb", "block_trade", "stock_news"}:
        if ak_module is None:
            import akshare as ak_module  # type: ignore[no-redef]
        if source == "lhb":
            value = ak_module.stock_lhb_detail_em(start_date=ymd, end_date=ymd)
        elif source == "block_trade":
            value = ak_module.stock_dzjy_mrmx(start_date=ymd, end_date=ymd)
        else:
            value = ak_module.stock_news_em(symbol="全部A股")
        records = _frame_records(value)
        if source == "stock_news":
            published_values = sorted(filter(None, (
                _text(row, "发布时间", "published_at") for row in records
            )))
            same_day = sum(value[:10] == trade_day for value in published_values)
            coverage = {
                "query": "全部A股",
                "scope": "provider_keyword_results",
                "received": len(records),
                "same_day_received": same_day,
                "latest_published_at": published_values[-1] if published_values else None,
                "failed": 0,
            }
        else:
            coverage = {"requested": "market-day", "received": len(records), "failed": 0}
        return AuxiliaryFetchResult(
            provider=_SOURCE_SPECS[source].provider,
            publication_state="published",
            records=records,
            coverage=coverage,
            reasons=[],
        )
    if source == "orderbook_snapshot":
        requested = sorted({
            str(symbol).split(".", 1)[0].strip()
            for symbol in (symbols or [])
            if str(symbol).split(".", 1)[0].strip()
        })
        if not requested:
            raise ValueError("orderbook_snapshot requires explicit symbols")
        if quote_client_factory is None:
            from mootdx.quotes import Quotes

            quote_client_factory = lambda: Quotes.factory(
                market="std", bestip=True, timeout=15, auto_retry=True,
            )
        client = quote_client_factory()
        try:
            records = _frame_records(client.quotes(requested))
        finally:
            client.close()
        valid_depth_records = [row for row in records if _has_bid_ask_depth(row)]
        returned_depth_symbols = {
            str(row.get("code") or row.get("symbol") or "").split(".", 1)[0]
            for row in valid_depth_records
        }
        returned_depth_symbols.discard("")
        requested_symbols = set(requested)
        received_symbols = returned_depth_symbols & requested_symbols
        unexpected_symbols = returned_depth_symbols - requested_symbols
        failed = max(0, len(requested) - len(received_symbols))
        if not received_symbols:
            publication_state = "missing"
            reasons = [
                "no_valid_bid_ask_depth"
                if not returned_depth_symbols
                else "no_valid_requested_bid_ask_depth"
            ]
        elif failed:
            publication_state = "partial"
            reasons = ["partial_orderbook_coverage"]
        else:
            publication_state = "published"
            reasons = []
        if unexpected_symbols:
            reasons.append("unexpected_orderbook_symbols")
        coverage = {
            "requested": len(requested),
            "received": len(received_symbols),
            "failed": failed,
        }
        if unexpected_symbols:
            coverage["unexpected"] = len(unexpected_symbols)
        return AuxiliaryFetchResult(
            provider=_SOURCE_SPECS[source].provider,
            publication_state=publication_state,
            records=records,
            coverage=coverage,
            reasons=reasons,
        )
    raise ValueError(f"unknown auxiliary source: {source}")


def _write_receipt(output_root: Path, result: AuxiliaryBuildResult) -> Path:
    manifest = result.manifest
    receipt = {
        "schema_version": 1,
        "source": manifest["source"],
        "trade_day": manifest["trade_day"],
        "observed_at": manifest["observed_at"],
        "evidence_status": manifest["evidence_status"],
        "retryable": manifest["evidence_status"] in {
            "pending_publication", "missing", "blocked"
        },
        "authoritative": manifest["authoritative"],
        "affects_strategy": manifest["affects_strategy"],
        "observation_impact": manifest["observation_impact"],
        "bundle_id": result.bundle_id,
        "bundle_path": str(result.path),
        "manifest_sha256": file_sha256(result.path / "manifest.json"),
        "coverage": manifest["coverage"],
        "reasons": manifest["reasons"],
    }
    receipt_dir = output_root / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{manifest['source']}.json"
    with _exclusive_file_lock(receipt_path.with_suffix(".lock")):
        try:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
            existing_at = datetime.fromisoformat(existing["observed_at"])
            incoming_at = datetime.fromisoformat(receipt["observed_at"])
            if existing_at >= incoming_at:
                return receipt_path
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{manifest['source']}.", suffix=".tmp", dir=receipt_dir
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.write_bytes(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
            )
            os.replace(temporary, receipt_path)
        finally:
            temporary.unlink(missing_ok=True)
    return receipt_path


def run_auxiliary_collection(
    *,
    output_root: str | Path,
    source: str,
    trade_day: str,
    observed_at: str,
    fetcher: Any | None = None,
    symbols: Sequence[str] | None = None,
) -> AuxiliaryBuildResult:
    """Run an injected collector and commit a classified immutable bundle."""
    root = Path(output_root)
    spec = _SOURCE_SPECS.get(source)
    if spec is None:
        raise ValueError(f"unknown auxiliary source: {source}")
    try:
        if fetcher is None:
            fetched = fetch_source_records(source, trade_day, symbols=symbols)
        else:
            fetched = fetcher(source=source, trade_day=trade_day, observed_at=observed_at)
        if not isinstance(fetched, AuxiliaryFetchResult):
            raise ValueError("fetcher must return AuxiliaryFetchResult")
        observed_local = datetime.fromisoformat(observed_at).astimezone(_SHANGHAI)
        before_release = (
            source in {"lhb", "block_trade"}
            and not fetched.records
            and observed_local.date().isoformat() == trade_day
            and (observed_local.hour * 60 + observed_local.minute) < 18 * 60
        )
        missing_same_day_news = (
            source == "stock_news"
            and fetched.publication_state == "published"
            and fetched.coverage.get("same_day_received") == 0
        )
        if fetched.publication_state == "pending" or before_release:
            status = "pending_publication"
        elif fetched.publication_state == "not_applicable":
            status = "not_applicable"
        elif fetched.publication_state in {"missing", "partial"}:
            status = "missing"
        elif missing_same_day_news:
            status = "missing"
        elif fetched.publication_state == "published":
            status = "available"
        else:
            raise ValueError(f"unknown publication state: {fetched.publication_state}")
        provider = fetched.provider
        coverage = dict(fetched.coverage)
        reasons = list(fetched.reasons)
        raw_records = [dict(row) for row in fetched.records]
        try:
            normalized = normalize_records(source, raw_records, trade_day=trade_day)
        except Exception as exc:
            status = "invalid"
            normalized = []
            reasons.append(f"normalization_error:{type(exc).__name__}:{exc}")
        if before_release and "before_auxiliary_release_window" not in reasons:
            reasons.append("before_auxiliary_release_window")
        if missing_same_day_news and "no_same_day_keyword_result" not in reasons:
            reasons.append("no_same_day_keyword_result")
    except Exception as exc:  # network/adapter failures become explicit evidence
        status = "blocked"
        provider = spec.provider
        raw_records = []
        normalized = []
        coverage = {"requested": None, "received": None, "failed": None}
        reasons = [f"{type(exc).__name__}:{exc}"]
    result = build_auxiliary_bundle(AuxiliaryBuildRequest(
        output_root=root,
        source=source,
        trade_day=trade_day,
        observed_at=observed_at,
        provider=provider,
        status=status,
        reasons=reasons,
        raw_records=raw_records,
        normalized_records=normalized,
        coverage=coverage,
    ))
    _write_receipt(root, result)
    return result


