# 09:25 Signal Freeze Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the 09:25 target snapshot the verified, auditable decision input for the paper-trading engine, while preserving late results for manual next-day review.

**Architecture:** A small `signal_snapshot` module owns canonical serialization, SHA-256 integrity checks, snapshot storage, late/review artifacts, and retention. `realtime_engine` remains the only automatic decision consumer and freezes or reads targets according to a versioned mode control; `PriceFeed` receives a verified snapshot target set only in enforced paper mode. The existing `signal_freeze_watch` hash-chain ledger remains the common audit sink.

**Tech Stack:** Python 3.10+, standard library (`datetime`, `zoneinfo`, `hashlib`, `json`, `pathlib`), existing `utils.atomic_write_json`, pytest, PaperBook virtual trading.

**Spec:** `docs/evolution/signal-freeze-design.md`

## Global Constraints

- All time comparisons use `ZoneInfo("Asia/Shanghai")`; serialized snapshots also include UTC.
- Canonical JSON is UTF-8, `ensure_ascii=False`, `sort_keys=True`, `separators=(",", ":")`; all integrity hashes use SHA-256.
- No new third-party dependencies and no broker integration; `TRADE_BROKER != "paper"` continues to reject orders.
- L1 missing, L2 invalid, and L3 tampered snapshots are fail-closed for enforced paper trading; same-day automatic recovery is forbidden.
- Default mode is `shadow`; only a named `config/signal_freeze_mode.json` promotion record can enable `enforce` after five clean trading days.
- Preserve user-owned `_gap.txt`, `_gen.txt`, and `_val.txt`; never add them to commits.
- Use `utils.atomic_write_json` for every mutable JSON artifact. Retention only deletes matching freeze artifacts after a newly written snapshot validates.

## Review Focus

- Non-finite floats or non-JSON values must fail snapshot construction rather than yield a platform-dependent hash; Task 1 adds the rejection test.
- An artifact path outside the repository root must never be serialized as a trusted source artifact; Task 1 adds the relative-path rejection test.
- UTC CI and Shanghai development hosts must produce the same canonical timestamp pair for an injected instant; Task 1 adds the time-zone test.
- A valid but wrong-day snapshot must not be accepted as today’s authority; Task 3 adds the fail-closed test.
- A missing trade calendar must never cause retention to guess and delete old files; Task 6 adds the no-delete test.

---

## Branch and PR topology

Create `feature/signal-freeze-phase-a` from current `main` for Tasks 1–2. Create each later branch from the previous phase head (`phase-b` from A, C from B, D from C). A–D may be presented as one cumulative PR from phase D after every phase is reviewed; do not merge to `main` before that PR passes. Phase E is a runtime observation period, and Phase F is a separately reviewed promotion/config PR.

## File Structure

- Create `src/signal_snapshot.py`: frozen-signal schema, canonical hashes, IO validation, late/review artifacts, retention and mode parsing.
- Modify `src/realtime_engine.py`: source resolution versus frozen consumption, 09:25 trigger, state metadata, late handling and fail-closed enforced paper path.
- Modify `src/paper_book.py`: watchlist injection; remove enforced-mode dependence on recent `selection.json`.
- Modify `src/signal_freeze_watch.py`: public append-only event helper for freeze, late, review, cleanup and shadow records.
- Create `tests/test_signal_snapshot.py`: core schema, hash and snapshot verification.
- Create `tests/test_signal_late_signals.py`: late window, review completeness and ledger events.
- Create `tests/test_signal_cleanup.py`: 90-trading-day artifact retention.
- Create `tests/test_signal_watchlist.py`: verified snapshot targets union dynamic PaperBook positions.
- Create `docs/evolution/signal-freeze-promotion.md`: shadow classification and human-promotion evidence template.

### Task 1: Canonical snapshot contract (Phase A)

**Files:**
- Create: `src/signal_snapshot.py`
- Create: `tests/test_signal_snapshot.py`

