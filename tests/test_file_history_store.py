import json

import pandas as pd
import pytest

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


def test_trade_record_adapter_normalizes_days_and_filters_symbol(tmp_path):
    day_dir = tmp_path / "20261002"
    day_dir.mkdir()
    (day_dir / "trades.json").write_text(
        json.dumps([
            {"time": "09:31:00", "type": "buy", "canon": "600000.SH", "qty": 100, "price": 10.0},
            {"time": "09:32:00", "type": "sell", "canon": "000001.SZ", "qty": 100, "price": 12.0},
        ]),
        encoding="utf-8",
    )

    out = FileHistoryStore(tmp_path).read_trade_records(symbol="600000.SH")

    assert list(out["day"]) == ["2026-10-02"]
    assert list(out["canon"]) == ["600000.SH"]
    assert list(out["qty"]) == [100]


def test_factor_ic_adapter_reads_sorted_bounded_curve(tmp_path):
    ic_dir = tmp_path / "ic"
    ic_dir.mkdir()
    (ic_dir / "ic_curve_roe_k20.csv").write_text(
        "day,ic_h5,ic_h20\n20261002,0.02,0.01\n20261001,0.01,0.00\n20260930,-0.01,-0.02\n",
        encoding="utf-8",
    )

    out = FileHistoryStore(tmp_path, ic_root=ic_dir).read_factor_ic("roe", days=2)

    assert list(out["day"]) == ["20261001", "20261002"]
    assert list(out["ic_h20"]) == [0.0, 0.01]


def test_factor_ic_adapter_rejects_path_traversal_factor_name(tmp_path):
    with pytest.raises(ValueError):
        FileHistoryStore(tmp_path).read_factor_ic("../secrets")


def test_perf_report_adapter_reads_daily_immutable_artifacts(tmp_path):
    day_dir = tmp_path / "20261002"
    day_dir.mkdir()
    (day_dir / "performance_report.json").write_text(
        json.dumps({
            "period": {"end": "2026-10-02"},
            "metrics": {
                "total_return": 0.02,
                "annual_return": 0.12,
                "max_drawdown": -0.03,
                "sharpe_annual": 1.1,
                "calmar": 2.0,
            },
            "benchmark": {"excess_total": 0.01},
        }),
        encoding="utf-8",
    )

    out = FileHistoryStore(tmp_path).read_perf_reports()

    assert list(out.index) == ["20261002"]
    assert out.loc["20261002", "total_return"] == 0.02
    assert out.loc["20261002", "excess_total"] == 0.01
    assert json.loads(out.loc["20261002", "summary_json"])["period"]["end"] == "2026-10-02"


def test_perf_report_adapter_returns_explicit_empty_schema(tmp_path):
    out = FileHistoryStore(tmp_path).read_perf_reports()

    assert out.empty
    assert list(out.columns) == [
        "summary_json",
        "total_return",
        "annual_return",
        "max_drawdown",
        "sharpe_annual",
        "calmar",
        "excess_total",
    ]


def test_reward_curve_adapter_reads_jsonl_bounded_by_training_days(tmp_path):
    drl_dir = tmp_path / "drl" / "20261002"
    drl_dir.mkdir(parents=True)
    (drl_dir / "reward_curve.jsonl").write_text(
        "{\"model_version\": \"m2\", \"step\": 1, \"reward\": 0.2}\n"
        "{\"model_version\": \"m2\", \"step\": 0, \"reward\": 0.1}\n",
        encoding="utf-8",
    )

    out = FileHistoryStore(tmp_path, drl_root=tmp_path / "drl").read_reward_curve()

    assert list(out["day"]) == ["20261002", "20261002"]
    assert list(out["step"]) == [0, 1]
    assert list(out["reward"]) == [0.1, 0.2]
    assert list(out["model_version"]) == ["m2", "m2"]


def test_reward_curve_adapter_does_not_treat_train_meta_as_curve(tmp_path):
    drl_dir = tmp_path / "drl" / "20261002"
    drl_dir.mkdir(parents=True)
    (drl_dir / "train_meta.json").write_text(
        json.dumps({"mean_reward": 0.2}), encoding="utf-8"
    )

    out = FileHistoryStore(tmp_path, drl_root=tmp_path / "drl").read_reward_curve()

    assert out.empty
    assert list(out.columns) == ["day", "model_version", "step", "reward", "weights_json"]
