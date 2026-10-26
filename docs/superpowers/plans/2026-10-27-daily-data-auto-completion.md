# 每日行情数据自动补全 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 建立独立的 `AStockDataUpdate` 每日更新服务，先同步 StockDB 主链，确认引擎与 H5i 双缺口后再自动执行经过质量闸门和 provenance 留痕的 Baostock 回补。

**Architecture:** 复用现有 `engine_bars_sync`、`backfill_trigger` 和 `baostock_backfill`，把 `data_update_daemon` 的日线阶段改为官方日历驱动的幂等流程；新增独立 NSSM 服务安装脚本，不把补数逻辑嵌入 `AStockDaemon`。更新服务只写行情更新回执、H5i 和回补 provenance，不调用选股、target、DRL、PaperBook 或 broker。

**Tech Stack:** Python 3.10、pytest、h5i_db、StockDB SDK、Baostock、PowerShell、NSSM。

**Spec:** `docs/superpowers/specs/2026-10-27-daily-data-auto-completion-design.md`

## Global Constraints

- StockDB 是日线主源；Baostock 仅在引擎缺口与 H5i 缺口同时成立时使用。
- 缺口日期必须来自官方交易日历，不得通过已有 `daily_bars` 日期推测整日缺失。
- 自动回补默认依赖显式 `data/backfill_switch.json`/`BACKFILL_ENABLED` 开关；坏配置按关闭处理。
- 任一日期覆盖不足、质量守卫失败、日历弱或关键输入缺失时拒写并保留失败原因。
- 回补后必须验证 H5i 水位前进且 StockDB 引擎日期未改变。
- 保持 `TRADE_BROKER=paper`、`DRL_PLAN_MODE=shadow`、`FUSION_WEIGHT_MODE=shadow`、`RANK_BY_FUSION=0` 和冻结 selector 权重。
- 不安装、启动或修改真实 broker，不改变选股、目标权重、撮合、PaperBook 或 DRL production 行为。

## Review Focus

- **整日完全缺失**：不能依赖 `daily_bars` 已有日期发现缺口；由官方日历计算并测试 2026-10-26 类场景。
- **主源已追平而 H5i 落后**：必须修摄入链，不得调用备用源掩盖问题。
- **备用源覆盖不完整**：任一失败标的或质量守卫失败都要整日拒写，并保持下一轮可重试。
- **服务环境与交互环境不同**：服务必须显式使用 `.venv310`、`STOCKDB_ROOT` 和开关文件，不能依赖当前 PowerShell 环境。
- **自动任务副作用**：测试必须证明更新流程不加载 selector/target/DRL/broker 执行路径。

---

### Task 1: 为官方日历驱动的缺口发现建立失败测试

**Files:**
- Modify: `tests/test_data_update_daemon.py` (create if absent)
- Test: `tests/test_backfill_trigger.py` (only if a shared fixture/helper is required)

**Interfaces:**
- Consumes: `backfill_trigger.decide()` / `missing_days()` and the existing `data_update_daemon` daily-stage helpers.
- Produces: executable regression cases for the later daemon integration.

- [x] **Step 1: Write the failing tests**

  Add tests that assert:

  - engine `2026-10-23`, H5i `2026-10-23`, expected `2026-10-26`, official calendar => missing day `20261026` is discovered;
  - engine and H5i both `2026-10-26` => no fallback call;
  - engine `2026-10-26`, H5i `2026-10-23` => no fallback call and reason identifies ingestion gap;
  - non-official calendar or missing watermark => no write/no fallback.

- [x] **Step 2: Run tests to verify they fail for the missing daemon behavior**

  Run:

  ```powershell
  .venv310\Scripts\python.exe -m pytest tests/test_data_update_daemon.py -q
  ```

  Expected: FAIL because the existing daemon derives candidates from already-present `daily_bars` dates and has no testable primary/fallback decision boundary.

- [x] **Step 3: Commit the failing tests**

  ```powershell
  git add tests/test_data_update_daemon.py tests/test_backfill_trigger.py
  git commit -m "test: define daily data completion gap contract"
  ```

### Task 2: Implement a pure, injectable daily completion decision

