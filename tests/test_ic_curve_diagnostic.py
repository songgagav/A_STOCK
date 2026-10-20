from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ic_curve_diagnostic as diagnostic
from ic_curve_diagnostic import (
    DEFAULT_HORIZONS,
    FACTOR_NAMES,
    build_ic_curves,
    compute_factor_scores,
    compute_forward_labels,
)


def _calendar(start: str, count: int) -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _bars(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["day", "symbol", "close", "change_pct"])


def _factor_score(scores: pd.DataFrame, day: str, factor: str, symbol: str) -> float:
    row = scores.loc[
        (scores.day == day.replace("-", ""))
        & (scores.factor == factor)
        & (scores.symbol == symbol),
        "score",
    ]
    assert len(row) == 1
    return float(row.iloc[0])


def test_factor_window_never_reads_after_signal_day():
    days = _calendar("2026-05-01", 24)
    rows = [(day, "A", 10.0 + i * 0.03, 0.0) for i, day in enumerate(days[:21])]
    baseline, _ = compute_factor_scores(_bars(rows), [days[20]])
    changed_future = _bars(rows + [(days[21], "A", 1_000_000.0, 0.0)])
    changed, _ = compute_factor_scores(changed_future, [days[20]])

    pd.testing.assert_frame_equal(baseline, changed)


def test_present_day_universe_change_does_not_rewrite_historical_scores():
    days = _calendar("2026-05-01", 25)
    historical = [(day, "A", 12.0 + i * 0.02, 0.0) for i, day in enumerate(days[:21])]
    baseline, _ = compute_factor_scores(_bars(historical), [days[20]])
    present_only = [
        (day, "NEW_SYMBOL", 8.0 + i * 0.1, 0.0)
        for i, day in enumerate(days[21:])
    ]
    changed, _ = compute_factor_scores(_bars(historical + present_only), [days[20]])

    pd.testing.assert_frame_equal(baseline, changed)
    assert "NEW_SYMBOL" not in set(changed.symbol)


def test_invalid_or_short_history_is_counted_not_neutral():
    days = _calendar("2026-05-01", 21)
    rows: list[tuple[str, str, float, float]] = []
    rows.extend((day, "GOOD", 10.0 + i * 0.01, 0.0) for i, day in enumerate(days))
    rows.extend((day, "SHORT", 10.0 + i * 0.01, 0.0) for i, day in enumerate(days[1:], 1))
    rows.extend(
        (day, "BAD_HISTORY", (0.0 if i == 0 else 10.0 + i * 0.01), 0.0)
        for i, day in enumerate(days)
    )
    rows.extend((day, "BAD_SIGNAL", (0.0 if i == 20 else 10.0 + i * 0.01), 0.0)
                for i, day in enumerate(days))

    scores, missing = compute_factor_scores(_bars(rows), [days[-1]])

    assert set(scores.symbol) == {"GOOD"}
    assert missing[days[-1].replace("-", "")]["insufficient_history"] == 2
    assert missing[days[-1].replace("-", "")]["invalid_signal_close"] == 1
    assert not scores.score.eq(0.5).any()


def test_lower_vol_and_lower_momentum_map_to_higher_named_scores():
    days = _calendar("2026-05-01", 23)
    rows: list[tuple[str, str, float, float]] = []
    for i, day in enumerate(days):
        rows.extend(
            [
                (day, "LOW_VOL", 100.0 + i + (0.05 if i % 2 else -0.05), 0.0),
                (day, "HIGH_VOL", 100.0 + i + (4.0 if i % 2 else -4.0), 0.0),
                (day, "DOWN", 100.0 - i * 0.7, 0.0),
                (day, "UP", 100.0 + i * 0.7, 0.0),
            ]
        )

    scores, _ = compute_factor_scores(_bars(rows), [days[-1]])
    vol = "vol_selector_score_v1"
    mom = "mom_rev_selector_score_v1"

    assert _factor_score(scores, days[-1], vol, "LOW_VOL") > _factor_score(scores, days[-1], vol, "HIGH_VOL")
    assert _factor_score(scores, days[-1], mom, "DOWN") > _factor_score(scores, days[-1], mom, "UP")
    assert set(scores.factor) == set(FACTOR_NAMES)
    assert DEFAULT_HORIZONS == (1, 3, 5, 10, 20)


