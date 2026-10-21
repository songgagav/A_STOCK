from __future__ import annotations

import builtins
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_ic_curve_diagnostic as cli
import ic_curve_diagnostic as diagnostic


def _weekdays(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    result = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _previous_weekdays(before: str, count: int) -> list[str]:
    current = date.fromisoformat(before) - timedelta(days=1)
    result = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current -= timedelta(days=1)
    return list(reversed(result))


def _write_fixture(tmp_path: Path, *, eligible=True, calendar_count=21, source_count=None):
    h5i_db = pytest.importorskip("h5i_db", reason="CLI integration fixture requires the Python 3.10 H5i runtime")
    import pyarrow as pa

    start_days = _weekdays("2026-05-04", calendar_count)
    source_count = calendar_count if source_count is None else source_count
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps({"days": start_days}), encoding="utf-8")
    h5i_path = tmp_path / "market.db"
    schema = pa.schema(
        [
            pa.field("ts", pa.timestamp("us")),
            pa.field("symbol", pa.string()),
            pa.field("close", pa.float64()),
            pa.field("change_pct", pa.float64()),
        ]
    )
    db = h5i_db.Database(str(h5i_path), create=True)
    try:
        db.create_table("daily_bars", schema, time_column="ts")
        rows = []
        if eligible:
            symbols = [f"S{i}" for i in range(5)]
            all_days = _previous_weekdays(start_days[0], 20) + start_days[:source_count]
            for symbol_index, symbol in enumerate(symbols):
                base = 10.0 + symbol_index
                for day_index, day in enumerate(all_days):
                    phase = -1.0 if day_index % 2 else 1.0
                    step = phase * (symbol_index + 1) * 0.0007 + (symbol_index - 2) * 0.0001
                    close = base * (1.0 + step) ** day_index
                    change_pct = 0.05 * (symbol_index + 1) if day_index >= 20 else 0.0
                    rows.append((day, symbol, close, change_pct))
        else:
            rows.append((start_days[0], "INVALID", -1.0, 0.0))
        rows.sort(key=lambda row: (row[0], row[1]))
        db.append(
            "daily_bars",
            pa.Table.from_pydict(
                {
                    "ts": [datetime.combine(date.fromisoformat(row[0]), datetime.min.time()) for row in rows],
                    "symbol": [row[1] for row in rows],
                    "close": [row[2] for row in rows],
                    "change_pct": [row[3] for row in rows],
                },
                schema=schema,
            ),
        )
    finally:
        db.close()
    return h5i_path, calendar_path, start_days[0]


def _args(h5i_path: Path, calendar_path: Path, start: str, end: str, output_root: Path):
    return [
        "--h5i-path", str(h5i_path),
        "--calendar-path", str(calendar_path),
        "--start", start,
        "--end", end,
        "--output-root", str(output_root),
    ]


def _install_clean_code_sha(monkeypatch):
    monkeypatch.setattr(cli, "_git_code_sha", lambda _: "1" * 40)


def test_cli_requires_every_explicit_input(capsys):
    result = cli.main([])
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert result != 0
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_arguments"
    assert "--h5i-path" in payload["message"]