**Interfaces:**
- Produces: `canonical_json_bytes(value: dict) -> bytes`, `sha256_json(value: dict) -> str`, `build_snapshot(day: str, targets: list[dict], source_tier: str, source_date: str, source_artifacts: list[dict], generated_at: datetime, repo_root: str) -> dict`.
- Produces: `snapshot_path(data_dir: str, day: str) -> str` and `SnapshotValidation` mapping with `status` in `ready|missing|invalid|tampered`.

- [ ] **Step 1: Write the failing canonical-hash tests**

```python
def test_snapshot_hash_is_stable():
    targets = [{"canon": "600000.SH", "target_weight": 1.0}]
    a = build_snapshot("20261003", targets, "drl_same_day", "20261002", [], FIXED_SH, REPO)
    b = build_snapshot("20261003", targets, "drl_same_day", "20261002", [], FIXED_SH, REPO)
    assert a["snapshot_hash"] == b["snapshot_hash"]

def test_snapshot_hash_changes_when_targets_change():
    left = build_snapshot("20261003", [{"canon": "600000.SH", "target_weight": 1.0}], "drl_same_day", "20261002", [], FIXED_SH, REPO)
    right = build_snapshot("20261003", [{"canon": "000001.SZ", "target_weight": 1.0}], "drl_same_day", "20261002", [], FIXED_SH, REPO)
    assert left["snapshot_hash"] != right["snapshot_hash"]
```

Also add tests for schema version `1`, explicit Shanghai/UTC timestamps, non-finite-value rejection, and source artifacts outside the repository root being rejected.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_snapshot.py -v`  
Expected: FAIL because `signal_snapshot` and `build_snapshot` do not exist.

- [ ] **Step 3: Implement the canonical data helpers in `src/signal_snapshot.py`**

Use `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)` and UTF-8. Require an aware `generated_at`, normalize it to Shanghai and UTC, normalize all source artifact paths relative to the repo root, compute `input_hash`, then compute `snapshot_hash` with its own field set to `None`.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_snapshot.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit the Phase A contract**

```powershell
git add src/signal_snapshot.py tests/test_signal_snapshot.py
git commit -m "feat(signal-freeze): add canonical snapshot contract"
```

### Task 2: Atomic snapshot persistence and validation (Phase A)

**Files:**
- Modify: `src/signal_snapshot.py`
- Modify: `tests/test_signal_snapshot.py`

**Interfaces:**
- Consumes: Task 1 `build_snapshot` and `snapshot_path`.
- Produces: `write_snapshot(data_dir: str, snapshot: dict) -> str` and `read_snapshot(data_dir: str, day: str) -> dict` where the result contains `status`, `snapshot`, and `reason`.

- [ ] **Step 1: Write failing persistence and classification tests**

```python
def test_write_then_read_returns_verified_snapshot(tmp_path):
    path = write_snapshot(str(tmp_path), SNAPSHOT)
    assert read_snapshot(str(tmp_path), "20261003")["status"] == "ready"

def test_changed_target_with_old_hash_is_tampered(tmp_path):
    write_json(snapshot_path(str(tmp_path), "20261003"), TAMPERED)
    assert read_snapshot(str(tmp_path), "20261003")["status"] == "tampered"
```

Add L1 missing, L2 malformed JSON, L2 missing required key, L2 unsupported schema version, and wrong-day snapshot tests.

- [ ] **Step 2: Run persistence tests to verify they fail**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_snapshot.py -v`  
Expected: FAIL because storage and verification functions do not exist.

- [ ] **Step 3: Implement persistence and validation**

Write only to `data/daily/<day>/signal_snapshot_<day>.json` through `utils.atomic_write_json`. `read_snapshot` must distinguish `missing`, `invalid`, and `tampered`; never resolve a new live target as a fallback. Validate date equality before returning `ready`.

- [ ] **Step 4: Run focused tests and the existing freeze-watch tests**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_snapshot.py tests/test_signal_freeze_watch.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit verified snapshot IO**

```powershell
git add src/signal_snapshot.py tests/test_signal_snapshot.py
git commit -m "feat(signal-freeze): persist and validate snapshots"
```

### Task 3: Engine freeze point and fail-closed target consumption (Phase B)

