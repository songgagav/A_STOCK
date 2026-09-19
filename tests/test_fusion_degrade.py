# -*- coding: utf-8 -*-
"""融合口径降级路径的回归测试 (2026-09-19).

覆盖本次修复的场景:
  fusion 覆盖跌破 MIN_COVERAGE -> 回退 f_ml -> **f_ml 因数据湖退役整条断链** ->
  最终静默 used=equal(排序完全不含融合加成), 实盘无任何可观测信号。

修复后要求:
  1. f_ml 可正常产出时, 降级链止于 `fml_fallback`(而非 equal);
  2. f_ml 不可用时, 必须以 `degraded=True` + 明确 `degrade_reason` 落到
     fusion_health.json, 并追加 fusion_degradation.jsonl;
  3. 留痕失败**不得**影响主链路(绝不抛异常)。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

import factor_fusion as ff  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_health(tmp_path, monkeypatch):
    """把健康度落盘重定向到临时目录, 避免污染 data/."""
    monkeypatch.setattr(ff, "FUSION_HEALTH_FP", str(tmp_path / "fusion_health.json"))
    monkeypatch.setattr(ff, "FUSION_DEGRADE_FP",
                        str(tmp_path / "fusion_degradation.jsonl"))
    monkeypatch.delenv("FORCE_FML", raising=False)
    monkeypatch.delenv("FUSION_SCORE", raising=False)
    return tmp_path


def _health() -> dict:
    with open(ff.FUSION_HEALTH_FP, encoding="utf-8") as f:
        return json.load(f)


def _degrade_lines() -> list[str]:
    if not os.path.exists(ff.FUSION_DEGRADE_FP):
        return []
    with open(ff.FUSION_DEGRADE_FP, encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


def _patch_fusion(monkeypatch, scored: dict, n_scored: int = 100):
    """伪造 cross_section_scores 的返回(全截面打分).

    注意 n_scored 默认 100(>= MIN_POOL_N=30): 否则会先撞"样本少"分支,
    测不到我们想测的覆盖率分支。
    """
    def _fake(as_of, symbols=None):
        return {"as_of": str(as_of), "n_pool": 100, "n_scored": int(n_scored),
                "coverage": 1.0, "scores": scored, "meta": {}}
    monkeypatch.setattr(ff, "cross_section_scores", _fake)


def _patch_fml(monkeypatch, scores: dict | None, error: str = ""):
    import ml_fusion_bridge

    def _fake(canons, as_of, model_path=None):
        if scores is None:
            return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                    "meta": {"error": error}}
        return {"available": True, "as_of": as_of, "scores": scores,
                "n": len(scores), "meta": {}}
    monkeypatch.setattr(ml_fusion_bridge, "compute_fml", _fake)


CANONS = ["600519.SH", "000001.SZ", "300750.SZ", "601398.SH", "600036.SH"]


def test_fusion_ok_is_not_degraded(monkeypatch):
    """覆盖达标 -> used=fusion, 不降级, 不写降级留痕."""
    _patch_fusion(monkeypatch, {c.split(".")[0]: 0.1 for c in CANONS})
    sc, used = ff.fusion_or_fml([{"canon": c} for c in CANONS], "2026-03-05")
    assert used == "fusion"
    assert len(sc) == len(CANONS)
    h = _health()
    assert h["used"] == "fusion" and h["degraded"] is False
    assert _degrade_lines() == []


def test_low_coverage_falls_back_to_fml(monkeypatch):
    """覆盖不足但 f_ml 可用 -> 止于 fml_fallback(这正是修复前到不了的那一步)."""
    _patch_fusion(monkeypatch, {"600519": 0.1})          # 5 个只命中 1 个 -> cov 0.2
    _patch_fml(monkeypatch, {c: 0.02 for c in CANONS})
    sc, used = ff.fusion_or_fml([{"canon": c} for c in CANONS], "2026-03-05")
    assert used == "fml_fallback"
    assert set(sc) == set(CANONS)
    h = _health()
    assert h["degraded"] is True and h["used"] == "fml_fallback"
    assert "覆盖不足" in h["degrade_reason"]
    assert len(_degrade_lines()) == 1


def test_fml_broken_ends_equal_with_reason(monkeypatch):
    """f_ml 不可用 -> used=equal, 但必须带明确原因落盘(修复前的静默场景)."""
    _patch_fusion(monkeypatch, {"600519": 0.1})
    _patch_fml(monkeypatch, None, error="duckdb: Table daily_bars does not exist")
    sc, used = ff.fusion_or_fml([{"canon": c} for c in CANONS], "2026-03-05")
    assert used == "equal" and sc == {}
    h = _health()
    assert h["degraded"] is True and h["used"] == "equal"
    assert "覆盖不足" in h["degrade_reason"]
    assert "daily_bars" in h["degrade_reason"]          # 根因被带出来, 不再静默
    assert len(_degrade_lines()) == 1


def test_fml_exception_is_captured(monkeypatch):
    """f_ml 抛异常也不能炸主链路, 且原因要落盘."""
    import ml_fusion_bridge

    def _boom(canons, as_of, model_path=None):
        raise RuntimeError("fusion venv 缺失")
    monkeypatch.setattr(ml_fusion_bridge, "compute_fml", _boom)
    _patch_fusion(monkeypatch, {})
    sc, used = ff.fusion_or_fml([{"canon": c} for c in CANONS], "2026-03-05")
    assert used == "equal"
    assert "fusion venv 缺失" in _health()["degrade_reason"]


def test_record_state_never_raises(monkeypatch, tmp_path):
    """落盘失败(路径不可写)必须被吞掉: 选股主链路不允许因此中断."""
    monkeypatch.setattr(ff, "FUSION_HEALTH_FP",
                        str(tmp_path / "no_such_dir" / "x" / "h.json"))
    monkeypatch.setattr(ff, "FUSION_DEGRADE_FP", str(tmp_path / "no_such_dir" / "d.jsonl"))
    ff._record_fusion_state("equal", {"used": "equal", "degraded": True})  # 不抛即通过
