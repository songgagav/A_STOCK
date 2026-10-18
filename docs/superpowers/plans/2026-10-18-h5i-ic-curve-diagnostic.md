# H5i technical-factor IC diagnostic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Every task uses checkbox syntax, writes tests before implementation, and ends with a focused verification and commit.

**Goal:** Build a reproducible offline PIT diagnostic for the selector's low-volatility and reverse-momentum scores, with immutable output kept separate from all authoritative IC, weight, runtime, and observation artifacts.

**Architecture:** Keep all computation and input/output validation in a research-only module, with a thin standalone CLI as the only entry point. Read H5i and the explicitly supplied local calendar in read-only mode; write a content-addressed diagnostic bundle only under an operator-supplied output root. Do not import this producer from daily, health, optimizer, selector, realtime, or dashboard paths.

**Tech Stack:** Python 3.10+, existing NumPy/pandas, h5i_db 0.1.6+, pytest, stdlib argparse/hashlib/json/pathlib/tempfile. No new runtime dependency.

**Spec:** [2026-10-18-h5i-ic-curve-diagnostic-design.md](../specs/2026-10-18-h5i-ic-curve-diagnostic-design.md)

## Global Constraints

- V1 accepts signal dates only from 2026-05-01 onward and requires explicit --h5i-path, --calendar-path, --start, --end, and --output-root.
- Open H5i with h5i_db.Database(path, read_only=True); query daily_bars only; never fall back to another source.
- The sample is an all-bar-eligible diagnostic universe, not the live selector's historical universe or a claim of full selector parity.
- Factor names are vol_selector_score_v1 and mom_rev_selector_score_v1; use factor_library.score_factor("vol"|"mom", bars_as_of_t), with higher scores hypothesized favorable.
- H5i close adjustment basis is as_stored_unverified. Forward labels compound exactly the next 1, 3, 5, 10, or 20 listed trading-day change_pct values.
- Use Spearman correlation with average ranks, at least five paired symbols, and null IC for immature, insufficient, or constant-rank samples.
- Write only to a new immutable bundle under the explicit output root; manifest flags are diagnostic_only=true, authoritative=false, and observation_eligible=false.
- Preserve RANK_BY_FUSION=0, DRL_PLAN_MODE=shadow, FUSION_WEIGHT_MODE=shadow, TRADE_BROKER=paper, and Alpha not_promotable.
- Do not modify the old IC producer/consumer, five-factor health gate, data/ic, data/weights.json, frozen selector snapshot, service, production state, existing Evidence, or Observation Epoch; do not qualify, backfill, deploy, or promote an observation day.
- Report PASS, FAIL, SKIP, and NOT_RUN separately. A skipped H5i check is not a pass.

## Review Focus

- A listed past trading day with no H5i bars, or an end date beyond the source watermark, must fail closed rather than shorten the label horizon; test both in Task 2.
- Duplicate day/symbol keys, schema drift, malformed or non-monotonic calendars, and non-finite or invalid close/change_pct values must be rejected or counted by their specified cause; test in Tasks 1–2.
- A future/unsettled label, a symbol missing any one of the h future rows, or a change_pct at or below -100 must never become a shortened or zero-filled return; test in Task 1.
- An output root equal to or nested under a protected live-artifact path, including through a resolved junction/symlink, must be rejected before any write; test in Task 3.
- Repeated runs, destination races, and interrupted writes must never overwrite a finalized bundle or expose a partial final directory; test in Task 3.

---

### Task 1: PIT factors, forward labels, and IC rows

**Files:**
- Create: src/ic_curve_diagnostic.py
- Create: tests/test_ic_curve_diagnostic.py

**Interfaces:**
- Produces DEFAULT_HORIZONS=(1, 3, 5, 10, 20), FACTOR_NAMES=("vol_selector_score_v1", "mom_rev_selector_score_v1"), compute_factor_scores(bars: pandas.DataFrame, signal_days: Sequence[str]) -> tuple[pandas.DataFrame, dict], compute_forward_labels(bars: pandas.DataFrame, calendar_days: Sequence[str], signal_days: Sequence[str], source_max_day: str, horizons: Sequence[int] = DEFAULT_HORIZONS) -> tuple[pandas.DataFrame, dict], and build_ic_curves(factor_scores: pandas.DataFrame, forward_labels: pandas.DataFrame, signal_days: Sequence[str], horizons: Sequence[int] = DEFAULT_HORIZONS) -> dict[str, pandas.DataFrame].
- Factor-score rows contain day, symbol, factor, and score. Forward-label rows contain day, horizon, symbol, nullable forward_return, and an explicit validity/reason field. Each curve uses exactly day,horizon,ic,status,n_factor,n_forward_valid,n_pairs,n_missing_factor,n_missing_forward.
- Factor missingness counts are keyed by day and reason. CSV status is settled, not_mature, insufficient_pairs, or constant_rank; null IC is represented as a null value.