**Files:**
- Modify: `src/realtime_engine.py:490-608, 628-846, 1529-1681`
- Modify: `src/signal_snapshot.py`
- Modify: `tests/test_signal_snapshot.py`

**Interfaces:**
- Consumes: Task 2 `read_snapshot` and `write_snapshot`.
- Produces: `RealtimeEngine._freeze_or_load_targets(now: datetime) -> tuple[list[dict], dict, str, dict]`, returning target data plus snapshot status metadata; `run_tick(now: datetime | None = None) -> None` accepts an injected test time.
- Produces: `read_mode_control(repo_root: str) -> dict` with default `{"mode": "shadow"}` and explicit `shadow|enforce` validation.

- [ ] **Step 1: Write failing engine tests with an injected Shanghai time**

```python
def test_at_0925_engine_freezes_weighted_live_targets(engine, tmp_path):
    targets, _sel, _source_day, meta = engine._freeze_or_load_targets(FIXED_0925)
    assert meta["snapshot_status"] == "ready"
    assert read_snapshot(str(tmp_path), "20261003")["snapshot"]["weights"] == {"600000.SH": 1.0}

def test_enforce_missing_snapshot_only_values_and_does_not_rebalance(engine, paper_book, mode_file):
    mode_file.write_text('{"mode":"enforce","promoted_at":"2026-10-03T15:00:00+08:00","promoted_by":"tester","evidence":"test"}', encoding="utf-8")
    engine.run_tick(now=AFTER_0925)
    assert paper_book.rebalance_calls == 0
    assert engine.snapshot_status == "missing"
```

Add `test_missing_or_invalid_mode_control_defaults_to_shadow`, L2/L3 no-rebalance/alert tests, a post-09:25 verified snapshot-only test, a wrong-day fail-closed test, a pre-09:25 test that preserves existing live resolution for preparation, and a non-paper broker rejection test. Use injected times; do not change the system clock.

- [ ] **Step 2: Run the focused engine tests to verify they fail**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_snapshot.py -v`  
Expected: FAIL because the engine has no freeze state or snapshot-only path.

- [ ] **Step 3: Extract live target resolution and add the 09:25 state transition**

Keep the existing five-rung resolver available for legacy backtest callers. Add `read_mode_control` with strict required enforce fields (`promoted_at`, `promoted_by`, `evidence`), defaulting invalid control files to shadow. In `RealtimeEngine`, re-resolve and weight once at or after 09:25, build/write/verify the snapshot, and record only `snapshot_status`, `snapshot_ref`, and `snapshot_hash` in live state. In `enforce`, L1/L2/L3 must bypass `_rebalance`; in `shadow`, preserve existing target consumption and only record comparison material. Do not make same-day retry/recovery an automatic path.

- [ ] **Step 4: Run focused, regression and full-suite verification**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_snapshot.py tests/test_engine_date_override.py tests/test_plan_consume_day.py tests/test_signal_freeze_watch.py -v`  
Expected: PASS.

Run: `.venv314\Scripts\python.exe -m pytest tests -q`  
Expected: exit 0; record passed/skipped counts without claiming skipped tests passed.

- [ ] **Step 5: Commit the Phase B engine gate**

```powershell
git add src/realtime_engine.py src/signal_snapshot.py tests/test_signal_snapshot.py
git commit -m "feat(signal-freeze): gate paper targets on verified snapshot"
```

### Task 4: Verified snapshot watchlist (Phase B)

**Files:**
- Modify: `src/paper_book.py:727-1105`
- Modify: `src/realtime_engine.py`
- Create: `tests/test_signal_watchlist.py`

**Interfaces:**
- Consumes: Task 2 `read_snapshot` and the engine’s snapshot status/ref.
- Produces: `PriceFeed.set_snapshot_targets(targets: list[dict] | None) -> None`; `_fetch_spot(fetch_all=False)` builds a copy-based union of supplied snapshot target codes and `positions`.

- [ ] **Step 1: Write failing watchlist tests**

