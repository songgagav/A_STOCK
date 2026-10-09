"""ArcticDB 缺失时，策略退化检测必须显式不可用。"""

import sys
import types

import dashboard
import degradation as D
import incremental_learn as IL


def test_run_full_check_reports_unavailable_when_arcticdb_is_missing(monkeypatch):
    """缺可选历史后端不能伪造空退化分数，也不能只抛异常让上层吞掉。"""

    def missing_arcticdb():
        raise ModuleNotFoundError("No module named 'arcticdb'")

    monkeypatch.setattr(D, "get_store", missing_arcticdb)

    result = D.run_full_check(days=10)

    assert result["status"] == "unavailable"
    assert result["ok"] is False
    assert result["backend"] == "arcticdb"
    assert result["degradation_index"] is None
    assert "arcticdb" in result["reason"].lower()


def test_dashboard_does_not_turn_unavailable_into_empty_index(monkeypatch):
    """看板必须展示不可用，而不是把缺历史后端渲染成空指标。"""
    fake_degradation = types.SimpleNamespace(
        run_full_check=lambda days: {
            "ok": False,
            "status": "unavailable",
            "backend": "arcticdb",
            "reason": "ArcticDB 不可用",
            "degradation_index": None,
            "incremental_samples": [],
        }
    )
    monkeypatch.setitem(sys.modules, "degradation", fake_degradation)

    result = dashboard.read_degradation()

    assert result["status"] == "unavailable"
    assert result["backend"] == "arcticdb"
    assert result["index"] is None
    assert result["error"] == "ArcticDB 不可用"


def test_degradation_step_result_keeps_unavailable_non_ok():
    """日终回执的步骤结果不能把 unavailable 写成 ok=true。"""
    result = D.degradation_step_result({
        "ok": False,
        "status": "unavailable",
        "backend": "arcticdb",
        "reason": "ArcticDB 不可用",
        "degradation_index": None,
        "incremental_samples": [],
    })

    assert result == {
        "ok": False,
        "status": "unavailable",
        "backend": "arcticdb",
        "reason": "ArcticDB 不可用",
        "samples_n": 0,
    }


def test_incremental_learn_does_not_treat_unavailable_as_not_triggered(monkeypatch, tmp_path):
    """增量学习必须停止并报告 unavailable，不能继续按 OK 逻辑跳过。"""
    import config

    class FakeHeartbeat:
        def __init__(self, *args, **kwargs):
            self.stops = []

        def start(self, **kwargs):
            pass

        def stop(self, **kwargs):
            self.stops.append(kwargs)

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "heartbeat", types.SimpleNamespace(Heartbeat=FakeHeartbeat))
    monkeypatch.setitem(
        sys.modules,
        "degradation",
        types.SimpleNamespace(
            run_full_check=lambda days: {
                "ok": False,
                "status": "unavailable",
                "backend": "arcticdb",
                "reason": "ArcticDB 不可用",
                "degradation_index": None,
                "incremental_samples": [],
            }
        ),
    )

    result = IL.run_incremental_learn("2026-10-05")

    assert result["status"] == "unavailable"
    assert result["triggered"] is False
    assert "ArcticDB" in result["error"]
