# -*- coding: utf-8 -*-
"""不可变 09:25 信号快照的规范化与完整性契约。"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from signal_snapshot import build_snapshot  # noqa: E402


_SH = ZoneInfo("Asia/Shanghai")
_FIXED_AT = datetime(2026, 10, 3, 9, 25, tzinfo=_SH)


def _snapshot(targets: list[dict]) -> dict:
    return build_snapshot(
        "20261003",
        targets,
        "drl_same_day",
        "20261002",
        [],
        _FIXED_AT,
        _REPO,
    )


def test_snapshot_hash_is_stable_for_identical_input():
    """防止键顺序或默认 JSON 空白导致同一输入被误判篡改。"""
    targets = [{"canon": "600000.SH", "target_weight": 1.0}]

    assert _snapshot(targets)["snapshot_hash"] == _snapshot(targets)["snapshot_hash"]


def test_snapshot_hash_changes_when_target_changes():
    """防止读取方把不同目标池误当成同一冻结决策。"""
    left = _snapshot([{"canon": "600000.SH", "target_weight": 1.0}])
    right = _snapshot([{"canon": "000001.SZ", "target_weight": 1.0}])

    assert left["snapshot_hash"] != right["snapshot_hash"]


def test_snapshot_records_schema_weights_and_both_timezones():
    """防止跨平台时钟让 09:25 的审计时间失去上海和 UTC 对照。"""
    snap = _snapshot([{"canon": "600000.SH", "target_weight": 1.0}])

    assert snap["schema_version"] == 1
    assert snap["weights"] == {"600000.SH": 1.0}
    assert snap["generated_at"] == "2026-10-03T09:25:00+08:00"
    assert snap["generated_at_utc"] == "2026-10-03T01:25:00Z"
    assert len(snap["input_hash"]) == len(snap["snapshot_hash"]) == 64


def test_snapshot_rejects_non_finite_input_values():
    """防止 NaN/Infinity 产生解释器相关的完整性哈希。"""
    with pytest.raises(ValueError):
        _snapshot([{"canon": "600000.SH", "target_weight": math.nan}])


def test_snapshot_rejects_source_artifact_outside_repository(tmp_path):
    """防止快照把仓库外的任意路径伪装成受控决策输入。"""
    outside = tmp_path / "untrusted.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        build_snapshot(
            "20261003",
            [{"canon": "600000.SH", "target_weight": 1.0}],
            "drl_same_day",
            "20261002",
            [{"path": str(outside), "sha256": "0" * 64}],
            _FIXED_AT.astimezone(timezone.utc),
            _REPO,
        )
