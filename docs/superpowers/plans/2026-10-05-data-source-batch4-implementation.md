# Data-Source Batch 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dependency-free staging, manifest, and orphan-reconciliation coordinator around canonical data batches without touching real h5i or Phase E.

**Architecture:** `batch_hash.py` owns one canonical serialization/hash function. `staging.py` atomically persists canonical JSONL before creating Batch 1 metadata. `commit.py` owns fake-injectable sink/probe contracts, per-trade-day in-process locking, manifest publication, same-day idempotency, and staged reconciliation. Existing `bars_ingest.py` and `h5i_sync.py` remain unchanged; Batch 5 will provide real adapters.

**Tech Stack:** Python standard library, existing `src.data_sources` metadata contracts, `pytest`, `tmp_path`; no new dependency and no real h5i import.

**Spec:** `docs/superpowers/specs/2026-10-05-data-source-batch4-design.md`

## Global Constraints

- Do not modify `signal_snapshot.py`, `realtime_engine.py`, `paper_book.py`, `signal_freeze_watch.py`, `h5i_bar_store.py`, `bars_ingest.py`, or `h5i_sync.py`.
- Do not connect Baostock, AkShare, mootdx, ZZShare, or real h5i.
- Do not change the `daily_bars` schema or add a h5i manifest table.
- All production-path roots are explicit function arguments; tests use only `tmp_path`.
- Canonical hash fields are exactly `symbol, trade_day, open, high, low, close, volume, amount, adj_factor`.
- Numeric hash normalization uses finite values, volume as integer, and the six floating fields quantized to 10 decimal places.
- One successful batch is allowed per `trade_day`; same-day different batches are rejected.
- `unknown` sink outcomes remain `staged`; they never publish a manifest.
- Batch 4 uses an in-process `trade_day` lock; no timeout or cross-process lock is implemented.

## Review Focus

- Float64 round-trip changes representation: `test_content_hash_stable_after_round_trip` must prove staging and probe use identical canonical serialization.
- Existing h5i data without a sidecar manifest: `occupied_unknown` must block a new batch rather than treating date existence as success.
- Sink exceptions after a possible write: `unknown` must keep metadata staged and omit the manifest.
- Atomic ordering: metadata must not appear if staging replacement fails; manifest must not appear before sink success.
- Repeated requests and exceptions: committed batch retries must be idempotent and locks must release in `finally`.

### Task 1: Canonical batch serialization and content hashes

**Files:**
- Create: `src/data_sources/batch_hash.py`
- Modify: `src/data_sources/__init__.py`
- Test: `tests/test_data_source_batch4.py`

**Interfaces:**
- Consumes: `CanonicalBatch` from `src.data_sources.adapters`.
- Produces: `canonical_serialize(batch: CanonicalBatch) -> str` and `content_hash(batch: CanonicalBatch) -> str`.

- [ ] **Step 1: Write the failing tests**

  Add tests for fixed field selection, `(trade_day, symbol)` ordering, NaN/Infinity rejection, 10-decimal float normalization, and stable hash output.

- [ ] **Step 2: Run the hash tests to verify RED**

  Run:

  ```powershell
  & D:\狗屁通のA大奇妙冒险\A_stock_rotation\.venv314\Scripts\python.exe -m pytest tests/test_data_source_batch4.py -k hash -v
  ```

  Expected: collection failure because `src.data_sources.batch_hash` does not exist.

- [ ] **Step 3: Implement the minimal hash module**

  Define the exact nine-field projection, deterministic row sorting, Decimal quantization, compact UTF-8 JSON, SHA-256, and explicit rejection of non-finite values. Do not add staging or h5i behavior here.

- [ ] **Step 4: Run the hash tests to verify GREEN**

  Run the same command; expected result is all hash tests passing in both `.venv310` and `.venv314`.

- [ ] **Step 5: Commit**

  ```powershell
  git add src/data_sources/batch_hash.py src/data_sources/__init__.py tests/test_data_source_batch4.py
  git commit -m "feat(data-source): add canonical batch hashing"
  ```

### Task 2: Atomic staging and staged metadata

**Files:**
- Create: `src/data_sources/staging.py`
- Modify: `src/data_sources/__init__.py`
- Test: `tests/test_data_source_batch4.py`