- [ ] Step 1: Write tests named test_factor_window_never_reads_after_signal_day, test_present_day_universe_change_does_not_rewrite_historical_scores, test_invalid_or_short_history_is_counted_not_neutral, test_lower_vol_and_lower_momentum_map_to_higher_named_scores, test_forward_label_compounds_exactly_next_h_rows, test_missing_or_invalid_future_row_is_not_filled, test_horizon_maturity_requires_source_watermark, test_n_forward_valid_counts_only_complete_labels, test_spearman_uses_average_tie_ranks, and test_insufficient_pairs_or_constant_rank_has_null_ic. Use synthetic bars/calendar only.
- [ ] Step 2: Run & ..\..\.venv310\Scripts\python.exe -m pytest tests/test_ic_curve_diagnostic.py -q; expected: pytest collection fails with ModuleNotFoundError for ic_curve_diagnostic because the module/API has not been created.
- [ ] Step 3: Implement the pure calculations. For each symbol and signal day, use only finite positive closes on or before that day and pass the last 21 ordered closes to factor_library.score_factor; never encode an insufficient history as the factor library's neutral 0.5. Label from the exact next h calendar days, require every finite change_pct > -100, and use average rank before Pearson correlation.
- [ ] Step 4: Run the focused test file; expected: all tests pass, including exact H1/H20 maturity and missingness counts.
- [ ] Step 5: Commit as feat(research): add PIT technical factor IC calculations.

### Task 2: Read-only H5i snapshot and explicit calendar contract

**Files:**
- Modify: src/ic_curve_diagnostic.py
- Create: tests/test_ic_curve_diagnostic_h5i.py
- Modify: .github/workflows/ci.yml

**Interfaces:**
- Produces DiagnosticInputs(bars, calendar_days, source_max_day, calendar_sha256, schema_fingerprint, queried_rows_sha256, source/calendar paths, and query bounds), read_diagnostic_inputs(h5i_path: Path, calendar_path: Path, *, start: str, end: str) -> DiagnosticInputs, and verify_inputs_unchanged(inputs: DiagnosticInputs) -> None.
- Load only daily_bars through h5i_db.Database(str(h5i_path), read_only=True). Read the last 20 finite positive pre-start closes per symbol plus rows from start through the 20th listed future trading day after end, capped at the source max; do not load full history as a fallback. Query the full-table max date only as a watermark aggregate.
- Read the calendar's days array from the exact supplied bytes, retain its byte SHA-256, require strictly increasing valid dates, and use only weekdays in the requested/future horizon. Record calendar provenance as local_calendar_unknown_upstream; it is not exchange-authoritative.

- [ ] Step 1: Write tests test_read_only_h5i_window_preserves_source_bytes, test_h5i_query_is_bounded_and_includes_twenty_pre_start_valid_closes, test_end_after_source_max_fails_closed, test_duplicate_day_symbol_fails_closed, test_daily_bars_schema_mismatch_fails_closed, test_calendar_sha_schema_fingerprint_and_row_digest_are_deterministic, test_malformed_duplicate_or_non_monotonic_calendar_fails_closed, test_calendar_must_cover_max_future_due_day, test_past_calendar_day_without_any_h5i_bars_is_a_gap, test_future_calendar_day_after_source_max_is_not_a_gap, and test_changed_calendar_or_queried_rows_fail_revalidation. Build all H5i databases and calendar files under tmp_path.
- [ ] Step 2: Run & ..\..\.venv310\Scripts\python.exe -m pytest tests/test_ic_curve_diagnostic_h5i.py -q; expected: the contract tests fail on the missing loader or unsafe/default behavior.
- [ ] Step 3: Implement explicit read-only database access, required-column/schema fingerprint checks, bounded row query, deterministic symbol/date normalization, duplicate rejection, source watermark, sorted queried-row digest, and calendar-byte hashing. Reject --end later than the source max; distinguish absent past source dates from not-yet-observed future dates. Revalidation must reopen H5i read-only and compare the calendar SHA, schema fingerprint, source max, and bounded row digest captured by the initial read.
- [ ] Step 4: Add the new H5i test file to the regression-h5i command in .github/workflows/ci.yml, then run the focused pure and H5i test files in .venv310; expected: all pass without touching the repository's production H5i database or calendar.
- [ ] Step 5: Commit as feat(research): add read-only H5i diagnostic inputs.

### Task 3: Content-addressed manifest and immutable bundle writer

**Files:**
- Modify: src/ic_curve_diagnostic.py
- Modify: tests/test_ic_curve_diagnostic.py

**Interfaces:**
- Produces build_run_identity(parameters: Mapping[str, Any], *, code_sha: str, calendar_sha256: str, schema_fingerprint: str, queried_rows_sha256: str, source_max_day: str) -> tuple[str, dict[str, Any]] and finalize_diagnostic_bundle(output_root: Path, *, repo_root: Path, run_id: str, csv_files: Mapping[str, bytes], manifest: Mapping[str, Any]) -> Path.
- Final bundle path is output-root/run_id and contains vol_selector_score_v1.csv, mom_rev_selector_score_v1.csv, and manifest.json. Rows are unique and sorted by day,horizon; CSV dates use YYYYMMDD and null IC cells are empty.
- The manifest records parameters/input identities/formulas/horizons/source max/calendar due dates/last settled day/row and missingness counts/CSV SHA-256 values and the three required diagnostic authority flags. It records calendar provenance as local_calendar_unknown_upstream, not exchange-authoritative. It contains no nondeterministic timestamp that would make an identical run differ.

