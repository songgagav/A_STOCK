"""Read-only file-backed history adapter for the ArcticDB migration.

This module deliberately has no production consumers yet.  It establishes the
first replacement contract for ``daily_summary`` using the immutable daily
JSON artifacts already produced by the paper-book pipeline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd


_LOG = logging.getLogger("file_history_store")
_COLUMNS = ["summary_json", "equity"]


class FileHistoryStore:
    """Read daily history from an injected ``data/daily``-like directory."""

    def __init__(self, daily_root: str | Path):
        self.daily_root = Path(daily_root)

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

    @staticmethod
    def _is_day(value: str) -> bool:
        return len(value) == 8 and value.isdigit()

    @staticmethod
    def _day_value(value: Any, fallback: str) -> str:
        text = str(value or fallback).strip().replace("-", "")
        return text[:8] if len(text) >= 8 and text[:8].isdigit() else fallback

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
