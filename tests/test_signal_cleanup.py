# -*- coding: utf-8 -*-
"""迟到候选人工审核与冻结产物保留边界。"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from signal_snapshot import (  # noqa: E402
    approved_late_candidate,
    archive_late_signal,
    build_snapshot,
    cleanup_freeze_artifacts,
    write_late_review,
    write_snapshot,
)


SH = ZoneInfo("Asia/Shanghai")
AT_1128 = datetime(2026, 10, 3, 11, 28, tzinfo=SH)
AT_1500 = datetime(2026, 10, 3, 15, 0, tzinfo=SH)
TARGETS = [{"canon": "600000.SH", "target_weight": 1.0}]


def _review(candidate_id: str, payload_hash: str, verdict: str = "approved") -> dict:
    return {
        "reviewer": "tester",
        "reviewed_at": AT_1500,
        "candidate_id": candidate_id,
        "payload_hash": payload_hash,
        "verdict": verdict,
        "comment": "人工审核记录",
    }


def test_partial_review_is_not_approved(tmp_path):
    archived = archive_late_signal(str(tmp_path), "20261003", {
        "targets": TARGETS, "source": "midday_reselect",
    }, AT_1128, "ready")
    write_late_review(str(tmp_path), "20261003", _review(archived["candidate_id"], "a" * 64))

    assert approved_late_candidate(str(tmp_path), "20261003") is None


def test_complete_matching_review_is_approved(tmp_path):
    archived = archive_late_signal(str(tmp_path), "20261003", {
        "targets": TARGETS, "source": "midday_reselect",
    }, AT_1128, "ready")
    write_late_review(str(tmp_path), "20261003", _review(
        archived["candidate_id"],
        __import__("json").loads(Path(archived["path"]).read_text(encoding="utf-8"))["items"][0]["payload_hash"],
    ))

    result = approved_late_candidate(str(tmp_path), "20261003")
    assert result is not None
    assert result["targets"] == TARGETS


def test_cleanup_does_not_delete_when_calendar_missing(tmp_path):
    old = Path(tmp_path) / "daily" / "20260101" / "signal_snapshot_20260101.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}", encoding="utf-8")

    report = cleanup_freeze_artifacts(str(tmp_path), None)

    assert report["deleted"] == 0
    assert old.exists()


def test_cleanup_does_not_delete_when_latest_snapshot_is_invalid(tmp_path):
    days = ["20260101", "20260102", "20260103"]
    old = Path(tmp_path) / "daily" / days[0] / f"late_review_{days[0]}.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"reviews": []}', encoding="utf-8")
    latest = Path(tmp_path) / "daily" / days[-1] / f"signal_snapshot_{days[-1]}.json"
    latest.parent.mkdir(parents=True)
    latest.write_text("{bad-json", encoding="utf-8")

    report = cleanup_freeze_artifacts(str(tmp_path), days, keep_days=2)

    assert report["deleted"] == 0
    assert old.exists()


def test_cleanup_removes_t91_but_keeps_t90_after_valid_new_snapshot(tmp_path):
    days = [f"2026{month:02d}01" for month in range(1, 13)]
    latest = days[-1]
    snapshot = build_snapshot(latest, TARGETS, "selection", latest, [], AT_1128, _REPO)
    write_snapshot(str(tmp_path), snapshot)
    old_day, boundary_day = days[0], days[1]
    for day in (old_day, boundary_day):
        path = Path(tmp_path) / "daily" / day / f"late_signals_{day}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"items": []}', encoding="utf-8")

    report = cleanup_freeze_artifacts(str(tmp_path), days, keep_days=11)

    assert report["deleted"] == 1
    assert not (Path(tmp_path) / "daily" / old_day / f"late_signals_{old_day}.json").exists()
    assert (Path(tmp_path) / "daily" / boundary_day / f"late_signals_{boundary_day}.json").exists()