- [ ] Step 1: Write tests test_run_identity_is_canonical_and_binds_all_sources, test_manifest_has_required_provenance_and_authority_flags, test_csv_schema_order_dates_nulls_and_hashes_are_exact, test_output_root_equal_to_or_nested_in_protected_path_is_rejected, test_output_root_through_junction_or_symlink_is_rejected, test_existing_identical_bundle_is_verified_without_rewrite, test_existing_conflicting_bundle_is_rejected, test_concurrent_destination_collision_never_overwrites, and test_finalize_failure_never_exposes_final_bundle.
- [ ] Step 2: Run the named tests; expected: fail because identity/writer APIs do not exist.
- [ ] Step 3: Implement canonical SHA-256 identity over canonical parameters, code SHA, factor-definition version, calendar SHA, H5i schema fingerprint, sorted queried-row digest, and source max. Resolve paths before enforcing protected-path rejection. Write CSVs into a temporary sibling directory, write manifest last, then finalize with a create-only atomic rename; an existing destination is accepted only after byte-for-byte verification.
- [ ] Step 4: Run the bundle tests and repeat an identical write; expected: identical finalized bytes and hashes, no replacement, no staging directory exposed as the final bundle.
- [ ] Step 5: Commit as feat(research): finalize immutable IC diagnostic bundles.

### Task 4: Explicit CLI orchestration

**Files:**
- Create: scripts/build_ic_curve_diagnostic.py
- Create: tests/test_ic_curve_diagnostic_cli.py
- Modify: src/ic_curve_diagnostic.py

**Interfaces:**
- Produces main(argv: Sequence[str] | None = None) -> int. Require --h5i-path, --calendar-path, --start, --end, and --output-root; no implicit source, date, or output default.
- On success print one JSON object with ok=true and the finalized manifest path, then return 0. On validation/runtime error print one JSON object with ok=false and a stable error code/message, then return nonzero. Do not print success until atomic finalization completes.

- [ ] Step 1: Write test_cli_requires_every_explicit_input, test_cli_success_emits_machine_readable_final_manifest_path, test_cli_failure_is_nonzero_and_leaves_no_final_bundle, test_cli_empty_eligible_window_fails_without_final_bundle, test_cli_rejects_start_before_2026_05_01, test_input_mutation_during_compute_prevents_finalization, and test_cli_does_not_call_legacy_refresh_or_any_production_consumer.
- [ ] Step 2: Run tests/test_ic_curve_diagnostic_cli.py -q; expected: fail because the entry point and orchestration do not exist.
- [ ] Step 3: Implement a thin argparse entry point that composes the prior task interfaces, bounds signal dates to 2026-05-01 or later, and calls verify_inputs_unchanged immediately before finalization. Keep all paths explicit and keep the module unreachable from daily/health/optimizer/selector/realtime/dashboard code.
- [ ] Step 4: Run all three focused diagnostic test files; expected: all pass and CLI failure paths leave no finalized directory.
- [ ] Step 5: Commit as feat(research): add explicit IC diagnostic CLI.

### Task 5: Bounded real-data proof, protected-path audit, and branch handoff

**Files:**
- No production files; diagnostic output only under an operator-supplied external output root.

**Interfaces:**
- Consumes the completed CLI and returns a finalized manifest path plus a concise difference report against existing legacy curves. The report is diagnostic only and cannot update health, optimizer, selector, or observation state.

- [ ] Step 1: Run the focused diagnostic tests in .venv310, then the applicable full local regression suite; report PASS/FAIL/SKIP/NOT_RUN separately.
- [ ] Step 2: From the clean committed code SHA, run one bounded real-H5i diagnostic using explicit inputs and an output root outside all protected live-artifact directories. Before and after, compare a sorted filename-plus-file-SHA digest of data/ic and exact byte hashes of data/weights.json, the frozen selector snapshot, and the existing Epoch; require them unchanged. Do not run a producer, daily pipeline, service, or backfill.
- [ ] Step 3: Review the difference report only for sample coverage, factor direction, dates, and maturity; do not claim parity, repair the five-factor health gate, alter weights, or qualify an observation day.
- [ ] Step 4: Inspect the complete branch diff and git diff --check. Require no changes to the protected files and no imports/hooks from production consumers. Obtain a fresh-context whole-branch review and fix only test-backed Critical/Important findings.
- [ ] Step 5: Commit the final test-backed review fix if any and upload only the isolated feature branch. Do not merge, deploy, touch the daemon, freeze an Epoch, or start observation counting in this phase.
- [ ] Step 6: Verify the uploaded branch contents, run the h5i integration job including the new test file, and wait for all required CI/security checks on the pushed SHA; report any unavailable check as NOT_RUN.

## Execution Gate

This plan is not implementation authorization. After reviewing it, the user must approve execution and choose an execution approach. Implementation begins in a dedicated isolated feature worktree based on the verified current remote main plus the approved spec and plan; the deployed primary checkout remains untouched.
