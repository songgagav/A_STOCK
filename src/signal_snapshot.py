"""09:25 冻结信号快照的规范化构造器。

本模块的后续阶段会增加落盘、读取和迟到信号治理；本阶段只定义稳定、
可审计且不依赖运行环境的快照数据契约。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from utils import atomic_write_json


SCHEMA_VERSION = 1
SHANGHAI = ZoneInfo("Asia/Shanghai")
_LATE_SIGNAL_LOCK = threading.Lock()


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    """返回用于跨平台哈希的 UTF-8 规范 JSON；拒绝 NaN 和 Infinity。"""
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"快照包含不可规范化的值: {exc}") from exc
    return text.encode("utf-8")


def sha256_json(value: dict[str, Any]) -> str:
    """计算规范化 JSON 的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_day(value: str, field: str) -> str:
    text = str(value)
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"{field} 必须为 YYYYMMDD: {value!r}")
    return text


def _relative_artifacts(artifacts: list[dict[str, Any]], repo_root: str) -> list[dict[str, str]]:
    """验证并规范化实际消费的输入文件，拒绝仓库外路径。"""
    root = Path(repo_root).resolve()
    result: list[dict[str, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("source_artifacts 项必须为对象")
        raw_path = artifact.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("source_artifacts 项缺少 path")
        path = Path(raw_path)
        resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"source_artifact 不在仓库内: {raw_path}") from exc
        digest = artifact.get("sha256")
        if digest is None:
            if not resolved.is_file():
                raise ValueError(f"source_artifact 不存在: {raw_path}")
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"source_artifact sha256 非法: {raw_path}")
        result.append({"path": relative.as_posix(), "sha256": digest.lower()})
    return result


def _normalized_targets(targets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    if not isinstance(targets, list):
        raise ValueError("targets 必须为列表")
    normalized: list[dict[str, Any]] = []
    weights: dict[str, float] = {}
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("target 必须为对象")
        canon = target.get("canon")
        if not isinstance(canon, str) or not canon:
            raise ValueError("target 缺少 canon")
        if canon in weights:
            raise ValueError(f"target 重复: {canon}")
        weight = target.get("target_weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not math.isfinite(weight):
            raise ValueError(f"target_weight 非有限: {canon}")
        copied = dict(target)
        copied["target_weight"] = float(weight)
        normalized.append(copied)
        weights[canon] = float(weight)
    return normalized, weights


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_snapshot(
    day: str,
    targets: list[dict[str, Any]],
    source_tier: str,
    source_date: str,
    source_artifacts: list[dict[str, Any]],
    generated_at: datetime,
    repo_root: str,
) -> dict[str, Any]:
    """构造 schema v1 快照，并生成输入与快照完整性哈希。"""
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at 必须带时区")
    normalized_targets, weights = _normalized_targets(targets)
    artifacts = _relative_artifacts(source_artifacts, repo_root)
    day = _require_day(day, "date")
    source_date = _require_day(source_date, "source_date")
    if not isinstance(source_tier, str) or not source_tier:
        raise ValueError("source_tier 不能为空")

    input_payload = {
        "date": day,
        "targets": normalized_targets,
        "weights": weights,
        "source_tier": source_tier,
        "source_date": source_date,
        "source_artifacts": artifacts,
    }
    shanghai_at = generated_at.astimezone(SHANGHAI).replace(microsecond=0)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        **input_payload,
        "generated_at": shanghai_at.isoformat(),
        "generated_at_utc": _iso_utc(generated_at),
        "input_hash": sha256_json(input_payload),
        "snapshot_hash": None,
    }
    snapshot["snapshot_hash"] = sha256_json(snapshot)
    return snapshot


def snapshot_path(data_dir: str, day: str) -> str:
    """返回指定交易日冻结快照的唯一权威路径。"""
    day = _require_day(day, "date")
    return str(Path(data_dir) / "daily" / day / f"signal_snapshot_{day}.json")


def _invalid(reason: str) -> dict[str, Any]:
    return {"status": "invalid", "snapshot": None, "reason": reason}


def _tampered(reason: str) -> dict[str, Any]:
    return {"status": "tampered", "snapshot": None, "reason": reason}


def _required_snapshot_fields(snapshot: dict[str, Any]) -> bool:
    required = {
        "schema_version",
        "date",
        "targets",
        "weights",
        "source_tier",
        "source_date",
        "source_artifacts",
        "generated_at",
        "generated_at_utc",
        "input_hash",
        "snapshot_hash",
    }
    return required.issubset(snapshot)


def _input_payload_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": snapshot["date"],
        "targets": snapshot["targets"],
        "weights": snapshot["weights"],
        "source_tier": snapshot["source_tier"],
        "source_date": snapshot["source_date"],
        "source_artifacts": snapshot["source_artifacts"],
    }