```python
def test_watchlist_is_verified_snapshot_targets_union_current_positions():
    feed.set_snapshot_targets([{"canon": "600000.SH"}])
    feed.set_positions({"000001.SZ": {"qty": 100}})
    assert feed.watchlist_codes() == {"600000.SH", "000001.SZ"}
```

Add tests proving a post-freeze enforced feed does not add codes from `selection.json`, and that mutating the original positions mapping after injection does not mutate the feed’s stored copy.

- [ ] **Step 2: Run the watchlist tests to verify they fail**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_watchlist.py -v`  
Expected: FAIL because no snapshot-target injection API exists.

- [ ] **Step 3: Implement copy-safe watchlist composition**

Copy injected positions in `set_positions`; add `set_snapshot_targets`. In enforced mode, use only the injected verified targets plus positions and remove the recent `selection.json` scan from that path. Preserve legacy/shadow behavior until enforce promotion.

- [ ] **Step 4: Run focused and full regression tests**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_watchlist.py tests/test_signal_snapshot.py -v`  
Expected: PASS.

Run: `.venv314\Scripts\python.exe -m pytest tests -q`  
Expected: exit 0.

- [ ] **Step 5: Commit Phase B watchlist isolation**

```powershell
git add src/paper_book.py src/realtime_engine.py tests/test_signal_watchlist.py
git commit -m "feat(signal-freeze): source paper watchlist from snapshot"
```

### Task 5: Late-signal archive and audit events (Phase C)

**Files:**
- Modify: `src/signal_snapshot.py`
- Modify: `src/signal_freeze_watch.py`
- Modify: `src/realtime_engine.py:1474-1527`
- Create: `tests/test_signal_late_signals.py`

**Interfaces:**
- Produces: `archive_late_signal(data_dir: str, day: str, candidate: dict, arrived_at: datetime, snapshot_status: str) -> dict`.
- Produces: `signal_freeze_watch.record_event(kind: str, payload: dict, path: str | None = None, now: datetime | None = None) -> dict`.

- [ ] **Step 1: Write failing late-window and ledger tests**

```python
def test_midday_candidate_is_archived_and_cannot_replace_targets(tmp_path):
    candidate = {"targets": [{"canon": "600000.SH", "target_weight": 1.0}], "source": "midday_reselect"}
    result = archive_late_signal(str(tmp_path), "20261003", candidate, SH_1128, "ready")
    assert result["status"] == "archived"
    assert json.loads(path.read_text())["items"][0]["late_for_consume_day"] == "20261003"

def test_out_of_window_candidate_only_records_audit_event(tmp_path):
    candidate = {"targets": [{"canon": "600000.SH", "target_weight": 1.0}], "source": "midday_reselect"}
    assert archive_late_signal(str(tmp_path), "20261003", candidate, SH_150001, "ready")["status"] == "out_of_window"
```

Add tests for `unknown` delay reason, atomic read-modify-write serialization, archive failure recording, and an engine midday path that leaves `self.targets` unchanged.

- [ ] **Step 2: Run the late-signal tests to verify they fail**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_late_signals.py -v`  
Expected: FAIL because late archive and generic ledger event APIs do not exist.

- [ ] **Step 3: Implement bounded late archival**

Accept only `(09:25:00, 15:00:00]` Shanghai arrivals into `late_signals_<day>.json`; give every payload a canonical ID/hash, use an internal write lock plus atomic write, and write every outcome through `record_event`. Change midday reselect to archive its result and alert instead of assigning `self.targets` or `self.midday_targets`.

- [ ] **Step 4: Run focused and existing ledger tests**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_late_signals.py tests/test_signal_freeze_watch.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit Phase C**

```powershell
git add src/signal_snapshot.py src/signal_freeze_watch.py src/realtime_engine.py tests/test_signal_late_signals.py
git commit -m "feat(signal-freeze): archive late candidates for review"
```

### Task 6: Whole-pool reviews and 90-trading-day retention (Phase D)

**Files:**
- Modify: `src/signal_snapshot.py`
- Modify: `src/signal_freeze_watch.py`
- Create: `tests/test_signal_cleanup.py`
- Modify: `tests/test_signal_late_signals.py`

**Interfaces:**
- Produces: `write_late_review(data_dir: str, day: str, review: dict) -> dict`, `approved_late_candidate(data_dir: str, day: str) -> dict | None`, and `cleanup_freeze_artifacts(data_dir: str, trading_days: list[str], keep_days: int = 90) -> dict`.

- [ ] **Step 1: Write failing review and retention tests**

```python
def test_partial_review_is_not_approved(tmp_path):
    review = {"reviewer": "tester", "reviewed_at": SH_1500, "candidate_id": "c1", "payload_hash": "a" * 64, "verdict": "approved", "comment": "only one item"}
    write_late_review(str(tmp_path), "20261003", review)
    assert approved_late_candidate(str(tmp_path), "20261003") is None

