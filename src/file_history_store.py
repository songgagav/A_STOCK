"""Read-only file-backed history adapter for the ArcticDB migration.

This module deliberately has no production consumers yet.  It establishes the
first replacement contract for ``daily_summary`` using the immutable daily
JSON artifacts already produced by the paper-book pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd


_LOG = logging.getLogger("file_history_store")
_COLUMNS = ["summary_json", "equity"]
_PERF_COLUMNS = [
    "summary_json",
    "total_return",
    "annual_return",
    "max_drawdown",
    "sharpe_annual",
    "calmar",
    "excess_total",
]
_REWARD_COLUMNS = ["day", "model_version", "step", "reward", "weights_json"]


class FileHistoryStore:
    """Read daily history from an injected ``data/daily``-like directory."""

    def __init__(
        self,
        daily_root: str | Path,
        ic_root: str | Path | None = None,
        drl_root: str | Path | None = None,
    ):
        self.daily_root = Path(daily_root)
        self.ic_root = Path(ic_root) if ic_root is not None else self.daily_root.parent / "ic"
        self.drl_root = Path(drl_root) if drl_root is not None else self.daily_root.parent / "drl"

    def read_daily_summaries(self, days: int = 60) -> pd.DataFrame:
        """Return recent summaries with the first migration-compatible schema.

        The result uses a YYYYMMDD string index named ``day`` and the same
        materialized columns as ``ArcticStore.write_daily_summary``:
        ``summary_json`` and ``equity``.  Invalid directory names, missing
        artifacts, malformed JSON, and summaries without an equity value are
        excluded with a warning; callers can inspect logs instead of treating
        a malformed artifact as a valid empty history.
        """
        if days <= 0 or not self.daily_root.is_dir():
            return self._empty()

        rows: list[dict[str, Any]] = []
        for day_dir in sorted(self.daily_root.iterdir()):
            if not day_dir.is_dir() or not self._is_day(day_dir.name):
                continue
            summary_path = day_dir / "daily_summary.json"
            if not summary_path.is_file():
                continue
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                if not isinstance(summary, dict):
                    raise ValueError("summary root is not an object")
                equity = self._equity(summary)
                if equity is None:
                    _LOG.warning("daily summary has no equity: %s", summary_path)
                    continue
                rows.append(
                    {
                        "day": self._day_value(summary.get("day"), day_dir.name),
                        "summary_json": json.dumps(
                            summary, ensure_ascii=False, default=str
                        ),
                        "equity": float(equity),
                    }
                )
            except Exception as exc:  # malformed artifacts remain visible in logs
                _LOG.warning("read daily summary failed: %s: %s", summary_path, exc)

        if not rows:
            return self._empty()
        frame = pd.DataFrame(rows, columns=["day", *_COLUMNS])
        frame = frame.drop_duplicates(subset=["day"], keep="last")
        frame = frame.sort_values("day").tail(days).set_index("day")
        frame.index.name = "day"
        return frame[_COLUMNS]

    def read_trade_records(
        self, days: int = 30, symbol: str | None = None
    ) -> pd.DataFrame:
        """Read normalized paper trades from per-day ``trades.json`` files.

        The adapter deliberately returns a flat, read-only view.  It does not
        infer PnL, rewrite records, or use current market data.  Missing
        optional fields remain null so a future consumer can compare this
        result with ``ArcticStore.read_trades`` before cutover.
        """
        core = ["day", "time", "type", "canon", "qty", "price", "fee", "pnl"]
        if days <= 0 or not self.daily_root.is_dir():
            return pd.DataFrame(columns=core)

        day_dirs = [
            p for p in self.daily_root.iterdir()
            if p.is_dir() and self._is_day(p.name)
        ]
        selected = sorted(day_dirs, key=lambda p: p.name)[-days:]
        rows: list[dict[str, Any]] = []
        for day_dir in selected:
            path = day_dir / "trades.json"
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, list):
                    raise ValueError("trades root is not a list")
                for item in payload:
                    if not isinstance(item, dict):
                        _LOG.warning("skip non-object trade: %s", path)
                        continue
                    canon = item.get("canon") or item.get("symbol")
                    if symbol is not None and canon != symbol:
                        continue
                    row = dict(item)
                    row["day"] = self._iso_day(
                        item.get("date") or item.get("day") or day_dir.name
                    )
                    row["canon"] = canon
                    rows.append(row)
            except Exception as exc:  # malformed artifacts remain visible in logs
                _LOG.warning("read trades failed: %s: %s", path, exc)

        if not rows:
            return pd.DataFrame(columns=core)
        extras = sorted({key for row in rows for key in row if key not in core and key != "date"})
        columns = core + extras
        frame = pd.DataFrame(rows)
        for col in columns:
            if col not in frame:
                frame[col] = None
        sort_columns = [col for col in ("day", "time", "ts") if col in frame]
        if sort_columns:
            frame = frame.sort_values(sort_columns, kind="stable")
        return frame[columns].reset_index(drop=True)

    def read_factor_ic(self, factor: str, days: int = 60) -> pd.DataFrame:
        """Read a bounded factor IC curve from the existing CSV artifact.

        Factor names are restricted to filename-safe identifiers before any
        filesystem access.  The returned ``day`` column is canonicalized to
        YYYYMMDD and rows are sorted ascending; all other CSV columns are
        preserved for later equivalence testing.
        """
        if days <= 0 or not re.fullmatch(r"[A-Za-z0-9_-]+", factor or ""):
            if factor:
                raise ValueError(f"invalid factor name: {factor!r}")
            return pd.DataFrame(columns=["day"])
        path = (self.ic_root / f"ic_curve_{factor}_k20.csv").resolve()
        root = self.ic_root.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return pd.DataFrame(columns=["day"])
        try:
            frame = pd.read_csv(path)
        except Exception as exc:
            _LOG.warning("read factor IC failed: %s: %s", path, exc)
            return pd.DataFrame(columns=["day"])
        day_col = "day" if "day" in frame.columns else "date" if "date" in frame.columns else None
        if day_col is None:
            _LOG.warning("factor IC has no day/date column: %s", path)
            return pd.DataFrame(columns=["day"])
        if day_col != "day":
            frame = frame.rename(columns={day_col: "day"})
        frame["day"] = frame["day"].map(self._day_value)
        frame = frame[frame["day"].str.len() == 8]
        return frame.sort_values("day").tail(days).reset_index(drop=True)

    def read_perf_reports(self, days: int = 60) -> pd.DataFrame:
        """Read immutable per-day performance report artifacts.

        The adapter intentionally requires ``performance_report.json`` under
        each daily directory.  The current root-level
        ``data/performance_report.json`` is a latest-snapshot artifact, not a
        historical series, so it is never treated as history here.
        """
        if days <= 0 or not self.daily_root.is_dir():
            return self._empty_perf()
        rows: list[dict[str, Any]] = []
        for day_dir in sorted(self.daily_root.iterdir()):
            if not day_dir.is_dir() or not self._is_day(day_dir.name):
                continue
            path = day_dir / "performance_report.json"
            if not path.is_file():
                continue
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(report, dict):
                    raise ValueError("performance report root is not an object")
                metrics = report.get("metrics") or {}
                benchmark = report.get("benchmark") or {}
                day = self._day_value(
                    report.get("day")
                    or (report.get("period") or {}).get("end"),
                    day_dir.name,
                )
                rows.append(
                    {
                        "day": day,
                        "summary_json": json.dumps(
                            report, ensure_ascii=False, default=str
                        ),
                        "total_return": metrics.get("total_return"),
                        "annual_return": metrics.get("annual_return"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "sharpe_annual": metrics.get("sharpe_annual"),
                        "calmar": metrics.get("calmar"),
                        "excess_total": benchmark.get("excess_total"),
                    }
                )
            except Exception as exc:
                _LOG.warning("read performance report failed: %s: %s", path, exc)
        if not rows:
            return self._empty_perf()
        frame = pd.DataFrame(rows, columns=["day", *_PERF_COLUMNS])
        frame = frame.drop_duplicates(subset=["day"], keep="last")
        frame = frame.sort_values("day").tail(days).set_index("day")
        frame.index.name = "day"
        return frame[_PERF_COLUMNS]

    def read_reward_curve(self, days: int = 30) -> pd.DataFrame:
        """Read the dormant per-training-day ``reward_curve.jsonl`` contract.

        ``train_meta.json`` and ``reward_curve.png`` are deliberately not
        interpreted as a step-level history.  A producer must explicitly
        write one JSON object per line with ``step`` and ``reward``.
        """
        if days <= 0 or not self.drl_root.is_dir():
            return self._empty_reward()
        day_dirs = [
            p for p in self.drl_root.iterdir()
            if p.is_dir() and self._is_day(p.name)
        ]
        selected = sorted(day_dirs, key=lambda p: p.name)[-days:]
        rows: list[dict[str, Any]] = []
        for day_dir in selected:
            path = day_dir / "reward_curve.jsonl"
            if not path.is_file():
                continue
            try:
                for line_no, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError(f"line {line_no}: root is not an object")
                    if "step" not in item or "reward" not in item:
                        raise ValueError(f"line {line_no}: missing step/reward")
                    row = {
                        "day": self._day_value(item.get("day"), day_dir.name),
                        "model_version": item.get("model_version"),
                        "step": int(item["step"]),
                        "reward": float(item["reward"]),
                        "weights_json": item.get("weights_json"),
                    }
                    rows.append(row)
            except Exception as exc:
                _LOG.warning("read reward curve failed: %s: %s", path, exc)
        if not rows:
            return self._empty_reward()
        frame = pd.DataFrame(rows, columns=_REWARD_COLUMNS)
        return frame.sort_values(["day", "step"], kind="stable").reset_index(drop=True)

    @staticmethod
    def _is_day(value: str) -> bool:
        return len(value) == 8 and value.isdigit()

    @staticmethod
    def _day_value(value: Any, fallback: str = "") -> str:
        text = str(value or fallback).strip().replace("-", "")
        return text[:8] if len(text) >= 8 and text[:8].isdigit() else fallback

    @staticmethod
    def _iso_day(value: Any) -> str:
        text = str(value or "").strip().replace("/", "-")
        compact = text.replace("-", "")
        if len(compact) >= 8 and compact[:8].isdigit():
            return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
        return text

    @staticmethod
    def _equity(summary: dict[str, Any]) -> float | int | None:
        paper = ((summary.get("steps") or {}).get("paper") or {})
        for value in (
            paper.get("equity"),
            summary.get("equity"),
            (summary.get("summary") or {}).get("equity"),
        ):
            if value is not None:
                return value
        return None

    @staticmethod
    def _empty() -> pd.DataFrame:
        frame = pd.DataFrame(columns=_COLUMNS)
        frame.index.name = "day"
        return frame

    @staticmethod
    def _empty_perf() -> pd.DataFrame:
        frame = pd.DataFrame(columns=_PERF_COLUMNS)
        frame.index.name = "day"
        return frame

    @staticmethod
    def _empty_reward() -> pd.DataFrame:
        return pd.DataFrame(columns=_REWARD_COLUMNS)
