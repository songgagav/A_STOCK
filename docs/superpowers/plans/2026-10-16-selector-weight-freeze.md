# Selector Weight Freeze Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Each task is independently tested; steps use checkbox syntax.

**Goal:** Make one immutable, hash-verified effective selector-weight snapshot the only live authority in frozen paper/shadow mode, while preserving explicitly configured adaptive and historical behavior.

**Architecture:** Add a small policy/snapshot module that validates modes, resolves live weights with identity, creates immutable content-addressed snapshots, and stores optimizer results as non-authoritative proposals. Wire the resolver through selection, target reuse, signal snapshots, observation identity, and dashboard reporting; keep the generic historical/research APIs and legacy evidence readers compatible.

**Tech Stack:** Python 3.10+, existing atomic JSON helpers, stdlib `hashlib`/`json`/`dataclasses`, pytest, PowerShell service launchers where explicit environment declaration is required.

**Spec:** [2026-10-14-selector-weight-freeze-design.md](../specs/2026-10-14-selector-weight-freeze-design.md)

## Global Constraints

- Preserve `RANK_BY_FUSION=0`, `DRL_PLAN_MODE=shadow`, `FUSION_WEIGHT_MODE=shadow`, `TRADE_BROKER=paper`, `alpha_evidence_status=not_promotable`, and no automatic promotion.
- In `frozen` mode, the digest-selected snapshot is the only live selector-weight authority; invalid or missing identity fails closed without static/dynamic fallback.
- Preserve explicit `adaptive` behavior and historical `as_of=<date>` semantics.
- Do not overwrite/delete `data/weights.json` as an authoritative update in frozen mode; optimizer results are proposal-only; `--reset` must not delete authoritative inputs.
- Do not change scoring, target allocation, risk limits, costs, slippage, rebalance cadence, broker wiring, StockDB/H5i lineage, historical Evidence bundles, or the immutable Oct 11 Epoch.
- Do not modify the deployed `main` worktree, change machine configuration, deploy, freeze an Epoch, count/backfill observation days, or promote anything in this implementation branch.
- Run focused regressions, then applicable core/DRL/h5i/security checks; report skips and unavailable tools separately from passes.

## Review Focus

- A digest containing uppercase, non-hex, path-like, or wrong-length input must be rejected before path construction; exercise malformed digest inputs in Task 1.
- Concurrent or interrupted snapshot creation must never replace an existing digest file or leave a partial authoritative file; exercise create-once/atomic-write behavior in Task 1.
- A stale selection or v1 signal snapshot must not bypass frozen policy identity, while a matching artifact remains reusable; exercise in Tasks 4–5.
- A snapshot/config failure after PaperBook restore must preserve existing holdings and emergency/risk protections while blocking new target-driven rebalance; exercise in Task 4.
- Optimizer proposal failure must remain non-blocking for unrelated daily work and must not mutate either authoritative file; exercise in Task 3.

---

### Task 1: Policy, immutable snapshot, and resolver context

**Files:**
- Create: `src/selector_weight_policy.py`
- Create: `scripts/freeze_selector_weights.py`
- Test: `tests/test_selector_weight_policy.py`

**Interfaces:**
- Produces `SelectorWeightPolicyError(ValueError)`, `SelectorWeightContext(weights: dict[str, float], identity: dict[str, str])`, `selector_weight_mode() -> str`, `resolve_selector_weight_context(*, as_of: str | None = None) -> SelectorWeightContext`, and `create_frozen_snapshot(repo_root: str, *, captured_at: datetime | None = None) -> dict[str, Any]`. Task 3 adds `persist_optimizer_weights(day: str, optimizer_result: Mapping[str, Any]) -> dict[str, Any]` to the same module.
- The schema-v1 snapshot contains exactly the nine keys in the spec, canonical UTF-8 JSON, provenance, and a digest computed over the exact stored bytes. Frozen selector context identity is `{"mode": "frozen", "snapshot_sha256": <lowercase digest>}`.

- [ ] **Step 1: Write failing tests** — `test_frozen_snapshot_accepts_nine_factor_vector_with_zero`, `test_frozen_snapshot_rejects_malformed_weights_and_hash`, `test_missing_or_invalid_mode_fails_closed`, `test_snapshot_creation_is_create_only_canonical_and_provenanced`, and `test_failed_snapshot_write_leaves_no_authoritative_file`. Assert the provenance includes timestamp/timezone, current `weights.json` SHA, and factor-health input hashes.
- [ ] **Step 2: Run `pytest tests/test_selector_weight_policy.py -q`** — expected: fail because the policy module and snapshot resolver do not exist.
- [ ] **Step 3: Implement strict mode/digest parsing, canonical serialization, hash-verified snapshot loading, create-once atomic creation, provenance capture, and the context API.** Snapshot writes must not overwrite an existing digest path; clean up a newly created partial file on failure.
- [ ] **Step 4: Run `pytest tests/test_selector_weight_policy.py -q`** — expected: all tests pass, with the existing snapshot bytes unchanged after a repeated create attempt.
- [ ] **Step 5: Commit** — `feat(weights): add immutable selector weight policy`.