def test_factor_scoring_does_not_scan_full_frame_once_per_symbol(monkeypatch):
    days = _calendar("2026-05-01", 22)
    rows = [
        (day, f"S{symbol:03}", 20.0 + symbol + day_index * 0.01, 0.0)
        for symbol in range(30)
        for day_index, day in enumerate(days[:21])
    ]
    prepare_history = getattr(diagnostic, "_positive_history_by_symbol", None)
    assert prepare_history is not None
    calls = []

    def record_prepare(*args, **kwargs):
        calls.append(True)
        return prepare_history(*args, **kwargs)

    monkeypatch.setattr(diagnostic, "_positive_history_by_symbol", record_prepare)

    scores, _ = compute_factor_scores(_bars(rows), [days[20]])

    assert len(scores) == 60
    assert calls == [True]


def test_forward_label_compounds_exactly_next_h_rows():
    days = _calendar("2026-06-01", 4)
    bars = _bars(
        [
            (days[0], "A", 10.0, 99.0),  # t's own return must not enter the label
            (days[1], "A", 11.0, 10.0),
            (days[2], "A", 9.9, -10.0),
            (days[3], "A", 10.395, 5.0),
        ]
    )

    labels, _ = compute_forward_labels(bars, days, [days[0]], days[3], horizons=(1, 3))
    h1 = labels.loc[labels.horizon == 1].iloc[0]
    h3 = labels.loc[labels.horizon == 3].iloc[0]

    assert h1.valid and h1.forward_return == pytest.approx(0.10)
    assert h3.valid and h3.forward_return == pytest.approx(1.10 * 0.90 * 1.05 - 1.0)


def test_missing_or_invalid_future_row_is_not_filled():
    days = _calendar("2026-06-01", 4)
    bars = _bars(
        [
            (days[0], "MISSING", 10.0, 0.0),
            (days[1], "MISSING", 11.0, 1.0),
            (days[3], "MISSING", 12.0, 1.0),  # missing the exact middle due day
            (days[0], "INVALID", 10.0, 0.0),
            (days[1], "INVALID", 11.0, 1.0),
            (days[2], "INVALID", 11.0, -100.0),
            (days[3], "INVALID", 12.0, 1.0),
            (days[0], "NONFINITE", 10.0, 0.0),
            (days[1], "NONFINITE", 11.0, 1.0),
            (days[2], "NONFINITE", 11.0, np.nan),
            (days[3], "NONFINITE", 12.0, 1.0),
        ]
    )

    labels, missing = compute_forward_labels(bars, days, [days[0]], days[3], horizons=(3,))

    assert set(labels.reason) == {"missing_future_bar", "invalid_change_pct"}
    assert labels.forward_return.isna().all()
    assert missing[days[0].replace("-", "")]["h3:missing_future_bar"] == 1
    assert missing[days[0].replace("-", "")]["h3:invalid_change_pct"] == 2


def test_horizon_maturity_requires_source_watermark():
    days = _calendar("2026-06-01", 5)
    bars = _bars(
        [
            (days[0], "A", 10.0, 0.0),
            (days[1], "A", 11.0, 1.0),
            (days[2], "A", 12.0, 1.0),
        ]
    )

    labels, _ = compute_forward_labels(bars, days, [days[0]], days[2], horizons=(1, 3))
    h1 = labels.loc[labels.horizon == 1].iloc[0]
    h3 = labels.loc[labels.horizon == 3].iloc[0]

    assert h1.valid and h1.reason == ""
    assert not h3.valid and pd.isna(h3.forward_return) and h3.reason == "not_mature"