def test_cli_success_emits_machine_readable_final_manifest_path(tmp_path, monkeypatch, capsys):
    _install_clean_code_sha(monkeypatch)
    h5i_path, calendar_path, start = _write_fixture(tmp_path)
    output_root = tmp_path / "external-output"

    result = cli.main(_args(h5i_path, calendar_path, start, start, output_root))
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert result == 0
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    assert payload["ok"] is True
    assert Path(payload["manifest_path"]).is_file()
    manifest = json.loads(Path(payload["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["diagnostic_only"] is True
    assert manifest["authoritative"] is False
    assert manifest["observation_eligible"] is False


def test_manifest_eligible_counts_exclude_symbols_without_full_history(tmp_path, monkeypatch, capsys):
    h5i_db = pytest.importorskip(
        "h5i_db", reason="CLI integration fixture requires the Python 3.10 H5i runtime"
    )
    import pyarrow as pa

    _install_clean_code_sha(monkeypatch)
    h5i_path, calendar_path, start = _write_fixture(tmp_path, source_count=1)
    db = h5i_db.Database(str(h5i_path))
    try:
        schema = pa.schema(
            [
                pa.field("ts", pa.timestamp("us")),
                pa.field("symbol", pa.string()),
                pa.field("close", pa.float64()),
                pa.field("change_pct", pa.float64()),
            ]
        )
        db.append(
            "daily_bars",
            pa.Table.from_pydict(
                {
                    "ts": [datetime.combine(date.fromisoformat(start), datetime.min.time())],
                    "symbol": ["SHORT_HISTORY"],
                    "close": [12.0],
                    "change_pct": [0.0],
                },
                schema=schema,
            ),
        )
    finally:
        db.close()
    output_root = tmp_path / "external-output"

    result = cli.main(_args(h5i_path, calendar_path, start, start, output_root))
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    manifest = json.loads(Path(payload["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["eligible_counts_by_day"][start.replace("-", "")] == 5


def test_cli_failure_is_nonzero_and_leaves_no_final_bundle(tmp_path, monkeypatch, capsys):
    _install_clean_code_sha(monkeypatch)
    h5i_path, calendar_path, start = _write_fixture(tmp_path, calendar_count=30, source_count=2)
    end = _weekdays(start, 30)[3]
    output_root = tmp_path / "external-output"

    result = cli.main(_args(h5i_path, calendar_path, start, end, output_root))
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert result != 0
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    assert payload["ok"] is False
    assert payload["error_code"] == "validation_error"
    assert not output_root.exists()


def test_cli_empty_eligible_window_fails_without_final_bundle(tmp_path, monkeypatch, capsys):
    _install_clean_code_sha(monkeypatch)
    h5i_path, calendar_path, start = _write_fixture(tmp_path, eligible=False)
    output_root = tmp_path / "external-output"

    result = cli.main(_args(h5i_path, calendar_path, start, start, output_root))
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert result != 0
    assert payload["ok"] is False
    assert payload["error_code"] == "empty_eligible_window"
    assert not output_root.exists()


def test_cli_rejects_start_before_2026_05_01(tmp_path, monkeypatch, capsys):
    _install_clean_code_sha(monkeypatch)
    output_root = tmp_path / "external-output"

    result = cli.main(
        _args(tmp_path / "missing.db", tmp_path / "missing-calendar.json", "2026-04-30", "2026-05-01", output_root)
    )
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert result != 0
    assert payload["error_code"] == "validation_error"
    assert "2026-05-01" in payload["message"]
    assert not output_root.exists()


def test_input_mutation_during_compute_prevents_finalization(tmp_path, monkeypatch, capsys):
    _install_clean_code_sha(monkeypatch)
    h5i_path, calendar_path, start = _write_fixture(tmp_path)
    original_calendar = calendar_path.read_bytes()
    output_root = tmp_path / "external-output"
    original_compute = diagnostic.compute_factor_scores

    def mutate_after_scoring(*args, **kwargs):
        result = original_compute(*args, **kwargs)
        calendar_path.write_bytes(original_calendar + b" ")
        return result

    monkeypatch.setattr(diagnostic, "compute_factor_scores", mutate_after_scoring)
    finalize_calls = []
    original_finalize = diagnostic.finalize_diagnostic_bundle

    def record_finalize(*args, **kwargs):
        finalize_calls.append(True)
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(diagnostic, "finalize_diagnostic_bundle", record_finalize)
    result = cli.main(_args(h5i_path, calendar_path, start, start, output_root))
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert result != 0
    assert payload["error_code"] == "validation_error"
    assert "calendar bytes changed" in payload["message"]
    assert finalize_calls == []
    assert not output_root.exists()


def test_cli_does_not_call_legacy_refresh_or_any_production_consumer(tmp_path, monkeypatch, capsys):
    _install_clean_code_sha(monkeypatch)
    h5i_path, calendar_path, start = _write_fixture(tmp_path)
    output_root = tmp_path / "external-output"
    forbidden = {
        "ic_curve_refresh", "ic_backtest", "run_daily", "weight_optimizer",
        "premarket_healthcheck", "selector", "realtime_engine", "dashboard",
    }
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".", 1)[0] in forbidden:
            raise AssertionError(f"production consumer import attempted: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = cli.main(_args(h5i_path, calendar_path, start, start, output_root))
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert result == 0
    assert payload["ok"] is True
    assert Path(payload["manifest_path"]).is_file()
