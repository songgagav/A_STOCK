# -*- coding: utf-8 -*-
"""09:25 后候选仅归档、仅审计的契约。"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import signal_freeze_watch as SFW  # noqa: E402
from signal_snapshot import archive_late_signal  # noqa: E402


_SH = ZoneInfo("Asia/Shanghai")
_CANDIDATE = {"targets": [{"canon": "600000.SH", "target_weight": 1.0}], "source": "midday_reselect"}


def test_midday_candidate_is_archived_with_arrival_day_and_unknown_reason(tmp_path):
    result = archive_late_signal(
        str(tmp_path), "20261003", _CANDIDATE,
        datetime(2026, 10, 3, 11, 28, tzinfo=_SH), "ready",
    )

    assert result["status"] == "archived"
    item = json.loads(Path(result["path"]).read_text(encoding="utf-8"))["items"][0]
    assert item["late_for_consume_day"] == "20261003"
    assert item["delay_reason"] == "unknown"
    assert len(item["candidate_id"]) == len(item["payload_hash"]) == 64


def test_out_of_window_candidate_is_not_archived(tmp_path):
    result = archive_late_signal(
        str(tmp_path), "20261003", _CANDIDATE,
        datetime(2026, 10, 3, 15, 0, 1, tzinfo=_SH), "ready",
    )

    assert result["status"] == "out_of_window"
    assert not (tmp_path / "daily" / "20261003" / "late_signals_20261003.json").exists()


def test_generic_event_is_appended_to_hash_ledger(tmp_path):
    path = tmp_path / "events.jsonl"
    record = SFW.record_event("late_signal_archived", {"candidate_id": "abc"}, path=str(path))

    assert record["kind"] == "late_signal_archived"
    assert json.loads(path.read_text(encoding="utf-8"))["payload"] == {"candidate_id": "abc"}
