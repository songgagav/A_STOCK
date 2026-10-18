from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import ic_curve_diagnostic as diagnostic


def _weekdays(start: str, count: int) -> list[str]:
    day = date.fromisoformat(start)
    result: list[str] = []
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def _previous_weekdays(before: str, count: int) -> list[str]:
    day = date.fromisoformat(before) - timedelta(days=1)
    result: list[str] = []
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day -= timedelta(days=1)
    return list(reversed(result))


def _write_calendar(path, days: list[str]) -> bytes:
    payload = json.dumps({"days": days}, separators=(",", ":")).encode("utf-8")
    path.write_bytes(payload)
    return payload


def _write_h5i(path, rows, *, columns=("ts", "symbol", "close", "change_pct")) -> None:
    h5i_db = pytest.importorskip("h5i_db", reason="h5i_db integration tests require .venv310")
    import pyarrow as pa

    type_by_name = {
        "ts": pa.timestamp("us"),
        "symbol": pa.string(),
        "close": pa.float64(),
        "change_pct": pa.float64(),
        "open": pa.float64(),
    }
    schema = pa.schema([pa.field(name, type_by_name[name]) for name in columns])
    db = h5i_db.Database(str(path), create=True)
    try:
        db.create_table("daily_bars", schema, time_column="ts")
        if rows:
            rows = sorted(rows, key=lambda row: (row["day"], row["symbol"]))
            values = {
                "ts": [datetime.combine(date.fromisoformat(row["day"]), datetime.min.time()) for row in rows],
                "symbol": [row["symbol"] for row in rows],
                "close": [row.get("close", 10.0) for row in rows],
                "change_pct": [row.get("change_pct", 0.0) for row in rows],
                "open": [row.get("open", 10.0) for row in rows],
            }
            db.append(
                "daily_bars",
                pa.Table.from_pydict({name: values[name] for name in columns}, schema=schema),
            )
    finally:
        db.close()


def _rows_for_symbol(days: list[str], symbol="A"):
    return [
        {"day": day, "symbol": symbol, "close": 10.0 + index / 100, "change_pct": 0.1}
        for index, day in enumerate(days)
    ]


def _tree_digest(path) -> str:
    digest = hashlib.sha256()
    paths = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    for item in paths:
        relative = item.name if path.is_file() else item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_read_only_h5i_window_preserves_source_bytes(tmp_path, monkeypatch):
    import h5i_db

    days = _weekdays("2026-06-01", 22)
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, _rows_for_symbol(days))
    _write_calendar(calendar_path, days)
    before = _tree_digest(h5i_path)
    original_database = h5i_db.Database
    read_only_values = []

    def capture_database(path, **kwargs):
        read_only_values.append(kwargs.get("read_only"))
        return original_database(path, **kwargs)

    monkeypatch.setattr(h5i_db, "Database", capture_database)
    result = diagnostic.read_diagnostic_inputs(
        h5i_path, calendar_path, start=days[0], end=days[0]
    )

    assert read_only_values == [True]
    assert _tree_digest(h5i_path) == before
    assert result.source_path == h5i_path.resolve()
    assert result.calendar_path == calendar_path.resolve()


def test_h5i_query_is_bounded_and_includes_twenty_pre_start_valid_closes(tmp_path):
    start = "2026-06-01"
    prior_days = _previous_weekdays(start, 25)
    future_days = _weekdays(start, 21)
    rows = _rows_for_symbol(prior_days + future_days)
    rows[prior_days.index(prior_days[-2])]["close"] = -1.0  # excluded before ranking
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, rows)
    _write_calendar(calendar_path, future_days)

    result = diagnostic.read_diagnostic_inputs(
        h5i_path, calendar_path, start=future_days[0], end=future_days[0]
    )

    pre_start = result.bars.loc[result.bars.day < future_days[0].replace("-", "")]
    assert len(pre_start) == 20
    expected_history = [
        day.replace("-", "")
        for day in prior_days
        if day != prior_days[-2]
    ][-20:]
    assert pre_start.day.tolist() == expected_history
    assert result.query_bounds["data_end"] == future_days[20].replace("-", "")
    assert result.bars.day.max() == future_days[20].replace("-", "")


def test_end_after_source_max_fails_closed(tmp_path):
    days = _weekdays("2026-06-01", 24)
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, _rows_for_symbol(days[:3]))
    _write_calendar(calendar_path, days)

    with pytest.raises(ValueError, match="end.*source max"):
        diagnostic.read_diagnostic_inputs(
            h5i_path, calendar_path, start=days[0], end=days[3]
        )


def test_duplicate_day_symbol_fails_closed(tmp_path):
    days = _weekdays("2026-06-01", 22)
    rows = _rows_for_symbol(days[:3])
    rows.append(dict(rows[0]))
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, rows)
    _write_calendar(calendar_path, days)

    with pytest.raises(ValueError, match="duplicate day/symbol"):
        diagnostic.read_diagnostic_inputs(
            h5i_path, calendar_path, start=days[0], end=days[0]
        )


