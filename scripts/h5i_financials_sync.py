#!/usr/bin/env python3
"""Append a newly published bulk financial report to H5i with PIT evidence.

This is a data-ingestion utility only. It never recalculates targets, updates
PaperBook, or changes runtime configuration. The append is withheld unless the
provider returned the expected report period for the complete H5i financial
universe, because H5i financials are append-only by report date.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "h5i" / "market.db"
OUTPUT_ROOT = ROOT / "data" / "financials_sync"
SOURCE = "ak_stock_yjbb_em"
THS_SOURCE = "ak_stock_financial_abstract_ths"
SCHEMA_VERSION = 1
BUILDER_VERSION = "2.0"

FIN_MAP = {
    "营业总收入": "revenue",
    "营业总收入同比增长率": "rev_yoy",
    "净利润": "net_profit",
    "净利润同比增长率": "np_yoy",
    "扣非净利润": "ded_np",
    "扣非净利润同比增长率": "ded_np_yoy",
    "基本每股收益": "eps",
    "每股净资产": "bvps",
    "每股资本公积金": "capg_ps",
    "每股未分配利润": "und_ps",
    "每股经营现金流": "ocf_ps",
    "销售净利率": "net_margin",
    "净资产收益率": "roe",
    "净资产收益率-摊薄": "roe_diluted",
    "资产负债率": "debt_ratio",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
    "保守速动比率": "cons_quick",
    "产权比率": "debt_eq",
    "营业周期": "op_cycle",
    "应收账款周转天数": "ar_days",
    "销售毛利率": "gross_margin",
    "存货周转率": "inv_turn",
    "存货周转天数": "inv_days",
}
H5I_COLUMNS = ["ts", "symbol", *FIN_MAP.values(), "source"]
BULK_COLUMN_MAP = {
    "营业总收入-营业总收入": "revenue",
    "营业总收入-同比增长": "rev_yoy",
    "净利润-净利润": "net_profit",
    "净利润-同比增长": "np_yoy",
    "每股收益": "eps",
    "每股净资产": "bvps",
    "每股经营现金流量": "ocf_ps",
    "净资产收益率": "roe",
    "销售毛利率": "gross_margin",
}


def availability_date(report_period: str | date) -> date:
    """Match the existing selector PIT report-release calendar."""
    period = pd.Timestamp(report_period).date()
    if period.month == 12:
        return date(period.year + 1, 4, 30)
    if period.month == 3:
        return date(period.year, 4, 30)
    if period.month == 6:
        return date(period.year, 8, 31)
    if period.month == 9:
        return date(period.year, 10, 31)
    return date(period.year, 12, 31)


def expected_report_period(as_of: str | date) -> str:
    """Latest standard quarter whose PIT availability date has passed."""
    day = pd.Timestamp(as_of).date()
    candidates = []
    for year in range(day.year - 2, day.year + 1):
        for month, last_day in ((12, 31), (3, 31), (6, 30), (9, 30)):
            period = date(year, month, last_day)
            if availability_date(period) <= day:
                candidates.append(period)
    if not candidates:
        raise ValueError(f"cannot derive an expected financial period for {day}")
    return max(candidates).isoformat()


def _number(value):
    if value is None or pd.isna(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "False", "True", "None", "nan", "NaN"}:
        return None
    if text.endswith("%"):
        try:
            return round(float(text[:-1]) / 100.0, 8)
        except ValueError:
            return None
    multiplier = 1.0
    if text.endswith("万亿"):
        multiplier, text = 1e12, text[:-2]
    elif text.endswith("亿"):
        multiplier, text = 1e8, text[:-1]
    elif text.endswith("万"):
        multiplier, text = 1e4, text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def normalize_provider_frame(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize one THS history response into the current H5i schema."""
    if frame is None or frame.empty or "报告期" not in frame.columns:
        return pd.DataFrame(columns=H5I_COLUMNS)
    work = pd.DataFrame()
    work["ts"] = pd.to_datetime(frame["报告期"], format="%Y-%m-%d", errors="coerce")
    work["symbol"] = str(symbol).split(".")[0].zfill(6)
    for source_name, h5i_name in FIN_MAP.items():
        if source_name in frame.columns:
            work[h5i_name] = frame[source_name].map(_number)
        else:
            work[h5i_name] = None
    work["source"] = THS_SOURCE
    work = work.dropna(subset=["ts"])
    work = work[H5I_COLUMNS]
    work["ts"] = pd.to_datetime(work["ts"]).astype("datetime64[us]")
    for column in FIN_MAP.values():
        work[column] = pd.to_numeric(work[column], errors="coerce").astype("float64")
    return work.sort_values("ts").reset_index(drop=True)