def test_n_forward_valid_counts_only_complete_labels():
    days = _calendar("2026-06-01", 2)
    rows: list[tuple[str, str, float, float]] = []
    for index in range(5):
        symbol = f"S{index}"
        rows.append((days[0], symbol, 10.0 + index, 0.0))
        if index < 4:
            rows.append((days[1], symbol, 11.0 + index, 1.0))
    bars = _bars(rows)
    labels, _ = compute_forward_labels(bars, days, [days[0]], days[1], horizons=(1,))
    scores = pd.DataFrame(
        [
            {"day": days[0].replace("-", ""), "symbol": f"S{i}", "factor": FACTOR_NAMES[0], "score": float(i)}
            for i in range(5)
        ]
    )

    curve = build_ic_curves(scores, labels, [days[0]], horizons=(1,))[FACTOR_NAMES[0]].iloc[0]

    assert int(curve.n_forward_valid) == 4
    assert int(curve.n_missing_forward) == 1
    assert int(curve.n_factor) == 5
    assert int(curve.n_pairs) == 4


def test_spearman_uses_average_tie_ranks():
    day = "20260601"
    symbols = [f"S{i}" for i in range(5)]
    scores = pd.DataFrame(
        [
            {"day": day, "symbol": symbol, "factor": FACTOR_NAMES[0], "score": value}
            for symbol, value in zip(symbols, [1.0, 1.0, 2.0, 3.0, 4.0])
        ]
    )
    labels = pd.DataFrame(
        [
            {"day": day, "horizon": 1, "symbol": symbol, "forward_return": i / 100,
             "valid": True, "reason": ""}
            for i, symbol in enumerate(symbols)
        ]
    )

    ic = build_ic_curves(scores, labels, [day], horizons=(1,))[FACTOR_NAMES[0]].iloc[0].ic

    assert ic == pytest.approx(np.sqrt(0.95))


def test_insufficient_pairs_or_constant_rank_has_null_ic():
    day = "20260601"
    symbols = [f"S{i}" for i in range(5)]
    scores = pd.DataFrame(
        [
            {"day": day, "symbol": f"S{i}", "factor": FACTOR_NAMES[0], "score": float(i)}
            for i in range(4)
        ]
        + [
            {"day": day, "symbol": symbol, "factor": FACTOR_NAMES[1], "score": 0.7}
            for symbol in symbols
        ]
    )
    labels = pd.DataFrame(
        [
            {"day": day, "horizon": 1, "symbol": symbol, "forward_return": i / 100,
             "valid": True, "reason": ""}
            for i, symbol in enumerate(symbols)
        ]
    )

    curves = build_ic_curves(scores, labels, [day], horizons=(1,))

    assert curves[FACTOR_NAMES[0]].iloc[0].status == "insufficient_pairs"
    assert pd.isna(curves[FACTOR_NAMES[0]].iloc[0].ic)
    assert curves[FACTOR_NAMES[1]].iloc[0].status == "constant_rank"
    assert pd.isna(curves[FACTOR_NAMES[1]].iloc[0].ic)


def _bundle_csv(ic="0.1") -> bytes:
    return (
        "day,horizon,ic,status,n_factor,n_forward_valid,n_pairs,n_missing_factor,n_missing_forward\n"
        f"20260601,1,{ic},settled,6,5,5,0,1\n"
        "20260601,3,,not_mature,6,0,0,0,0\n"
    ).encode("utf-8")


def _bundle_manifest() -> dict:
    return {
        "parameters": {"start": "20260601", "end": "20260630", "horizons": [1, 3]},
        "inputs": {
            "h5i_path": "fixture/market.db",
            "calendar_path": "fixture/calendar.json",
            "calendar_sha256": "a" * 64,
            "schema_fingerprint": "b" * 64,
            "queried_rows_sha256": "c" * 64,
        },
        "formulas": {"score": "factor_library.score_factor", "label": "compound next h change_pct"},
        "horizons": [1, 3],
        "source_max_day": "20260701",
        "calendar_due_dates": {"1": "20260602", "3": "20260604"},
        "last_settled_ic_day": None,
        "row_counts": {"vol_selector_score_v1": 2, "mom_rev_selector_score_v1": 2},
        "missingness": {},
        "calendar_provenance": "local_calendar_unknown_upstream",
        "diagnostic_only": True,
        "authoritative": False,
        "observation_eligible": False,
    }


