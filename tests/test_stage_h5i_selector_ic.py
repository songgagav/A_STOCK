from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "stage_h5i_selector_ic.py"


def _cli():
    assert SCRIPT.is_file(), "explicit H5i IC staging CLI is not implemented"
    scripts_dir = str(SCRIPT.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("stage_h5i_selector_ic", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(db: Path, calendar: Path, output: Path, start="2026-10-01", end="2026-10-02"):
    return [
        "--h5i-db", str(db), "--calendar-json", str(calendar),
        "--start", start, "--end", end, "--output-root", str(output),
    ]


def _write_calendar(path: Path, *, source="unknown"):
    sessions = pd.bdate_range("2026-09-01", "2026-11-30").strftime("%Y-%m-%d").tolist()
    path.write_text(json.dumps({"days": sessions, "source": source}), encoding="utf-8")
    return sessions


class _FakeSource:
    def __init__(self, path):
        self.path = path
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def schema(self):
        return {
            "daily_bars": ["ts", "symbol", "open", "close", "change_pct"],
            "valuation": ["ts", "symbol", "pb"],
        }

    def trading_sessions(self, start, end):
        return [start, end]


def test_parser_requires_all_explicit_paths_and_dates():
    cli = _cli()
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as error:
        parser.parse_args([])

    assert error.value.code == 2
    assert {"--h5i-db", "--calendar-json", "--start", "--end", "--output-root"}.issubset(
        {action.option_strings[0] for action in parser._actions if action.option_strings}
    )


@pytest.mark.parametrize("start,end", [
    ("2026-10-03", "2026-10-02"),
    ("20261001", "2026-10-02"),
    ("not-a-date", "2026-10-02"),
])
def test_cli_rejects_invalid_or_reversed_date_range(tmp_path, start, end, capsys):
    cli = _cli()
    db = tmp_path / "market.db"
    db.write_bytes(b"read-only fixture")
    calendar = tmp_path / "calendar.json"
    _write_calendar(calendar)
    out = tmp_path / "output"

    result = cli.main(_args(db, calendar, out, start, end))

    assert result != 0
    assert not out.exists()
    message = capsys.readouterr().err.lower()
    assert any(token in message for token in ("date", "range", "iso"))


def test_cli_requires_absolute_input_and_external_output_paths(tmp_path, capsys):
    cli = _cli()
    relative_db = Path("relative-market.db")
    calendar = tmp_path / "calendar.json"
    _write_calendar(calendar)
    out = tmp_path / "output"

    result = cli.main(_args(relative_db, calendar, out))

    assert result != 0
    assert not out.exists()
    assert "absolute" in capsys.readouterr().err.lower()


def test_cli_refuses_missing_h5i_database_and_calendar(tmp_path, capsys):
    cli = _cli()
    db = tmp_path / "missing.db"
    calendar = tmp_path / "missing-calendar.json"
    out = tmp_path / "output"

    result = cli.main(_args(db, calendar, out))

    assert result != 0
    assert not out.exists()
    assert "exist" in capsys.readouterr().err.lower()


def test_cli_refuses_missing_calendar_even_when_h5i_exists(tmp_path, capsys):
    cli = _cli()
    db = tmp_path / "market.db"
    db.write_bytes(b"fixture")
    calendar = tmp_path / "missing-calendar.json"
    out = tmp_path / "output"

    result = cli.main(_args(db, calendar, out))

    assert result != 0
    assert not out.exists()
    assert "calendar" in capsys.readouterr().err.lower()


def test_cli_rejects_repository_protected_output_root_before_opening_h5i(tmp_path, monkeypatch):
    cli = _cli()
    db = tmp_path / "market.db"
    db.write_bytes(b"fixture")
    calendar = tmp_path / "calendar.json"
    _write_calendar(calendar)

    def forbidden_source(_path):
        raise AssertionError("H5i should not be opened for a protected output path")

    monkeypatch.setattr(cli, "H5iReadOnlySource", forbidden_source)
    result = cli.main(_args(db, calendar, REPO))

    assert result != 0


def test_calendar_requires_unique_iso_sessions_and_explicit_provenance(tmp_path):
    cli = _cli()
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"days": ["2026-10-01", "2026-10-01"]}), encoding="utf-8")

    with pytest.raises(cli.CliInputError, match="unique"):
        cli.load_calendar(calendar)

    calendar.write_text(json.dumps({"days": ["2026-10-01"]}), encoding="utf-8")
    sessions, provenance, digest = cli.load_calendar(calendar)
    assert sessions == ["2026-10-01"]
    assert provenance == "unknown"
    assert digest == hashlib.sha256(calendar.read_bytes()).hexdigest()