def test_cleanup_removes_t91_but_keeps_t90_after_valid_new_snapshot(tmp_path):
    report = cleanup_freeze_artifacts(str(tmp_path), TRADING_DAYS_92)
    assert old_snapshot.exists() is False and boundary_snapshot.exists()
```

Add reviewer/audit fields, rejection ledger-event tests, input-hash mismatch rejection, snapshot-write-failure no-cleanup, and missing-calendar no-delete tests.

- [ ] **Step 2: Run review/cleanup tests to verify they fail**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_late_signals.py tests/test_signal_cleanup.py -v`  
Expected: FAIL because review and cleanup APIs do not exist.

- [ ] **Step 3: Implement review validation and safe retention**

Persist reviews in `late_review_<day>.json`; require reviewer, aware review timestamp, candidate ID, exact payload hash, verdict and comment. Approval requires a complete candidate set and exact target/weight hash; rejected and partial reviews remain non-consumable and are audited. Delete only matching snapshot/late/review files older than the 90th retained trading day, only after a verified new snapshot and only when a supplied authoritative calendar is available.

- [ ] **Step 4: Run focused and full regression tests**

Run: `.venv314\Scripts\python.exe -m pytest tests/test_signal_late_signals.py tests/test_signal_cleanup.py tests/test_signal_snapshot.py -v`  
Expected: PASS.

Run: `.venv314\Scripts\python.exe -m pytest tests -q`  
Expected: exit 0.

- [ ] **Step 5: Commit Phase D**

```powershell
git add src/signal_snapshot.py src/signal_freeze_watch.py tests/test_signal_late_signals.py tests/test_signal_cleanup.py
git commit -m "feat(signal-freeze): require whole-pool late review"
```

### Task 7: Shadow evidence and enforce promotion (Phases E–F)

**Files:**
- Create: `docs/evolution/signal-freeze-promotion.md`

**Interfaces:**
- Consumes: Task 3 `read_mode_control`; it has already tested the invalid-control and non-paper-broker safety conditions.


- [ ] **Step 1: Add the shadow evidence and promotion template**

Create a template listing each trading day, every snapshot-versus-late-candidate difference, one required classification (`expected_timing`, `data_refresh`, `source_disagreement`, `unexplained`), the reviewer, and a paper-only promotion checklist. This is documentation, not an implementation shortcut.

- [ ] **Step 2: Run the Phase E runtime gate**

Run shadow for five actual trading days. For every difference, record an allowed classification.  
Expected: no `unexplained` records; this observation is manual evidence, not an automated test pass.

- [ ] **Step 3: Create the separate promotion PR only after the runtime gate**

```powershell
git switch -c feature/signal-freeze-phase-f
# add the named config/signal_freeze_mode.json promotion record only after approval
git commit -m "feat(signal-freeze): enable verified paper snapshot mode"
```

## Final verification

- [ ] Run `.venv314\Scripts\python.exe -m pytest tests -q` and record exact passed/skipped/warning totals.
- [ ] Run CI on the cumulative A–D PR and inspect Windows/Linux plus Python 3.10 h5i checks; do not merge on a red check.
- [ ] Confirm the test data root is temporary in all new tests and that no production `data/` file changed.
- [ ] Confirm the only execution path remains PaperBook and that no broker credentials, order API, or real-account side effect was introduced.
