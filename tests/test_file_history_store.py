import json

import pandas as pd

from file_history_store import FileHistoryStore


def _write_summary(root, day, equity):
    day_dir = root / day
    day_dir.mkdir(parents=True)
    (day_dir / "daily_summary.json").write_text(
        json.dumps({"day": day, "steps": {"paper": {"equity": equity}}}),
        encoding="utf-8",
    )


def test_daily_summary_adapter_reads_recent_rows_in_day_order(tmp_path):
    _write_summary(tmp_path, "20261002", 101.0)
    _write_summary(tmp_path, "20261001", 100.0)

    out = FileHistoryStore(tmp_path).read_daily_summaries(days=2)

    assert list(out.index) == ["20261001", "20261002"]
    assert list(out["equity"]) == [100.0, 101.0]
    assert json.loads(out.iloc[0]["summary_json"])["day"] == "20261001"


def test_daily_summary_adapter_limits_rows_and_skips_invalid_directories(tmp_path):
    _write_summary(tmp_path, "20260930", 99.0)
    _write_summary(tmp_path, "20261001", 100.0)
    _write_summary(tmp_path, "20261002", 101.0)
    (tmp_path / "not-a-day").mkdir()
    (tmp_path / "20261003").mkdir()

    out = FileHistoryStore(tmp_path).read_daily_summaries(days=2)

    assert list(out.index) == ["20261001", "20261002"]
    assert isinstance(out, pd.DataFrame)


def test_daily_summary_adapter_returns_explicit_empty_schema(tmp_path):
    out = FileHistoryStore(tmp_path).read_daily_summaries(days=20)

    assert out.empty
    assert list(out.columns) == ["summary_json", "equity"]
