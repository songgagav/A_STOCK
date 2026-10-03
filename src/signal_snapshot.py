"""09:25 冻结信号快照的规范化构造器。

本模块的后续阶段会增加落盘、读取和迟到信号治理；本阶段只定义稳定、
可审计且不依赖运行环境的快照数据契约。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
SHANGHAI = ZoneInfo("Asia/Shanghai")


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