**Interfaces:**
- Consumes: `build_metadata`, `serialize_metadata`, `CanonicalBatch`, and `content_hash`.
- Produces: `stage_batch(root: str | Path, metadata: Mapping[str, Any], batch: CanonicalBatch) -> dict[str, Any]` plus these exact helpers used by Tasks 3–4:
  - `staging_path(root: str | Path, batch_id: str) -> Path`
  - `metadata_path(root: str | Path, batch_id: str) -> Path`
  - `manifest_path(root: str | Path, batch_id: str) -> Path`
  - `load_staged(root: str | Path, batch_id: str) -> tuple[dict[str, Any], CanonicalBatch]`

- [ ] **Step 1: Write failing staging tests**

  Add tests proving staging writes one canonical JSONL per record, creates metadata only after staging replacement succeeds, writes `ingest_status=staged`, returns the staged row count/content hash without extending the exact ten-field metadata schema, and leaves no final file when replacement is forced to fail.

- [ ] **Step 2: Run staging tests to verify RED**

  Run:

  ```powershell
  & D:\狗屁通のA大奇妙冒险\A_stock_rotation\.venv314\Scripts\python.exe -m pytest tests/test_data_source_batch4.py -k staging -v
  ```

  Expected: collection failure because `src.data_sources.staging` does not exist.

- [ ] **Step 3: Implement atomic staging**

  Use the caller-provided root, create only the staging, metadata, and manifest directories under it, write temporary files in the destination directory, replace the JSONL atomically, and write exact-ten-field metadata only after the canonical batch file is complete. Return the batch ID, staging/metadata/manifest paths, row count, and content hash as coordinator data; do not add row-count/hash keys to metadata. Reject mismatched metadata batch IDs or non-staged metadata.

- [ ] **Step 4: Run staging tests to verify GREEN**

  Run the same command in `.venv310` and `.venv314`; expected all staging tests passing and no files outside the pytest `tmp_path`.

- [ ] **Step 5: Commit**

  ```powershell
  git add src/data_sources/staging.py src/data_sources/__init__.py tests/test_data_source_batch4.py
  git commit -m "feat(data-source): add atomic batch staging"
  ```

### Task 3: Commit sink/probe contracts and manifest publication

**Files:**
- Create: `src/data_sources/commit.py`
- Modify: `src/data_sources/__init__.py`
- Test: `tests/test_data_source_batch4.py`

**Interfaces:**
- Consumes: Task 1 hash functions and Task 2 staged artifacts.
- Produces: `CommitResult`, `ProbeResult`, `CommitSink`, `ContentProbe`, and `commit_staged(root: str | Path, batch_id: str, sink: CommitSink, probe: ContentProbe) -> dict[str, Any]`.

  `CommitResult` has `status` (`committed|failed|unknown`), `row_count`, and optional `error`. `ProbeResult` has `exists`, `row_count`, and optional `content_hash`. `commit_staged` always calls the probe with the staged `trade_day` and sorted staged symbols before invoking the sink; the probe is required even for a fake sink so same-day occupancy cannot be silently bypassed.

- [ ] **Step 1: Write failing commit tests**

  Add fake sink/probe tests for committed success, explicit failure, unknown failure, manifest fields including `source_tier`, no manifest before sink success, same-day occupied detection, and an exact-match retry that is idempotent rather than invoking a second write.

- [ ] **Step 2: Run commit tests to verify RED**

  Run:

  ```powershell
  & D:\狗屁通のA大奇妙冒险\A_stock_rotation\.venv314\Scripts\python.exe -m pytest tests/test_data_source_batch4.py -k commit -v
  ```

  Expected: collection failure because `src.data_sources.commit` does not exist.

- [ ] **Step 3: Implement commit coordination**

  Load the staged batch and metadata, acquire a process-local lock keyed by trade day, reject a different existing committed batch for the same day, call the probe before writing, and classify an existing h5i day without an exact verifiable sidecar match as `occupied_unknown`. An exact probe match for the staged batch is an idempotent committed result and must not call the sink again. Call the injected sink only for an available day, and atomically publish a self-contained manifest only after `status=committed`. Keep metadata staged for `unknown`; transition to failed only for a confirmed no-write failure.

- [ ] **Step 4: Run commit tests to verify GREEN**

  Run the same command in both interpreters; expected all sink/probe and manifest tests passing.

- [ ] **Step 5: Commit**

  ```powershell
  git add src/data_sources/commit.py src/data_sources/__init__.py tests/test_data_source_batch4.py
  git commit -m "feat(data-source): add injected commit and probe contracts"
  ```