def _write_bundle(tmp_path: Path, *, run_id="d" * 64, csv=None, manifest=None):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    output_root = tmp_path / "outside" / "diagnostics"
    payload = _bundle_csv() if csv is None else csv
    files = {f"{factor}.csv": payload for factor in FACTOR_NAMES}
    path = diagnostic.finalize_diagnostic_bundle(
        output_root,
        repo_root=repo_root,
        run_id=run_id,
        csv_files=files,
        manifest=_bundle_manifest() if manifest is None else manifest,
    )
    return path, files, repo_root, output_root


def test_run_identity_is_canonical_and_binds_all_sources():
    inputs = {
        "code_sha": "1" * 40,
        "calendar_sha256": "2" * 64,
        "schema_fingerprint": "3" * 64,
        "queried_rows_sha256": "4" * 64,
        "source_max_day": "20260701",
    }
    run_id, identity = diagnostic.build_run_identity(
        {"end": "20260630", "start": "20260601"}, **inputs
    )
    reordered_id, _ = diagnostic.build_run_identity(
        {"start": "20260601", "end": "20260630"}, **inputs
    )

    assert run_id == reordered_id
    assert len(run_id) == 64
    assert identity["factor_definition_version"] == diagnostic.FACTOR_DEFINITION_VERSION
    assert run_id == hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    for field, replacement in (
        ("code_sha", "5" * 40),
        ("calendar_sha256", "5" * 64),
        ("schema_fingerprint", "5" * 64),
        ("queried_rows_sha256", "5" * 64),
        ("source_max_day", "20260702"),
    ):
        changed = dict(inputs, **{field: replacement})
        changed_id, _ = diagnostic.build_run_identity(
            {"start": "20260601", "end": "20260630"}, **changed
        )
        assert changed_id != run_id
    changed_params, _ = diagnostic.build_run_identity(
        {"start": "20260602", "end": "20260630"}, **inputs
    )
    assert changed_params != run_id


def test_manifest_has_required_provenance_and_authority_flags(tmp_path):
    bundle, _, _, _ = _write_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["calendar_provenance"] == "local_calendar_unknown_upstream"
    assert manifest["diagnostic_only"] is True
    assert manifest["authoritative"] is False
    assert manifest["observation_eligible"] is False
    assert manifest["csv_sha256"] == {
        f"{factor}.csv": hashlib.sha256((bundle / f"{factor}.csv").read_bytes()).hexdigest()
        for factor in FACTOR_NAMES
    }
    for key in (
        "parameters", "inputs", "formulas", "horizons", "source_max_day",
        "calendar_due_dates", "last_settled_ic_day", "row_counts", "missingness",
    ):
        assert key in manifest


def test_csv_schema_order_dates_nulls_and_hashes_are_exact(tmp_path):
    bundle, files, _, _ = _write_bundle(tmp_path)
    expected_names = {"manifest.json", *(f"{factor}.csv" for factor in FACTOR_NAMES)}

    assert {item.name for item in bundle.iterdir()} == expected_names
    for name, payload in files.items():
        actual = (bundle / name).read_bytes()
        assert actual == payload
        assert hashlib.sha256(actual).hexdigest() == json.loads(
            (bundle / "manifest.json").read_text(encoding="utf-8")
        )["csv_sha256"][name]
        parsed = pd.read_csv(bundle / name, keep_default_na=False, dtype={"day": str, "ic": str})
        assert list(parsed.columns) == [
            "day", "horizon", "ic", "status", "n_factor", "n_forward_valid",
            "n_pairs", "n_missing_factor", "n_missing_forward",
        ]
        assert parsed.day.tolist() == ["20260601", "20260601"]
        assert parsed.ic.tolist() == ["0.1", ""]


@pytest.mark.parametrize("relative", [Path("data"), Path("data/ic/nested")])
def test_output_root_equal_to_or_nested_in_protected_path_is_rejected(tmp_path, relative):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output_root = repo_root / relative
    with pytest.raises(ValueError, match="protected"):
        diagnostic.finalize_diagnostic_bundle(
            output_root,
            repo_root=repo_root,
            run_id="e" * 64,
            csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
            manifest=_bundle_manifest(),
        )
    assert not output_root.exists()