def test_cli_enforces_bounded_signal_range(tmp_path):
    cli = _cli()
    sessions = pd.bdate_range("2020-01-01", "2026-10-30").strftime("%Y-%m-%d").tolist()
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"trading_days": sessions}), encoding="utf-8")

    with pytest.raises(cli.CliInputError, match="60"):
        cli.validate_signal_range(calendar, "2026-07-01", "2026-10-01")


def test_cli_builds_and_verifies_external_diagnostic_bundle(tmp_path, monkeypatch, capsys):
    cli = _cli()
    db = tmp_path / "market.db"
    db.write_bytes(b"fake read-only H5i identity")
    calendar = tmp_path / "calendar.json"
    sessions = _write_calendar(calendar, source="fixture-exchange-calendar-v1")
    out = tmp_path / "staging"
    sources = []

    def source_factory(path):
        source = _FakeSource(path)
        sources.append(source)
        return source

    factor_values = pd.DataFrame([{"factor": "vol", "value": 0.5}])
    labels = pd.DataFrame([{"horizon": 1, "return": 0.01}])
    ic = pd.DataFrame([
        {"factor": "roe", "horizon": 1, "status": "unsupported", "ic": None},
        {"factor": "mf_net", "horizon": 1, "status": "unsupported", "ic": None},
    ])
    coverage = pd.DataFrame([{"factor": "roe", "horizon": 1, "status": "unsupported"}])
    monkeypatch.setattr(cli, "H5iReadOnlySource", source_factory)
    monkeypatch.setattr(cli, "build_diagnostic_panels", lambda *_a, **_kw: {
        "factor_values": factor_values, "labels": labels, "ic": ic, "coverage": coverage,
    })

    result = cli.main(_args(db, calendar, out))

    assert result == 0, capsys.readouterr().err
    assert sources and sources[0].closed
    output = next(out.iterdir())
    manifest = cli.verify_diagnostic_bundle(output)
    identity = manifest["identity"]
    assert identity["h5i_db_sha256"] == hashlib.sha256(db.read_bytes()).hexdigest()
    assert identity["calendar_sha256"] == hashlib.sha256(calendar.read_bytes()).hexdigest()
    assert identity["calendar_provenance"] == "fixture-exchange-calendar-v1"
    assert identity["calendar_upstream_source_verified"] is False
    assert identity["vol_legacy_parity"].startswith("unresolved")
    assert identity["signal_range"] == ["2026-10-01", "2026-10-02"]
    assert identity["paperbook_fill_replay"] is False
    assert identity["coverage_summary"][0]["factor"] == "roe"
    assert identity["coverage_summary"][0]["status"] == "unsupported"
    assert manifest["diagnostic_only"] is True
    assert manifest["authoritative"] is False
    assert manifest["observation_eligible"] is False
    assert json.loads((output / "factor_contract.json").read_text(encoding="utf-8"))["unsupported"]
    assert (output / "source_schema.json").is_file()
    assert (output / "observed_h5i_sessions.json").is_file()


def test_cli_fails_closed_on_unsupported_schema_without_creating_output(tmp_path, monkeypatch, capsys):
    cli = _cli()
    db = tmp_path / "market.db"
    db.write_bytes(b"fixture")
    calendar = tmp_path / "calendar.json"
    _write_calendar(calendar)
    out = tmp_path / "staging"

    class InvalidSchema(_FakeSource):
        def schema(self):
            raise cli.H5iSchemaError("missing required H5i tables: valuation")

    monkeypatch.setattr(cli, "H5iReadOnlySource", InvalidSchema)

    result = cli.main(_args(db, calendar, out))

    assert result != 0
    assert not out.exists()
    assert "schema" in capsys.readouterr().err.lower() or "valuation" in capsys.readouterr().err.lower()


def test_cli_has_no_production_writer_or_default_production_path_reference():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ic_curve_refresh" not in source
    assert "data/ic" not in source.replace("\\", "/").lower()
    assert "premarket_healthcheck" not in source
    assert "ic_backtest" not in source
