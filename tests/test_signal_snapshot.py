# -*- coding: utf-8 -*-
"""不可变 09:25 信号快照的规范化与完整性契约。"""
from __future__ import annotations

import math
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from signal_snapshot import (  # noqa: E402
    build_snapshot,
    read_snapshot,
    snapshot_path,
    write_snapshot,
)


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


def test_write_then_read_returns_verified_snapshot(tmp_path):
    """防止原子落盘后的权威快照无法被当日消费者复核。"""
    snapshot = _snapshot([{"canon": "600000.SH", "target_weight": 1.0}])

    path = write_snapshot(str(tmp_path), snapshot)
    result = read_snapshot(str(tmp_path), "20261003")

    assert Path(path) == Path(snapshot_path(str(tmp_path), "20261003"))
    assert result["status"] == "ready"
    assert result["snapshot"] == snapshot
    assert result["reason"] is None


def test_read_missing_snapshot_returns_l1_missing(tmp_path):
    """防止读取方把缺失快照静默降级为重新解析目标池。"""
    result = read_snapshot(str(tmp_path), "20261003")

    assert result == {"status": "missing", "snapshot": None, "reason": "snapshot_not_found"}


def test_read_malformed_snapshot_returns_l2_invalid(tmp_path):
    """防止损坏 JSON 被解释为可交易的空信号。"""
    path = Path(snapshot_path(str(tmp_path), "20261003"))
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    result = read_snapshot(str(tmp_path), "20261003")

    assert result["status"] == "invalid"
    assert result["snapshot"] is None
    assert result["reason"] == "invalid_json"


@pytest.mark.parametrize(
    ("field", "value"),
    [("input_hash", None), ("schema_version", 2)],
)
def test_read_missing_required_or_unsupported_schema_is_l2_invalid(tmp_path, field, value):
    """防止旧 schema 或缺审计字段被误当成可消费快照。"""
    snapshot = _snapshot([{"canon": "600000.SH", "target_weight": 1.0}])
    snapshot[field] = value
    path = Path(snapshot_path(str(tmp_path), "20261003"))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = read_snapshot(str(tmp_path), "20261003")

    assert result["status"] == "invalid"
    assert result["snapshot"] is None


def test_read_changed_target_with_old_hash_returns_l3_tampered(tmp_path):
    """防止内容被修改但未更新摘要的快照继续驱动虚拟盘。"""
    snapshot = _snapshot([{"canon": "600000.SH", "target_weight": 1.0}])
    snapshot["targets"][0]["canon"] = "000001.SZ"
    path = Path(snapshot_path(str(tmp_path), "20261003"))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = read_snapshot(str(tmp_path), "20261003")

    assert result["status"] == "tampered"
    assert result["snapshot"] is None
    assert result["reason"] == "input_hash_mismatch"


def test_read_changed_metadata_with_old_hash_returns_l3_tampered(tmp_path):
    """防止非输入字段被修改后绕过快照自身的完整性校验。"""
    snapshot = _snapshot([{"canon": "600000.SH", "target_weight": 1.0}])
    snapshot["generated_at"] = "2026-10-03T09:26:00+08:00"
    path = Path(snapshot_path(str(tmp_path), "20261003"))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = read_snapshot(str(tmp_path), "20261003")

    assert result["status"] == "tampered"
    assert result["snapshot"] is None
    assert result["reason"] == "snapshot_hash_mismatch"


def test_read_snapshot_with_wrong_embedded_day_is_l2_invalid(tmp_path):
    """防止昨日的合法快照被放进今天目录后错误复用。"""
    snapshot = build_snapshot(
        "20261004",
        [{"canon": "600000.SH", "target_weight": 1.0}],
        "drl_same_day",
        "20261003",
        [],
        _FIXED_AT,
        _REPO,
    )
    path = Path(snapshot_path(str(tmp_path), "20261003"))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = read_snapshot(str(tmp_path), "20261003")

    assert result["status"] == "invalid"
    assert result["snapshot"] is None
    assert result["reason"] == "snapshot_day_mismatch"