def test_daily_bars_schema_mismatch_fails_closed(tmp_path):
    days = _weekdays("2026-06-01", 22)
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, _rows_for_symbol(days[:2]), columns=("ts", "symbol", "close"))
    _write_calendar(calendar_path, days)

    with pytest.raises(ValueError, match="daily_bars schema"):
        diagnostic.read_diagnostic_inputs(
            h5i_path, calendar_path, start=days[0], end=days[0]
        )


def test_calendar_sha_schema_fingerprint_and_row_digest_are_deterministic(tmp_path):
    days = _weekdays("2026-06-01", 22)
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    calendar_bytes = _write_calendar(calendar_path, days)
    _write_h5i(h5i_path, _rows_for_symbol(days[:3]))

    first = diagnostic.read_diagnostic_inputs(
        h5i_path, calendar_path, start=days[0], end=days[0]
    )
    second = diagnostic.read_diagnostic_inputs(
        h5i_path, calendar_path, start=days[0], end=days[0]
    )

    assert first.calendar_sha256 == hashlib.sha256(calendar_bytes).hexdigest()
    assert first.calendar_provenance == "local_calendar_unknown_upstream"
    assert first.schema_fingerprint == second.schema_fingerprint
    assert first.queried_rows_sha256 == second.queried_rows_sha256
    assert first.bars.equals(second.bars)


@pytest.mark.parametrize(
    "calendar_days",
    [
        ["2026-06-01", "2026-06-01"] + _weekdays("2026-06-02", 21),
        list(reversed(_weekdays("2026-06-01", 22))),
    ],
)
def test_malformed_duplicate_or_non_monotonic_calendar_fails_closed(tmp_path, calendar_days):
    days = _weekdays("2026-06-01", 22)
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, _rows_for_symbol(days[:2]))
    _write_calendar(calendar_path, calendar_days)

    with pytest.raises(ValueError, match="calendar.*strictly increasing"):
        diagnostic.read_diagnostic_inputs(
            h5i_path, calendar_path, start=days[0], end=days[0]
        )


def test_calendar_must_cover_max_future_due_day(tmp_path):
    days = _weekdays("2026-06-01", 8)
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, _rows_for_symbol(days[:2]))
    _write_calendar(calendar_path, days)

    with pytest.raises(ValueError, match="20 future weekdays"):
        diagnostic.read_diagnostic_inputs(
            h5i_path, calendar_path, start=days[0], end=days[0]
        )


def test_past_calendar_day_without_any_h5i_bars_is_a_gap(tmp_path):
    days = _weekdays("2026-06-01", 24)
    rows = _rows_for_symbol([days[0], days[2]])
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, rows)
    _write_calendar(calendar_path, days)

    with pytest.raises(ValueError, match="past calendar days without H5i bars"):
        diagnostic.read_diagnostic_inputs(
            h5i_path, calendar_path, start=days[0], end=days[2]
        )


def test_future_calendar_day_after_source_max_is_not_a_gap(tmp_path):
    days = _weekdays("2026-06-01", 23)
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    _write_h5i(h5i_path, _rows_for_symbol(days[:3]))
    _write_calendar(calendar_path, days)

    result = diagnostic.read_diagnostic_inputs(
        h5i_path, calendar_path, start=days[0], end=days[2]
    )

    assert result.source_max_day == days[2].replace("-", "")
    assert result.query_bounds["data_end"] == result.source_max_day
    assert result.calendar_days[-1] == days[22].replace("-", "")


def test_changed_calendar_or_queried_rows_fail_revalidation(tmp_path):
    import h5i_db
    import pyarrow as pa

    days = _weekdays("2026-06-01", 23)
    h5i_path = tmp_path / "market.db"
    calendar_path = tmp_path / "calendar.json"
    original_calendar = _write_calendar(calendar_path, days)
    _write_h5i(h5i_path, _rows_for_symbol(days[:4]))
    initial = diagnostic.read_diagnostic_inputs(
        h5i_path, calendar_path, start=days[0], end=days[1]
    )

    calendar_path.write_bytes(original_calendar + b" ")
    with pytest.raises(ValueError, match="calendar.*changed"):
        diagnostic.verify_inputs_unchanged(initial)

    calendar_path.write_bytes(original_calendar)
    initial = diagnostic.read_diagnostic_inputs(
        h5i_path, calendar_path, start=days[0], end=days[1]
    )
    db = h5i_db.Database(str(h5i_path))
    try:
        replacement = pa.Table.from_pydict(
            {
                "ts": [datetime.combine(date.fromisoformat(days[1]), datetime.min.time())],
                "symbol": ["A"],
                "close": [999.0],
                "change_pct": [0.1],
            },
            schema=db.schema("daily_bars"),
        )
        low = int(datetime.combine(date.fromisoformat(days[1]), datetime.min.time()).timestamp() * 1_000_000)
        high = low + 86_400_000_000 - 1
        db.plan_replace_range("daily_bars", low, high, data=replacement).apply()
    finally:
        db.close()

    with pytest.raises(ValueError, match="queried H5i rows.*changed"):
        diagnostic.verify_inputs_unchanged(initial)