**Files:**
- Create: `src/daily_data_completion.py`
- Modify: `src/backfill_trigger.py` only if a narrow shared helper is needed
- Test: `tests/test_data_update_daemon.py`

**Interfaces:**
- Consumes: `engine_day`, `h5i_watermark`, `expected_day`, `calendar_strength`, `enabled`, `lookback_days`.
- Produces: `plan_daily_completion(...) -> dict` with `action`, `triggered`, `missing_days`, `criteria`, `reasons`, and normalized identity fields.

- [x] **Step 1: Implement the smallest pure function that satisfies Task 1**

  Reuse `backfill_trigger.decide()` rather than duplicating A/B semantics. The new adapter must accept all observations as arguments, use no network/filesystem writes, and return a stable JSON-serializable plan.

- [x] **Step 2: Run the focused tests**

  Run:

  ```powershell
  .venv310\Scripts\python.exe -m pytest tests/test_data_update_daemon.py -q
  ```

  Expected: PASS.

- [x] **Step 3: Add edge-case tests for retry-safe failure states**

  Pin that an empty `missing_days` with A/B true remains `triggered=true` but `action=trigger` with a non-success reason, and that a failed prior run does not become a successful state without a new verified write.

- [x] **Step 4: Run focused tests again**

  Expected: PASS with no production data mutation.

- [x] **Step 5: Commit the pure decision contract**

  ```powershell
  git add src/daily_data_completion.py tests/test_data_update_daemon.py
  git commit -m "feat: add pure daily completion decision"
  ```

### Task 3: Refactor `data_update_daemon` to run primary-first and fallback safely

**Files:**
- Modify: `src/data_update_daemon.py`
- Modify: `src/backfill_trigger.py` only if the daemon needs an explicit injected observation path
- Test: `tests/test_data_update_daemon.py`
- Test: `tests/test_baostock_backfill.py` (only for integration contract assertions)

**Interfaces:**
- Consumes: `plan_daily_completion`, `engine_bars_sync.sync_to_latest`, `baostock_backfill.py --days ... --write`, and the existing data-update state file.
- Produces: a daily stage result containing `primary`, `decision`, `fallback`, `verification`, `retryable`, and `ok` fields; `data/data_update_last.json` remains the operational receipt.

- [x] **Step 1: Write failing orchestration tests**

  Inject primary and subprocess runners and assert:

  - primary sync runs before the decision;
  - primary caught up => fallback runner is not called;
  - only H5i is stale => fallback runner is not called;
  - double gap + enabled => fallback receives exactly the official missing dates;
  - fallback non-zero result or a date not marked `written` => result is failed/retryable and state is not marked successful;
  - successful fallback records engine unchanged and H5i advanced before marking success.

- [x] **Step 2: Run the tests to verify the orchestration fails**

  ```powershell
  .venv310\Scripts\python.exe -m pytest tests/test_data_update_daemon.py -q
  ```

  Expected: FAIL because the existing daily stage uses `scripts/backfill_daily.py` and does not expose the required primary/fallback orchestration.

- [x] **Step 3: Implement the minimal orchestration**

  Replace the daily-stage candidate calculation with official-calendar-driven planning. Run the engine sync first; then invoke the existing Baostock CLI through the known `.venv310` interpreter only when the pure plan says `trigger`. Read the JSON result from a temporary/explicit output path, never infer success from stdout, and keep each failed date retryable.

- [x] **Step 4: Add receipt/provenance assertions**

  Assert that the receipt retains engine/H5i before and after values, calendar strength, missing dates, source, write statuses, and failure reasons; do not alter selector/target/DRL/broker files.

- [x] **Step 5: Run focused regression**

  ```powershell
  .venv310\Scripts\python.exe -m pytest tests/test_data_update_daemon.py tests/test_backfill_trigger.py tests/test_baostock_backfill.py -q
  ```

  Expected: PASS.

- [x] **Step 6: Commit the daemon integration**

  ```powershell
  git add src/data_update_daemon.py src/daily_data_completion.py tests/test_data_update_daemon.py tests/test_backfill_trigger.py tests/test_baostock_backfill.py
  git commit -m "feat: automate primary-first daily data completion"
  ```