### Task 2: Live selector integration and historical isolation

**Files:**
- Modify: `src/factor_library.py`
- Modify: `src/selector.py`
- Test: `tests/test_factor_health.py`
- Test: `tests/test_rank_switch.py`
- Create: `tests/test_selector_weight_freeze.py`

**Interfaces:**
- Consumes Task 1's `resolve_selector_weight_context()` and policy error.
- `factor_library.selector_weights(as_of=None)` remains dict-returning; `RotationSelector.select()` adds `selector_weight_identity` from the same context read that supplied its weights. `select_as_of()` remains historical and does not read the live snapshot.

- [ ] **Step 1: Write `test_frozen_selector_ignores_dynamic_weights_and_factor_health`, `test_invalid_frozen_policy_never_returns_static_fallback`, `test_historical_as_of_keeps_point_in_time_weights`, and `test_live_selection_records_resolved_weight_identity`.** Vary dynamic/IC inputs; frozen weights and identity stay constant; malformed frozen inputs raise; historical as-of still uses point-in-time health; selection metadata matches the exact resolved context.
- [ ] **Step 2: Run `pytest tests/test_selector_weight_freeze.py -q`** — expected: fail because the live resolver still uses dynamic weights/health and selection artifacts omit the identity.
- [ ] **Step 3: Integrate the Task 1 context at the selector boundary and add identity to live selection artifacts.** Preserve score/target logic and the legacy dict API.
- [ ] **Step 4: Run `pytest tests/test_selector_weight_freeze.py tests/test_factor_health.py tests/test_rank_switch.py -q`** — expected: all focused tests pass, including historical cases.
- [ ] **Step 5: Commit** — `feat(selector): bind live selections to frozen weights`.

### Task 3: Shared optimizer persistence and proposal-only output

**Files:**
- Modify: `src/selector_weight_policy.py`
- Modify: `src/weight_optimizer.py`
- Modify: `src/run_daily.py`
- Test: `tests/test_phase_b_strategy_contracts.py`
- Create: `tests/test_weight_optimizer_policy.py`

**Interfaces:**
- Both CLI and daily feedback use Task 1's `persist_optimizer_weights(day, optimizer_result)`; its return identifies `mode`, `status`, `path`, `sha256`, and whether output is authoritative.

- [ ] **Step 1: Write `test_frozen_optimizer_persists_proposal_without_touching_weights_json`, `test_frozen_reset_is_rejected_without_deleting_inputs`, `test_adaptive_optimizer_write_preserves_weights_json_contract`, and `test_proposal_write_failure_is_nonblocking_in_daily_feedback`.** In frozen mode assert proposal path/hash/status and unchanged sentinel bytes in both authoritative inputs; reset returns nonzero; adaptive retains its contract; daily feedback records the error and continues.
- [ ] **Step 2: Run `pytest tests/test_weight_optimizer_policy.py -q`** — expected: fail because daily and CLI paths directly write `WEIGHTS_FILE`.
- [ ] **Step 3: Add the shared persistence helper and route daily and CLI persistence through it.** Preserve daily feedback's non-blocking exception behavior and never label proposal weights as effective weights.
- [ ] **Step 4: Run `pytest tests/test_weight_optimizer_policy.py tests/test_phase_b_strategy_contracts.py -q`** — expected: all focused tests pass.
- [ ] **Step 5: Commit** — `fix(weights): make frozen optimizer output proposal-only`.

### Task 4: Frozen identity through target reuse and runtime rebalance gate

**Files:**
- Modify: `src/realtime_engine.py`
- Modify: `src/selector.py` (only if selection artifact serialization needs a narrow adjustment)
- Test: `tests/test_plan_consume_day.py`
- Test: `tests/test_signal_freeze_engine.py`
- Create: `tests/test_frozen_weight_target_gate.py`

**Interfaces:**
- Consumes Task 1 context and Task 2 selection identity. Existing adaptive/DRL behavior is unchanged; the current DRL shadow contract remains authoritative.

