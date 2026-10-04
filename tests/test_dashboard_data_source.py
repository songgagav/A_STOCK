from __future__ import annotations

from pathlib import Path

from src import dashboard


def test_dashboard_exposes_data_source_endpoint_and_status_panel() -> None:
    source = Path(dashboard.__file__).read_text(encoding="utf-8")

    assert 'if path == "/api/data-sources"' in source
    assert 'id="sourceHealthBody"' in source
    assert "loadSourceHealth" in source


def test_dashboard_reports_h5i_unavailable_without_crashing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "_BASE", str(tmp_path))

    result = dashboard.read_data_source_health()

    assert result["ok"] is True
    assert result["status"] == "blocked"
    assert result["h5i"]["status"] == "unavailable"
    assert result["batches"]["total"] == 0