@pytest.mark.parametrize("relative", [Path("."), Path("research-output")])
def test_output_root_inside_repository_is_rejected(tmp_path, relative):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output_root = repo_root / relative

    with pytest.raises(ValueError, match="repository"):
        diagnostic.finalize_diagnostic_bundle(
            output_root,
            repo_root=repo_root,
            run_id="a" * 64,
            csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
            manifest=_bundle_manifest(),
        )

    assert not (repo_root / "research-output").exists()


def test_output_root_symlink_into_repository_is_rejected(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    alias = tmp_path / "repo-alias"
    try:
        alias.symlink_to(repo_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="repository"):
        diagnostic.finalize_diagnostic_bundle(
            alias / "research-output",
            repo_root=repo_root,
            run_id="b" * 64,
            csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
            manifest=_bundle_manifest(),
        )

    assert not (repo_root / "research-output").exists()


def test_output_root_through_junction_or_symlink_is_rejected(tmp_path):
    repo_root = tmp_path / "repo"
    protected = repo_root / "data" / "ic"
    protected.mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(protected, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="protected"):
        diagnostic.finalize_diagnostic_bundle(
            alias / "diagnostics",
            repo_root=repo_root,
            run_id="f" * 64,
            csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
            manifest=_bundle_manifest(),
        )
    assert not (protected / "diagnostics").exists()


def test_ancestor_swap_before_first_directory_write_never_touches_protected(
    tmp_path, monkeypatch
):
    repo_root = tmp_path / "repo"
    protected = repo_root / "data" / "ic"
    protected.mkdir(parents=True)
    safe_parent = tmp_path / "safe-parent"
    safe_parent.mkdir()
    moved_parent = tmp_path / "safe-parent-original"
    output_root = safe_parent / "nested" / "diagnostics"
    original_validate = diagnostic._assert_safe_output_root
    swapped = False

    def validate_then_swap(path, root):
        nonlocal swapped
        result = original_validate(path, root)
        if not swapped:
            safe_parent.rename(moved_parent)
            try:
                safe_parent.symlink_to(protected, target_is_directory=True)
            except OSError as exc:
                moved_parent.rename(safe_parent)
                pytest.skip(f"directory symlink creation is unavailable: {exc}")
            swapped = True
        return result

    monkeypatch.setattr(diagnostic, "_assert_safe_output_root", validate_then_swap)
    try:
        with pytest.raises(ValueError, match="protected"):
            diagnostic.finalize_diagnostic_bundle(
                output_root,
                repo_root=repo_root,
                run_id="d" * 64,
                csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
                manifest=_bundle_manifest(),
            )
        assert swapped
        assert not (protected / "nested" / "diagnostics").exists()
    finally:
        if safe_parent.is_symlink():
            safe_parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(safe_parent)


def test_ancestor_swap_after_directory_anchor_never_writes_through_link(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    protected = repo_root / "data" / "ic"
    protected.mkdir(parents=True)
    safe_parent = tmp_path / "safe-parent"
    safe_parent.mkdir()
    moved_parent = tmp_path / "safe-parent-original"
    output_root = safe_parent / "nested" / "diagnostics"
    original_acquire = diagnostic._acquire_output_directory
    swapped = False
    rename_blocked = False

    def acquire_then_swap(path, **kwargs):
        nonlocal swapped, rename_blocked
        anchor = original_acquire(path, **kwargs)
        if Path(path) == output_root and kwargs.get("add_children") and not swapped:
            try:
                safe_parent.rename(moved_parent)
            except OSError:
                rename_blocked = True
            else:
                try:
                    safe_parent.symlink_to(protected, target_is_directory=True)
                except OSError as exc:
                    moved_parent.rename(safe_parent)
                    anchor.close()
                    pytest.skip(f"directory symlink creation is unavailable: {exc}")
                swapped = True
        return anchor

    monkeypatch.setattr(diagnostic, "_acquire_output_directory", acquire_then_swap)
    try:
        if os.name == "nt":
            bundle = diagnostic.finalize_diagnostic_bundle(
                output_root,
                repo_root=repo_root,
                run_id="e" * 64,
                csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
                manifest=_bundle_manifest(),
            )
            assert rename_blocked
            assert bundle.is_dir()
        else:
            with pytest.raises(ValueError, match="changed after safe directory anchoring"):
                diagnostic.finalize_diagnostic_bundle(
                    output_root,
                    repo_root=repo_root,
                    run_id="e" * 64,
                    csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
                    manifest=_bundle_manifest(),
                )
            assert swapped
        assert not (protected / "nested" / "diagnostics").exists()
    finally:
        if safe_parent.is_symlink():
            safe_parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(safe_parent)


@pytest.mark.parametrize("child", ["ic", "evolution", "daily"])
def test_protected_child_symlink_to_external_output_is_rejected(tmp_path, child):
    repo_root = tmp_path / "repo"
    protected = repo_root / "data" / child
    protected.parent.mkdir(parents=True)
    external_target = tmp_path / "external-target"
    external_target.mkdir()
    try:
        protected.symlink_to(external_target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    output_root = protected / "diagnostics"
    with pytest.raises(ValueError, match="protected"):
        diagnostic.finalize_diagnostic_bundle(
            output_root,
            repo_root=repo_root,
            run_id="c" * 64,
            csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
            manifest=_bundle_manifest(),
        )

    assert not (external_target / "diagnostics").exists()


def test_existing_identical_bundle_is_verified_without_rewrite(tmp_path):
    bundle, files, repo_root, output_root = _write_bundle(tmp_path)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in bundle.iterdir()}
    second = diagnostic.finalize_diagnostic_bundle(
        output_root,
        repo_root=repo_root,
        run_id=bundle.name,
        csv_files=files,
        manifest=_bundle_manifest(),
    )

    assert second == bundle
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in bundle.iterdir()} == before


def test_existing_conflicting_bundle_is_rejected(tmp_path):
    bundle, _, repo_root, output_root = _write_bundle(tmp_path)
    before = {path.name: path.read_bytes() for path in bundle.iterdir()}
    conflicting = {f"{factor}.csv": _bundle_csv("0.2") for factor in FACTOR_NAMES}

    with pytest.raises(ValueError, match="existing bundle.*conflicts"):
        diagnostic.finalize_diagnostic_bundle(
            output_root,
            repo_root=repo_root,
            run_id=bundle.name,
            csv_files=conflicting,
            manifest=_bundle_manifest(),
        )
    assert {path.name: path.read_bytes() for path in bundle.iterdir()} == before


def test_concurrent_destination_collision_never_overwrites(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output_root = tmp_path / "outside"
    run_id = "a" * 64

    def write(value):
        files = {f"{factor}.csv": _bundle_csv(value) for factor in FACTOR_NAMES}
        try:
            return diagnostic.finalize_diagnostic_bundle(
                output_root, repo_root=repo_root, run_id=run_id,
                csv_files=files, manifest=_bundle_manifest(),
            )
        except (FileExistsError, ValueError) as exc:
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("0.1", "0.2")))

    bundle = output_root / run_id
    assert bundle.is_dir()
    assert sum(isinstance(result, Path) for result in results) == 1
    assert sum(isinstance(result, Exception) for result in results) == 1
    winning_ic = (bundle / f"{FACTOR_NAMES[0]}.csv").read_text(encoding="utf-8").splitlines()[1].split(",")[2]
    assert winning_ic in {"0.1", "0.2"}
    assert not list(output_root.glob(f".{run_id}.tmp-*"))


def test_finalize_failure_never_exposes_final_bundle(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output_root = tmp_path / "outside"
    run_id = "b" * 64
    write_file = diagnostic._write_bundle_file_at

    def fail_manifest(directory, name, payload):
        if name == "manifest.json":
            raise OSError("injected manifest write failure")
        write_file(directory, name, payload)

    monkeypatch.setattr(diagnostic, "_write_bundle_file_at", fail_manifest)
    with pytest.raises(OSError, match="injected"):
        diagnostic.finalize_diagnostic_bundle(
            output_root,
            repo_root=repo_root,
            run_id=run_id,
            csv_files={f"{factor}.csv": _bundle_csv() for factor in FACTOR_NAMES},
            manifest=_bundle_manifest(),
        )

    assert not (output_root / run_id).exists()
    assert not list(output_root.glob(f".{run_id}.tmp-*"))