- [ ] **Step 1: Write target-loader and runtime-gate tests** — `test_matching_frozen_selection_identity_is_reusable`, `test_legacy_or_mismatched_selection_identity_is_skipped`, `test_weight_gate_is_independent_of_signal_freeze_mode`, `test_invalid_frozen_identity_blocks_new_rebalance_and_preserves_book`, and `test_frozen_policy_failure_is_visible_in_health_and_logs`. Assert DRL plans never substitute, holdings/cash remain unchanged, and independent emergency/risk checks remain callable.
- [ ] **Step 2: Run `pytest tests/test_frozen_weight_target_gate.py -q`** — expected: fail because current `load_targets()` accepts unidentified cached selections and has no independent readiness gate.
- [ ] **Step 3: Add the independent selector-weight readiness gate and identity checks to `load_targets()`/runtime state.** Do not liquidate valid positions due solely to snapshot failure; do not auto-recover in the same session—recovery requires an explicit normal identity preflight after correcting the snapshot/config. Report the failure in the runtime receipt and logs without masking emergency/risk controls.
- [ ] **Step 4: Run `pytest tests/test_frozen_weight_target_gate.py tests/test_plan_consume_day.py tests/test_signal_freeze_engine.py -q`** — expected: all focused tests pass.
- [ ] **Step 5: Commit** — `fix(realtime): reject targets without frozen weight identity`.

### Task 5: Signal snapshot schema v2 binding

**Files:**
- Modify: `src/signal_snapshot.py`
- Modify: `src/realtime_engine.py`
- Test: `tests/test_signal_snapshot.py`
- Test: `tests/test_runtime_provenance_receipt.py`
- Test: `tests/test_signal_freeze_engine.py`

**Interfaces:**
- `build_snapshot(..., selector_weight_identity: Mapping[str, str] | None = None)` emits schema v2 when identity is supplied and includes identity in both input payload and integrity hash.
- `read_snapshot(data_dir, day, *, expected_selector_weight_identity: Mapping[str, str] | None = None)` retains schema-v1 reads for adaptive mode, but frozen mode requires exact v2 identity equality.

- [ ] **Step 1: Write `test_snapshot_v2_hash_binds_weight_identity`, `test_v1_snapshot_remains_readable_for_adaptive_mode`, and `test_v1_or_tampered_identity_cannot_authorize_frozen_mode`** — changing mode/SHA invalidates hashes; legacy v1 reads remain compatible; frozen expected identity rejects v1 or mismatched v2.
- [ ] **Step 2: Run `pytest tests/test_signal_snapshot.py -q`** — expected: fail because current snapshots are schema v1 and have no policy identity.
- [ ] **Step 3: Implement the optional v2 identity contract and pass the active identity from realtime freeze/read paths.** Keep v1 validation unchanged for legacy adaptive readers.
- [ ] **Step 4: Run `pytest tests/test_signal_snapshot.py tests/test_runtime_provenance_receipt.py tests/test_signal_freeze_engine.py -q`** — expected: focused suite passes.
- [ ] **Step 5: Commit** — `feat(evidence): bind signal snapshots to selector weights`.

### Task 6: Observation epoch and evidence identity

**Files:**
- Modify: `src/observation_epoch.py` only if the observation-specific validator needs a canonical helper not already exposed
- Modify: `src/evidence_bundle.py`
- Modify: `src/oos_dataset.py`
- Modify: `src/run_daily.py`
- Modify: `src/realtime_engine.py`
- Create: `scripts/freeze_observation_epoch.py`
- Test: `tests/test_observation_epoch.py`
- Test: `tests/test_evidence_bundle.py`
- Test: `tests/test_oos_dataset.py`

**Interfaces:**
- Add an observation-specific validator requiring `experiment_identity.selector_weight_mode == "frozen"` and a valid `selector_weights_snapshot_sha256`; do not narrow the generic `build_observation_epoch()` contract for research fixtures.
- `freeze_observation_epoch(record_root, epoch) -> Path` verifies the epoch then creates `data/evolution/epochs/<epoch_id>.json` without overwrite; the Oct 11 record path is never targeted.

- [ ] **Step 1: Write `test_weight_mode_or_snapshot_digest_changes_epoch_id`, `test_observation_freeze_requires_valid_frozen_identity`, and `test_epoch_record_is_create_only_without_breaking_legacy_verification`.** Also assert Evidence Bundle, OOS dataset, and daily runtime receipts preserve the exact experiment identity.
- [ ] **Step 2: Run `pytest tests/test_observation_epoch.py tests/test_evidence_bundle.py tests/test_oos_dataset.py -q`** — expected: fail because observation identity validation and create-only freeze command do not exist.
- [ ] **Step 3: Implement the observation-specific identity validator, evidence identity propagation, and create-only epoch command.** Compute `experiment_hash` over the canonical experiment payload excluding `experiment_hash` itself.
- [ ] **Step 4: Run `pytest tests/test_observation_epoch.py tests/test_evidence_bundle.py tests/test_oos_dataset.py -q`** — expected: all focused tests pass, including legacy fixtures.
- [ ] **Step 5: Commit** — `feat(evidence): bind observation epochs to selector weights`.