def _validate_snapshot(snapshot: Any, day: str) -> dict[str, Any]:
    """验证已落盘快照；绝不以实时目标池替代失败的快照。"""
    if not isinstance(snapshot, dict) or not _required_snapshot_fields(snapshot):
        return _invalid("missing_required_fields")
    if snapshot["schema_version"] != SCHEMA_VERSION:
        return _invalid("unsupported_schema_version")
    if snapshot["date"] != day:
        return _invalid("snapshot_day_mismatch")
    if not isinstance(snapshot["input_hash"], str) or len(snapshot["input_hash"]) != 64:
        return _invalid("invalid_input_hash")
    if not isinstance(snapshot["snapshot_hash"], str) or len(snapshot["snapshot_hash"]) != 64:
        return _invalid("invalid_snapshot_hash")

    try:
        if sha256_json(_input_payload_from_snapshot(snapshot)) != snapshot["input_hash"]:
            return _tampered("input_hash_mismatch")
        hash_payload = dict(snapshot)
        hash_payload["snapshot_hash"] = None
        if sha256_json(hash_payload) != snapshot["snapshot_hash"]:
            return _tampered("snapshot_hash_mismatch")
    except (TypeError, ValueError):
        return _invalid("non_canonical_snapshot")
    return {"status": "ready", "snapshot": snapshot, "reason": None}


def write_snapshot(data_dir: str, snapshot: dict[str, Any]) -> str:
    """以共享原子写入器落盘一个已验证的信号快照。"""
    day = _require_day(snapshot.get("date"), "date")
    result = _validate_snapshot(snapshot, day)
    if result["status"] != "ready":
        raise ValueError(f"拒绝写入无效快照: {result['reason']}")
    path = snapshot_path(data_dir, day)
    atomic_write_json(path, snapshot)
    return path


def read_snapshot(data_dir: str, day: str) -> dict[str, Any]:
    """读取并验证权威快照，明确区分 L1/L2/L3，且不做实时回退。"""
    day = _require_day(day, "date")
    path = Path(snapshot_path(data_dir, day))
    if not path.is_file():
        return {"status": "missing", "snapshot": None, "reason": "snapshot_not_found"}
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _invalid("invalid_json")
    return _validate_snapshot(snapshot, day)


def read_mode_control(repo_root: str) -> dict[str, str]:
    """读取冻结消费模式；任何缺失或无效控制记录都保守回退 shadow。"""
    default = {"mode": "shadow"}
    path = Path(repo_root) / "config" / "signal_freeze_mode.json"
    try:
        control = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default
    if not isinstance(control, dict):
        return default
    if control.get("mode") == "shadow":
        return default
    if control.get("mode") != "enforce":
        return default
    required = ("promoted_at", "promoted_by", "evidence")
    if not all(isinstance(control.get(field), str) and control[field].strip() for field in required):
        return default
    return {
        "mode": "enforce",
        "promoted_at": control["promoted_at"],
        "promoted_by": control["promoted_by"],
        "evidence": control["evidence"],
    }


def _late_signal_path(data_dir: str, day: str) -> str:
    return str(Path(data_dir) / "daily" / day / f"late_signals_{day}.json")


def _record_freeze_event(kind: str, payload: dict[str, Any]) -> None:
    """审计失败不得阻断归档；账本自身负责哈希链与吞异常。"""
    try:
        from signal_freeze_watch import record_event
        record_event(kind, payload)
    except Exception:  # noqa: BLE001
        pass


def archive_late_signal(
    data_dir: str,
    day: str,
    candidate: dict[str, Any],
    arrived_at: datetime,
    snapshot_status: str,
) -> dict[str, Any]:
    """归档迟到候选，绝不改变当日快照/目标池。"""
    day = _require_day(day, "date")
    if arrived_at.tzinfo is None or arrived_at.utcoffset() is None:
        raise ValueError("arrived_at 必须带时区")
    local_at = arrived_at.astimezone(SHANGHAI)
    at_time = local_at.time()
    start = local_at.replace(hour=9, minute=25, second=0, microsecond=0).time()
    end = local_at.replace(hour=15, minute=0, second=0, microsecond=0).time()
    if not (start < at_time <= end):
        result = {"status": "out_of_window", "reason": "outside_0925_1500_window"}
        _record_freeze_event("late_signal_out_of_window", {
            "date": day, "arrived_at": local_at.isoformat(),
            "snapshot_status": snapshot_status, **result,
        })
        return result
    try:
        payload_hash = sha256_json(candidate)
        item = {
            "candidate_id": hashlib.sha256(
                canonical_json_bytes({"date": day, "arrived_at": local_at.isoformat(),
                                      "payload_hash": payload_hash})
            ).hexdigest(),
            "payload_hash": payload_hash,
            "late_for_consume_day": day,
            "arrived_at": local_at.isoformat(),
            "arrived_at_utc": _iso_utc(arrived_at),
            "source": candidate.get("source", "unknown"),
            "delay_reason": candidate.get("delay_reason", "unknown"),
            "snapshot_status": snapshot_status,
            "candidate": candidate,
        }
        path = _late_signal_path(data_dir, day)
        with _LATE_SIGNAL_LOCK:
            try:
                current = json.loads(Path(path).read_text(encoding="utf-8"))
            except FileNotFoundError:
                current = {"schema_version": 1, "date": day, "items": []}
            if not isinstance(current, dict) or current.get("date") != day or not isinstance(current.get("items"), list):
                raise ValueError("late signal archive schema invalid")
            current["items"].append(item)
            atomic_write_json(path, current)
        result = {"status": "archived", "path": path, "candidate_id": item["candidate_id"]}
        _record_freeze_event("late_signal_archived", {"date": day, **result})
        return result
    except Exception as exc:  # noqa: BLE001
        result = {"status": "archive_failed", "reason": f"{type(exc).__name__}: {exc}"}
        _record_freeze_event("late_signal_archive_failed", {"date": day, **result})
        return result