### Task 4: Add the independent Windows service definition

**Files:**
- Create: `ops/install_data_update_service.ps1`
- Modify: `tests/test_run_services_runtime.py` or create `tests/test_data_update_service.py`
- Modify: `docs/superpowers/specs/2026-10-27-daily-data-auto-completion-design.md` only if implementation details need clarification

**Interfaces:**
- Consumes: repository root, `.venv310\Scripts\python.exe`, `STOCKDB_ROOT`, NSSM hash policy used by `ops/install_daemon_service.ps1`.
- Produces: idempotent service registration for `AStockDataUpdate`, with logs, auto-start, restart throttle, and explicit environment.

- [x] **Step 1: Write failing service-contract tests**

  Assert the script contains:

  - service name `AStockDataUpdate`;
  - `.venv310\Scripts\python.exe src\data_update_daemon.py`;
  - `STOCKDB_ROOT` and UTF-8 environment settings;
  - NSSM SHA-256 verification and explicit acceptance of unsigned NSSM;
  - `AppDirectory`, stdout/stderr logs, auto-start, restart delay, and no `daemon.py`/`run_daily.py` execution.

- [x] **Step 2: Run tests to verify the contract is absent**

  ```powershell
  .venv314\Scripts\python.exe -m pytest tests/test_data_update_service.py -q
  ```

  Expected: FAIL because the installer does not exist.

- [x] **Step 3: Implement the installer by following the existing NSSM trust and logging discipline**

  Keep installation/uninstallation idempotent and require administrator privileges. Installation must not silently start the service unless `-Start` is supplied.

- [x] **Step 4: Run the service-contract tests**

  Expected: PASS.

- [x] **Step 5: Commit the service definition**

  ```powershell
  git add ops/install_data_update_service.ps1 tests/test_data_update_service.py
  git commit -m "feat: add managed daily data update service"
  ```

### Task 5: Add deployment-safe operational configuration and end-to-end verification

**Files:**
- Modify: `docs/README` or the current operations documentation file identified by the repository’s service docs
- Create locally only: `data/backfill_switch.json` with `enabled=true`, `lookback_days=10` (gitignored runtime state)
- Test: existing focused suites plus an isolated fixture test

**Interfaces:**
- Consumes: Tasks 2–4 and the approved paper/shadow environment.
- Produces: service installation evidence, dry-run evidence, controlled single-day write evidence, and a clean Git state.

- [x] **Step 1: Run the full relevant regression before deployment**

  ```powershell
  .venv314\Scripts\python.exe -m pytest tests/test_data_update_service.py tests/test_run_services_runtime.py tests/test_backfill_trigger.py tests/test_baostock_backfill.py -q
  .venv310\Scripts\python.exe -m pytest tests/test_data_update_daemon.py tests/test_engine_bars_sync.py tests/test_h5i_sync.py -q
  ```

  Expected: all selected tests pass; skips are reported and classified, not counted as successful live-source coverage.

- [x] **Step 2: Run an isolated dry-run against a historical fixture**

  Verify the missing date is detected, the fallback result is fully covered, and production H5i, state, live state, selector snapshot, and PaperBook hashes remain unchanged.

- [ ] **Step 3: Install the service only after dry-run passes**

  Use an administrator PowerShell session with the explicit unsigned-NSSM acceptance flag; do not change the existing `AStockDaemon` or `AStockStockdb` services.

- [ ] **Step 4: Verify service identity and one controlled `--once` cycle**

  Confirm `AStockDataUpdate` is Running/Automatic, its command line points to `.venv310` and `data_update_daemon.py`, and a forced cycle produces a receipt. If live data is still stale, stop at receipt verification; do not use an unverified external source or count an observation day.

- [x] **Step 5: Run protected-path and repository verification**

  ```powershell
  git diff --check
  git status --short
  ```

  Confirm no tracked changes touch selector, target, DRL, PaperBook, broker, or production strategy configuration.

- [x] **Step 6: Commit documentation and report remaining external blockers**

  ```powershell
  git add docs
  git commit -m "docs: document daily data completion operations"
  ```

  Report separately: verified code behavior, service installation state, source freshness, and any unresolved upstream StockDB publication issue.