### Task 7: Dashboard visibility and explicit launcher modes

**Files:**
- Modify: `src/dashboard.py`
- Modify: `ops/start_daemon.ps1`
- Modify: `ops/install_daemon_service.ps1`
- Modify: root `conftest.py` only to declare adaptive mode for the test process
- Create: `tests/test_selector_weight_dashboard.py`

**Interfaces:**
- `/api/weights` preserves `weights` as effective weights; in frozen mode it returns `mode`, `snapshot_sha256`, `effective_source="frozen_snapshot"`, and a separate `proposal` field. Invalid snapshot returns `ok=false`.
- Launchers/test harness declare `SELECTOR_WEIGHT_MODE` explicitly; the resolver itself has no default. The two daemon scripts require a validated mode parameter; the service installer includes it in NSSM `AppEnvironmentExtra`; the pytest harness explicitly declares `adaptive`.

- [ ] **Step 1: Write `test_dashboard_reports_frozen_effective_weights_and_proposal_separately`, `test_dashboard_returns_not_ok_for_invalid_frozen_snapshot`, and `test_selector_weight_mode_must_be_explicit`** — frozen endpoint reports snapshot weights separately from proposal; invalid snapshot returns `ok=false`; missing/unknown mode fails closed; existing adaptive display remains compatible.
- [ ] **Step 2: Run the tests** — expected: fail because dashboard reads only `weights.json` and launch/test environments do not declare the policy.
- [ ] **Step 3: Implement dashboard resolution and explicit mode declaration in both daemon launch paths and pytest setup.** Do not silently default the resolver to adaptive.
- [ ] **Step 4: Run `pytest tests/test_selector_weight_dashboard.py tests/test_selector_weight_policy.py -q` and PowerShell parser/check-only probes for both launcher scripts** — expected: focused tests and safe launcher probes pass; no service is installed, started, or restarted.
- [ ] **Step 5: Commit** — `feat(dashboard): expose effective frozen selector weights`.

### Task 8: Full verification, fresh review, and branch upload

**Files:** all branch changes; no runtime data files.

**Interfaces:**
- Consumes all completed tasks; preserves the branch's feature-only boundary.

- [ ] **Step 1: Run `pytest tests/test_selector_weight_policy.py tests/test_selector_weight_freeze.py tests/test_weight_optimizer_policy.py tests/test_frozen_weight_target_gate.py tests/test_signal_snapshot.py tests/test_observation_epoch.py tests/test_evidence_bundle.py tests/test_oos_dataset.py tests/test_selector_weight_dashboard.py -q`.** Expected: zero failures; report skips distinctly.
- [ ] **Step 2: Run CI-equivalent checks** — `.venv314\Scripts\python.exe -m pytest tests -q` with `CI_TEST_PARTITION=core`; `.venv310\Scripts\python.exe -m pytest tests -q` with `CI_TEST_PARTITION=drl`; `.venv310\Scripts\python.exe -m pytest tests/test_baostock_backfill.py tests/test_h5i_accel_equivalence.py -q -rs`; then the repository's configured security workflow on the pushed SHA. Expected: applicable checks pass; unavailable checks are reported `NOT_RUN`, not inferred.
- [ ] **Step 3: Verify protected-path diff is empty** for broker wiring, PaperBook execution semantics, risk/cost/slippage, live data, production config, and historical Epoch/evidence; run `git diff --check` and inspect complete branch diff against merge-base `main`.
- [ ] **Step 4: Dispatch one fresh-context whole-branch review using the approved plan/spec and review focus.** Fix Critical/Important findings with RED→GREEN tests and a final green suite; ledger deferred minors.
- [ ] **Step 5: Commit any test-backed Critical/Important review fix; if none are found, make no review-fix commit. Push only `feature/freeze-selector-weights` using expected-old-SHA protection, then verify local/remote tree SHA and GitHub checks.** Never merge or deploy in this task.
- [ ] **Step 6: Final report** — exact local/remote SHA, checks, review findings, protected-path no-diff, and explicit remaining deployment/new-Epoch/observation gates.