def _bulk_ratio(value):
    if isinstance(value, str) and value.strip().endswith("%"):
        return _number(value)
    number = _number(value)
    return number / 100.0 if number is not None else None


def _provider_symbol(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and float(value).is_integer():
        digits = str(int(value))
    else:
        text = str(value).strip()
        numeric = re.fullmatch(r"(\d{1,6})\.0+", text)
        digits = numeric.group(1) if numeric else re.sub(r"\D", "", text)
    return digits.zfill(6) if 1 <= len(digits) <= 6 else None


def normalize_em_bulk_frame(
    frame: pd.DataFrame,
    *,
    report_period: str,
    as_of: str,
) -> pd.DataFrame:
    """Normalize Eastmoney's report-level table, excluding post-cutoff notices."""
    columns = ["股票代码", "最新公告日期", *BULK_COLUMN_MAP]
    if frame is None or frame.empty or "股票代码" not in frame.columns:
        return pd.DataFrame(columns=H5I_COLUMNS)
    # Announcement date is required to prove PIT availability; do not infer it.
    if "最新公告日期" not in frame.columns:
        return pd.DataFrame(columns=H5I_COLUMNS)
    work = frame.copy()
    work["symbol"] = work["股票代码"].map(_provider_symbol)
    work["ts"] = pd.Timestamp(report_period).normalize()
    announced = pd.to_datetime(work["最新公告日期"], errors="coerce").dt.normalize()
    cutoff = pd.Timestamp(as_of).normalize()
    work = work.loc[work["symbol"].notna() & announced.notna() & (announced <= cutoff)].copy()
    for source_name, h5i_name in BULK_COLUMN_MAP.items():
        if source_name not in work.columns:
            work[h5i_name] = None
        elif h5i_name in {"rev_yoy", "np_yoy", "roe", "gross_margin"}:
            work[h5i_name] = work[source_name].map(_bulk_ratio)
        else:
            work[h5i_name] = work[source_name].map(_number)
    for column in FIN_MAP.values():
        if column not in work.columns:
            work[column] = None
    work["source"] = SOURCE
    work = work[H5I_COLUMNS].drop_duplicates(["ts", "symbol"], keep="last")
    work["ts"] = pd.to_datetime(work["ts"]).astype("datetime64[us]")
    for column in FIN_MAP.values():
        work[column] = pd.to_numeric(work[column], errors="coerce").astype("float64")
    return work.sort_values(["ts", "symbol"]).reset_index(drop=True)


def prepare_bulk_append_batch(
    frame: pd.DataFrame,
    *,
    universe_symbols: list[str],
    existing_symbols: list[str] | None = None,
    max_timestamp: str | date,
    expected_period: str,
    as_of: str,
    minimum_coverage: float = 1.0,
) -> dict:
    """Prepare a PIT-safe append, allowing only explicitly marked high-coverage partials."""
    symbols = sorted({str(symbol).split(".")[0].zfill(6) for symbol in universe_symbols})
    existing = {str(symbol).split(".")[0].zfill(6) for symbol in (existing_symbols or [])}
    existing.intersection_update(symbols)
    normalized = normalize_em_bulk_frame(frame, report_period=expected_period, as_of=as_of)
    normalized = normalized.loc[normalized["symbol"].isin(symbols)]
    duplicated = normalized["symbol"].duplicated(keep=False)
    duplicate_symbols = sorted(normalized.loc[duplicated, "symbol"].unique().tolist())
    if duplicate_symbols:
        normalized = normalized.loc[~normalized["symbol"].isin(duplicate_symbols)]
    reported = set(normalized["symbol"].tolist())
    covered = existing | reported
    missing = sorted(set(symbols) - covered)
    coverage = len(covered) / len(symbols) if symbols else 0.0
    threshold_passed = bool(symbols) and coverage >= minimum_coverage
    status = (
        "ready" if threshold_passed and not missing
        else "partial_pending" if threshold_passed
        else "pending"
    )
    watermark = pd.Timestamp(max_timestamp).normalize()
    expected = pd.Timestamp(expected_period).normalize()
    rows = normalized.loc[
        (normalized["ts"].dt.normalize() == expected)
        & ~normalized["symbol"].isin(existing)
    ].copy().reset_index(drop=True)
    if not threshold_passed:
        rows = pd.DataFrame(columns=H5I_COLUMNS)
    if duplicate_symbols:
        status = "invalid"
        rows = pd.DataFrame(columns=H5I_COLUMNS)
    return {
        "status": status,
        "coverage": coverage,
        "minimum_coverage": minimum_coverage,
        "requested_symbols": len(symbols),
        "reported_symbols": len(covered),
        "already_present_symbols": len(existing),
        "new_rows": len(rows),
        "missing_period_symbols": missing,
        "failed_symbols": duplicate_symbols,
        "rows": rows,
        "raw_rows": frame.copy() if frame is not None else pd.DataFrame(),
        "expected_period": expected.date().isoformat(),
        "watermark_before": watermark.date().isoformat(),
        "universe_sha256": hashlib.sha256("\n".join(symbols).encode("ascii")).hexdigest(),
    }


def prepare_append_batch(
    fetched: dict[str, pd.DataFrame | None],
    *,
    max_timestamp: str | date,
    expected_period: str,
    minimum_coverage: float = 1.0,
) -> dict:
    """Prepare an all-or-nothing append; never append a partial period."""
    watermark = pd.Timestamp(max_timestamp).normalize()
    expected = pd.Timestamp(expected_period).normalize()
    normalized = []
    raw_expected = []
    failed = []
    missing = []
    for symbol in sorted(fetched):
        frame = fetched[symbol]
        if frame is None or frame.empty:
            failed.append(symbol)
            continue
        rows = normalize_provider_frame(symbol, frame)
        exact_raw = frame.loc[
            pd.to_datetime(frame.get("报告期"), errors="coerce").dt.normalize() == expected
        ].copy() if "报告期" in frame.columns else frame.iloc[0:0].copy()
        exact = rows.loc[rows["ts"].dt.normalize() == expected]
        if exact.empty:
            missing.append(symbol)
            continue
        if exact["ts"].duplicated().any():
            failed.append(symbol)
            continue
        exact_raw.insert(0, "symbol", str(symbol).split(".")[0].zfill(6))
        raw_expected.append(exact_raw)
        normalized.append(exact)

    total = len(fetched)
    coverage = len(normalized) / total if total else 0.0
    ready = bool(total) and not failed and not missing and coverage >= minimum_coverage
    rows = (pd.concat(normalized, ignore_index=True).sort_values(["ts", "symbol"])
            if ready else pd.DataFrame(columns=H5I_COLUMNS))
    if not rows.empty:
        rows = rows.loc[rows["ts"] > watermark].reset_index(drop=True)
        duplicate_keys = rows.duplicated(["ts", "symbol"])
        if duplicate_keys.any():
            ready = False
            rows = pd.DataFrame(columns=H5I_COLUMNS)
            failed.append("duplicate_report_period_symbol")
    raw = (pd.concat(raw_expected, ignore_index=True)
           if raw_expected else pd.DataFrame())
    return {
        "status": "ready" if ready else "pending",
        "coverage": coverage,
        "requested_symbols": total,
        "reported_symbols": len(normalized),
        "missing_period_symbols": sorted(missing),
        "failed_symbols": sorted(set(failed)),
        "rows": rows,
        "raw_rows": raw,
        "expected_period": expected.date().isoformat(),
        "watermark_before": watermark.date().isoformat(),
    }


def fetch_provider_frame(symbol: str, retries: int = 3) -> pd.DataFrame | None:
    """Fetch one symbol with bounded retry and no write side effects."""
    import akshare as ak

    for attempt in range(retries):
        try:
            frame = ak.stock_financial_abstract_ths(
                symbol=symbol, indicator="按报告期"
            )
            if frame is not None and not frame.empty:
                return frame
        except Exception:
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    return None


def fetch_bulk_frame(report_period: str) -> pd.DataFrame:
    """Fetch the provider's single whole-market quarterly report response."""
    import akshare as ak

    return ak.stock_yjbb_em(date=pd.Timestamp(report_period).strftime("%Y%m%d"))


def fetch_universe(symbols: list[str], workers: int = 6) -> dict[str, pd.DataFrame | None]:
    fetched = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_provider_frame, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                fetched[symbol] = future.result()
            except Exception:
                fetched[symbol] = None
    return fetched


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _raw_evidence_exists(
    output_root: Path, expected_period: str, raw_rows: pd.DataFrame | None,
) -> bool:
    """Deduplicate raw input only when an exact content-hash match is already stored."""
    if raw_rows is None or raw_rows.empty:
        return False
    raw_json = raw_rows.to_json(orient="records", lines=True, force_ascii=False, date_format="iso")
    raw_bytes = raw_json.encode("utf-8")
    exact_hashes = {
        hashlib.sha256(raw_bytes).hexdigest(),
        # Earlier Windows runs used Path.write_text(), which translated the
        # canonical JSONL newlines to CRLF before hashing the stored artifact.
        hashlib.sha256(raw_bytes.replace(b"\n", b"\r\n")).hexdigest(),
    }
    parent = output_root / expected_period.replace("-", "")
    if not parent.is_dir():
        return False
    for manifest_path in parent.glob("fin-*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_meta = manifest.get("artifacts", {}).get("raw_provider_rows.jsonl", {})
            if int(raw_meta.get("size_bytes", 0)) > 0 and raw_meta.get("sha256") in exact_hashes:
                return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def _persist_run(
    *,
    output_root: Path,
    expected_period: str,
    as_of: str,
    status: str,
    details: dict,
    raw_rows: pd.DataFrame | None = None,
    normalized_rows: pd.DataFrame | None = None,
    store_raw_rows: bool = True,
) -> dict:
    run_id = f"fin-{expected_period.replace('-', '')}-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    parent = output_root / expected_period.replace("-", "")
    parent.mkdir(parents=True, exist_ok=True)
    final_dir = parent / run_id
    temp_dir = parent / f".{run_id}.partial"
    temp_dir.mkdir()
    artifacts = {}
    details = dict(details)
    raw_path = temp_dir / "raw_provider_rows.jsonl"
    raw_json = (
        raw_rows.to_json(orient="records", lines=True, force_ascii=False, date_format="iso")
        if raw_rows is not None and not raw_rows.empty else ""
    )
    if raw_json and store_raw_rows:
        # Keep JSONL bytes platform independent. Text-mode writes translate LF
        # to CRLF on Windows, which makes the manifest hash differ from the
        # canonical content hash used by retry deduplication.
        raw_path.write_bytes(raw_json.encode("utf-8"))
        details["raw_capture_status"] = "captured"
    elif raw_json:
        details["raw_capture_status"] = "fingerprint_only_no_new_append"
        details["raw_input_sha256"] = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        details["raw_input_row_count"] = int(len(raw_rows))
        raw_path.write_text("", encoding="utf-8")
    else:
        details["raw_capture_status"] = "unavailable"
        raw_path.write_text("", encoding="utf-8")
    artifacts[raw_path.name] = {"sha256": _sha256(raw_path), "size_bytes": raw_path.stat().st_size}
    normalized_path = temp_dir / "normalized_h5i_rows.jsonl"
    if normalized_rows is not None and not normalized_rows.empty:
        normalized_path.write_text(
            normalized_rows.to_json(orient="records", lines=True, force_ascii=False, date_format="iso"),
            encoding="utf-8",
        )
    else:
        normalized_path.write_text("", encoding="utf-8")
    artifacts[normalized_path.name] = {"sha256": _sha256(normalized_path), "size_bytes": normalized_path.stat().st_size}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": datetime.now().astimezone().isoformat(),
        "builder_version": BUILDER_VERSION,
        "builder_sha256": _sha256(Path(__file__).resolve()),
        "as_of": as_of,
        "expected_period": expected_period,
        "source": SOURCE,
        "target": "h5i.financials",
        "status": status,
        "artifacts": artifacts,
        **details,
    }
    (temp_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    os.replace(temp_dir, final_dir)
    manifest["bundle_path"] = str(final_dir)
    _atomic_json(output_root / "last_run.json", manifest)
    return manifest


def run_sync(
    *,
    db_path: str | Path,
    as_of: str,
    output_root: str | Path = OUTPUT_ROOT,
    workers: int = 6,
    minimum_coverage: float = 1.0,
    fetcher=fetch_bulk_frame,
) -> dict:
    expected = expected_report_period(as_of)
    db_path = Path(db_path)
    output_root = Path(output_root)
    from h5i_db import Database

    reader = Database(str(db_path), read_only=True)
    try:
        watermark_df = reader.sql("SELECT MAX(CAST(ts AS DATE)) d FROM financials").to_pandas()
        watermark_value = watermark_df.iloc[0, 0]
        universe_meta = reader.sql(
            "SELECT MAX(CAST(ts AS DATE)) AS universe_day FROM daily_bars "
            "WHERE CAST(ts AS DATE)<=DATE '" + pd.Timestamp(as_of).date().isoformat() + "'"
        ).to_pandas()
        universe_day_value = universe_meta.iloc[0, 0]
        if universe_day_value is None:
            symbols = []
            universe_day = None
        else:
            universe_day = pd.Timestamp(universe_day_value).date().isoformat()
            symbols_df = reader.sql(
                "SELECT DISTINCT symbol FROM daily_bars WHERE CAST(ts AS DATE)=DATE '"
                + universe_day + "' ORDER BY symbol"
            ).to_pandas()
            symbols = [str(s).split(".")[0].zfill(6) for s in symbols_df["symbol"].dropna().tolist()]
        existing_df = reader.sql(
            "SELECT DISTINCT symbol FROM financials WHERE CAST(ts AS DATE)=DATE '"
            + expected + "'"
        ).to_pandas()
        existing_symbols = [str(s).split(".")[0].zfill(6) for s in existing_df["symbol"].dropna().tolist()]
    finally:
        reader.close()
    existing_symbols = sorted(set(existing_symbols) & set(symbols))
    watermark = pd.Timestamp(watermark_value).date().isoformat() if watermark_value is not None else "1900-01-01"
    if symbols and set(symbols).issubset(set(existing_symbols)):
        return _persist_run(
            output_root=output_root, expected_period=expected, as_of=as_of,
            status="up_to_date", details={"watermark_before": watermark, "watermark_after": watermark,
                                           "requested_symbols": len(symbols), "appended_rows": 0,
                                           "universe_day": universe_day,
                                           "verified_period_symbols": len(set(symbols) & set(existing_symbols)),
                                           "verified_universe_coverage": 1.0},
        )
    if not symbols:
        return _persist_run(
            output_root=output_root, expected_period=expected, as_of=as_of,
            status="blocked", details={"reason": "h5i_financials_universe_empty", "appended_rows": 0},
        )

    frame = fetcher(expected)
    prepared = prepare_bulk_append_batch(
        frame,
        universe_symbols=symbols,
        existing_symbols=existing_symbols,
        max_timestamp=watermark,
        expected_period=expected,
        as_of=as_of,
        minimum_coverage=minimum_coverage,
    )
    details = {key: prepared[key] for key in (
        "coverage", "requested_symbols", "reported_symbols", "missing_period_symbols",
        "failed_symbols", "watermark_before", "expected_period", "minimum_coverage",
        "universe_sha256", "already_present_symbols",
    )}
    details["universe_day"] = universe_day
    rows = prepared["rows"]
    status = prepared["status"]
    watermark_after = watermark
    append_receipt = None
    if status in {"ready", "partial_pending"} and not rows.empty:
        writer = Database(str(db_path))
        try:
            schema = writer.schema("financials")
            import pyarrow as pa
            table = pa.Table.from_pandas(rows[H5I_COLUMNS], schema=schema, preserve_index=False)
            append_receipt = writer.append("financials", table)
            verify_df = writer.sql(
                "SELECT MAX(CAST(ts AS DATE)) d, COUNT(DISTINCT symbol) n "
                "FROM financials WHERE CAST(ts AS DATE)=DATE '" + expected + "' "
                "AND source='" + SOURCE + "'"
            ).to_pandas()
            verified_period = verify_df.iloc[0, 0]
            verified_symbols = int(verify_df.iloc[0, 1] or 0)
            watermark_after = pd.Timestamp(verified_period).date().isoformat() if verified_period is not None else watermark
            details["verified_period_symbols"] = verified_symbols
            verified_period_df = writer.sql(
                "SELECT DISTINCT symbol FROM financials WHERE CAST(ts AS DATE)=DATE '" + expected + "'"
            ).to_pandas()
            verified_active = {
                str(symbol).split(".")[0].zfill(6)
                for symbol in verified_period_df["symbol"].dropna().tolist()
            } & set(symbols)
            required_coverage = len(verified_active) / len(symbols) if symbols else 0.0
            if watermark_after != expected or not set(existing_symbols).union(set(rows["symbol"])).issubset(verified_active):
                status = "invalid"
                details["verify_error"] = "post_append_period_or_symbol_coverage_mismatch"
            else:
                details["verified_period_symbols"] = len(verified_active)
                details["verified_universe_coverage"] = required_coverage
                status = "available" if not prepared["missing_period_symbols"] else "partial_pending"
        except Exception as exc:
            status = "blocked"
            details["write_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        finally:
            writer.close()
    elif status in {"ready", "partial_pending"}:
        if status == "ready":
            status = "available"
        else:
            details["reason"] = "awaiting_provider_rows_for_missing_symbols"
    details["watermark_after"] = watermark_after
    details["appended_rows"] = int(len(rows)) if append_receipt is not None else 0
    details["append_receipt"] = append_receipt
    normalized_evidence = None
    if watermark_after == expected:
        try:
            evidence_db = Database(str(db_path), read_only=True)
            try:
                normalized_evidence = evidence_db.sql(
                    "SELECT * FROM financials WHERE CAST(ts AS DATE)=DATE '" + expected + "' "
                    "AND source='" + SOURCE + "' ORDER BY symbol"
                ).to_pandas()
            finally:
                evidence_db.close()
        except Exception as exc:
            details["evidence_read_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            status = "blocked"
    store_full_evidence = bool(
        append_receipt is not None
        or not _raw_evidence_exists(output_root, expected, prepared["raw_rows"])
    )
    return _persist_run(
        output_root=output_root, expected_period=expected, as_of=as_of,
        status=status, details=details, raw_rows=prepared["raw_rows"],
        normalized_rows=normalized_evidence if store_full_evidence else None,
        store_raw_rows=store_full_evidence,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="explicit ISO date used for PIT maturity")
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--output-root", default=str(OUTPUT_ROOT))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--minimum-coverage", type=float, default=1.0)
    args = parser.parse_args(argv)
    if not 0 < args.minimum_coverage <= 1:
        parser.error("--minimum-coverage must be in (0, 1]")
    result = run_sync(
        db_path=args.db_path, as_of=args.as_of, output_root=args.output_root,
        workers=args.workers, minimum_coverage=args.minimum_coverage,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2, default=str))
    return 0 if result.get("status") in {"available", "up_to_date", "pending", "partial_pending"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