### Task 4: Orphan reconciliation and lock safety

**Files:**
- Modify: `src/data_sources/commit.py`
- Modify: `src/data_sources/__init__.py`
- Test: `tests/test_data_source_batch4.py`

**Interfaces:**
- Consumes: staged metadata/batch, `CommitResult`/`ProbeResult`, and `content_hash`.
- Produces: `reconcile_staged(root: str | Path, batch_id: str, probe: ContentProbe) -> dict[str, Any]` with outcomes `committed`, `still_staged`, or `occupied_unknown`. The returned mapping includes `batch_id`, `trade_day`, `outcome`, `row_count`, `content_hash`, and `reason`; it never deletes staged data or writes a manifest for `still_staged` or `occupied_unknown`.

- [ ] **Step 1: Write failing reconciliation tests**

  Add tests for exact probe match transitioning staged to committed, row-count/hash mismatch remaining staged, existing unmatched data returning `occupied_unknown`, same-batch retry idempotency, lock release after sink exception, and a second same-day batch being rejected. Include the round-trip hash test through a fake sink/probe so staging and probe are proven to use the same canonical serialization.

- [ ] **Step 2: Run reconciliation tests to verify RED**

  Run:

  ```powershell
  & D:\狗屁通のA大奇妙冒险\A_stock_rotation\.venv314\Scripts\python.exe -m pytest tests/test_data_source_batch4.py -k reconcile -v
  ```

  Expected: failure because `reconcile_staged` and its state transitions are not implemented.

- [ ] **Step 3: Implement reconciliation**

  Compare `exists`, row count, and content hash; publish the manifest and transition metadata only on an exact match; preserve staged files for unresolved cases. Treat `exists=True` with missing or mismatched row count/hash as `occupied_unknown`, never as success. Ensure the per-day lock is released with `finally` on every path.

- [ ] **Step 4: Run reconciliation tests to verify GREEN**

  Run the same command in both interpreters; expected all reconciliation and lock tests passing.

- [ ] **Step 5: Commit**

  ```powershell
  git add src/data_sources/commit.py src/data_sources/__init__.py tests/test_data_source_batch4.py
  git commit -m "feat(data-source): reconcile orphaned staged batches"
  ```

### Task 5: Full verification and handoff

**Files:**
- Modify: `.planning/2026-10-04-data-source-metadata/task_plan.md`
- Modify: `.planning/2026-10-04-data-source-metadata/progress.md`

- [ ] **Step 1: Run the complete Batch 1–4 focused suite**

  ```powershell
  & D:\狗屁通のA大奇妙冒险\A_stock_rotation\.venv310\Scripts\python.exe -m pytest tests/test_data_source_metadata.py tests/test_data_source_router.py tests/test_data_source_adapters.py tests/test_data_source_batch4.py -q
  & D:\狗屁通のA大奇妙冒险\A_stock_rotation\.venv314\Scripts\python.exe -m pytest tests/test_data_source_metadata.py tests/test_data_source_router.py tests/test_data_source_adapters.py tests/test_data_source_batch4.py -q
  ```

  Expected: all focused tests pass in both environments; no real h5i dependency is required.

- [ ] **Step 2: Run full regression in both environments**

  ```powershell
  & D:\狗屁通のA大奇妙冒险\A_stock_rotation\.venv310\Scripts\python.exe -m pytest tests -q
  & D:\狗屁通のA大奇妙冒险\A_stock_rotation\.venv314\Scripts\python.exe -m pytest tests -q
  ```

  Expected: no new failures; existing dependency/data skips remain explicitly reported.

- [ ] **Step 3: Review boundaries**

  Run `git diff main -- src/bars_ingest.py src/h5i_sync.py src/signal_snapshot.py src/realtime_engine.py src/paper_book.py src/signal_freeze_watch.py src/h5i_bar_store.py` and verify there are no changes to those protected production paths.

- [ ] **Step 4: Commit planning ledger only if needed**

  Update the ignored planning files with exact test counts, commit hashes, and any environment skips. Do not stage them.

- [ ] **Step 5: Push only after verification**

  ```powershell
  git status -sb
  git push origin feature/data-source-router-v2
  ```

  The branch must contain only the reviewed Batch 4 commits and the previously pushed design commit.
