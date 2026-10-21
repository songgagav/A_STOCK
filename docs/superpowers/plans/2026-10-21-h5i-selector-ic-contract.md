# H5i Selector-Factor IC Contract — Implementation Plan

**Status:** Approved plan; inline execution authorized by the user on 2026-10-21.
**Spec:** [`2026-10-21-h5i-selector-ic-contract-design.md`](../specs/2026-10-21-h5i-selector-ic-contract-design.md)
**Worktree/branch:** `.worktrees/h5i-selector-ic-contract` / `feature/h5i-selector-ic-contract`
**Base:** approved spec commit `eb59b6ca1596403fe3e71c63bdddccba97d0d8b6`

## Goal

Deliver a reproducible, read-only H5i/PIT factor IC diagnostic that writes only to a caller-selected staging directory outside the repository and protected production data. Every unsupported/incomplete source remains explicit. No staged artifact is eligible for production weights, the health gate, selector, execution, or Observation Epoch.

## Architecture

```text
explicit H5i DB (read-only) + explicit calendar JSON
                    │
                    ▼
      PIT signals + 09:25 label/coverage panels
                    │
                    ▼
       diagnostic IC + source/missingness report
                    │
                    ▼
caller-selected external directory, atomic immutable bundle
```

- `selector_ic_contract.py`: pure versioned factor/label/rank semantics; no I/O.
- `h5i_selector_ic.py`: explicit read-only H5i adapter and bounded panel builder; no legacy `db.py`, DuckDB, or default-path fallback.
- `diagnostic_ic_bundle.py`: safe path validation, content identity, no-overwrite atomic finalize, hashes, and verification.
- `scripts/stage_h5i_selector_ic.py`: explicit-input CLI that wires the above and exits nonzero for invalid inputs or failed finalization.
- No changes to `ic_backtest.py`, `ic_curve_refresh.py`, `weight_optimizer.py`, `premarket_healthcheck.py`, selector/realtime/PaperBook/broker, service controls, or Epoch code.

## Label and factor rulings

### 09:25-to-daily-bar label

For signal session `D`, compute features only through the immediately preceding completed session. The snapshot is frozen during 09:25 Shanghai time; the engine then waits for live quotes and PaperBook applies actual fill price, slippage, and fees. Daily bars cannot reproduce those fills.

Use `open[D]` as the first post-decision daily price proxy. For horizon `h`, exit at `close[D+h-1]` and calculate:

```text
(close[D] / open[D]) * product(1 + change_pct[s] / 100 for s = D+1 ... D+h-1) - 1
```

`change_pct` is in percentage points. H1 is `close[D]/open[D]-1`. This is `label_version=0925_open_to_close_v1`, `entry_proxy=daily_open`, `paperbook_fill_replay=false`: diagnostic only, not realized or tradeable performance. Require every calendar session and every needed bar/price/return; never shorten a horizon, forward-fill, or substitute zero. Signal date, last closed feature date, and label settlement date remain separate fields.

### Factor registry and orientation

| Optimizer key | Staged signal | Source/cutoff | Stored orientation / expected consumer direction | Stage status |
|---|---|---|---|---|
| `vol` | Selector-facing low-vol score and raw annualized volatility as separate named columns; 20 close returns, sample std (`ddof=1`), `sqrt(252)`, existing 10–60% inverse clip | H5i `daily_bars`, last closed session before signal date | IC is reported for both raw and selector score; do not assert parity with legacy `vol` curve until reviewed | `candidate_unverified_consumer_parity` |
| `mom_rev` | Raw 20-session close return `mom_20`; retain the separately derived inverse selector score only for audit | H5i `daily_bars`, last closed session before signal date | Store raw `mom_20`; apply optimizer's expected `-1` direction exactly once; never calculate IC on inverse score and then negate again | `candidate` |
| `pb_rev` | Existing inverse PB selector score plus raw PB | H5i `valuation`, exact effective timestamp `<= D 09:25:00 Asia/Shanghai`; retain selected `ts` and age | Selector-facing score, expected `+1`; rows without provable timestamp are missing | `candidate`, source coverage/parity unverified |
| `roe` | None | H5i financials currently lacks verified announcement availability | No synthetic/neutral panel values | `unsupported: announcement_time_pit_unproven` |
| `mf_net` | None | No historical stock-level H5i money-flow source | Do not use DuckDB, latest snapshots, or industry estimates | `unsupported: stock_level_pit_source_absent` |

Neutral `0.5` from live helpers is not an observed value; all factor panels carry an independent validity/status/reason. Universe membership for signal date `D` is the observed set of symbols with valid daily bars on the prior calendar session. Record that proxy and its difference from the live selector universe; this is not exact selector replay.

## Interfaces to implement

1. `selector_ic_contract.py`
   - `FACTOR_CONTRACTS`, `HORIZONS=(1, 3, 5, 10, 20)`, `LABEL_VERSION`.
   - `signal_cutoff(day)`, `previous_session(day, sessions)`, and `horizon_sessions(day, h, sessions)`.
   - Pure factor helpers returning value plus `status/reason` (missing is not numeric neutral): vol score/raw vol, raw mom20/reverse score, PB score/raw PB.
   - `forward_return_for_symbol(bars_by_session, symbol, day, h, sessions)` with explicit maturity and missing-session result.
   - `rank_ic(signal_values, forward_returns, min_pairs=5)` using average ranks, finite matched pairs only, and statuses `ok`, `insufficient_pairs`, `constant_rank`, or `invalid`.
2. `h5i_selector_ic.py`
   - `H5iReadOnlySource(path, database_factory=None)` context manager opening `h5i_db.Database(path, read_only=True)` only when invoked.
   - Bounded `schema()`, `trading_sessions()`, `bars_for_sessions()`, and `valuation_asof(cutoff, symbols)` methods. Preserve valuation effective timestamps. Validate dates/symbols before SQL construction; close the database on success and error.
   - `build_diagnostic_panels(source, calendar, start, end)` emits per-date/per-factor values, label returns, IC/status, coverage and unsupported reasons without importing production weight or selector consumers.
3. `diagnostic_ic_bundle.py`
   - `write_diagnostic_bundle(output_root, run_identity, artifacts)` and `verify_diagnostic_bundle(path)`.
   - Require absolute explicit output root, reject the repository or any descendant/protected path and symlink/reparse traversal into the repository, refuse existing final/partial collisions, hash every artifact, write manifest last, atomically rename a same-parent temporary directory, and never overwrite.
   - Manifest carries `diagnostic_only=true`, `authoritative=false`, `observation_eligible=false`, source/code/schema/calendar hashes, formulas, cutoff, horizon settlement, coverage/missingness, direction/orientation, unsupported factors and `paperbook_fill_replay=false`.
4. CLI `scripts/stage_h5i_selector_ic.py`
   - Required: `--h5i-db`, `--calendar-json`, `--start`, `--end`, `--output-root` (no implicit defaults).
   - Validate source file, calendar schema/dates, bounded date order, supported H5i schemas, and external output path; open H5i read-only; generate/finalize/verify bundle; print output and factor statuses; return nonzero on invalid input/read failure/finalization failure.
   - Never invoke `ic_curve_refresh`, create `data/ic`, or accept a flag that makes the output authoritative.

## Global constraints

- Keep frozen selector weights and automatic reweighting pause unchanged; do not edit `data/weights.json`, frozen weight snapshot, or weight policy.
- Preserve `RANK_BY_FUSION=0`, `DRL_PLAN_MODE=shadow`, `FUSION_WEIGHT_MODE=shadow`, paper broker, Alpha `not_promotable`.
- Do not modify selector, target, costs, trading rules, PaperBook, broker, health gate, services, production IC files, production state, or Epoch/observation count.
- Do not update/rebuild StockDB/H5i in this implementation; all database access is explicit and read-only.
- Keep PR #24 separate. No push, merge, deployment, data refresh, production curve write, or service restart in this plan.

## Review focus

1. No post-cutoff feature or valuation timestamp can affect a signal; date-only valuation rows on `D` are not presumed available by 09:25.
2. H1/H5/H20 labels obey the approved session mapping; missing intermediate bars stay incomplete; no daily-bar label is called a PaperBook fill replay.
3. The `mom_rev` raw `mom_20` curve receives the negative consumer direction once; inverse score does not get double-negated. Vol legacy parity stays unresolved in metadata.
4. Missing/unsupported observations never become a neutral IC; fewer than five pairs/constant ranks are null with explicit statuses.
5. Bundle cannot write inside repo/protected paths, overwrite prior evidence, or produce authoritative/observation-eligible flags.

## Execution tasks (TDD; commit each task locally)

### Task 1 — Pure signal, session, label, rank contracts

**Files:** add `src/selector_ic_contract.py`, `tests/test_selector_ic_contract.py`.

1. RED: write tests for cutoff/timezone, strict prior session, H1/H3/H5/H20 exits, exact return formula, missing intermediate session/bar, nonfinite/invalid values, 20-return volatility sample std/annualizer/clip, mom20 endpoint and inverse mapping, PB clip, average-rank ties, minimum five pairs, constant ranks, and null/status semantics. Include a test that the stored raw mom20 IC is negated only by explicit expected direction.
2. Run `python -m pytest tests/test_selector_ic_contract.py -q`; confirm failures are due to missing module/interfaces.
3. Implement only pure helpers and immutable definitions; no filesystem/database imports.
4. Re-run focused tests; also run `python -m pytest tests/test_weight_optimizer_policy.py tests/test_factor_health.py -q` to prove no consumer behavior has been touched.
5. Commit: `feat(ic): define diagnostic selector PIT and label contract`.

### Task 2 — Explicit H5i read-only adapter and PIT panels

**Files:** add `src/h5i_selector_ic.py`, `tests/test_h5i_selector_ic.py`.

1. RED: fake database tests assert `read_only=True`, bounded calls, preserved `ts`, timestamp at cutoff included / after cutoff excluded, date-only `D` valuation rejected, invalid/missing schemas fail closed, membership uses prior-session bars, `roe`/`mf_net` remain unsupported, DB closes on exceptions, and no legacy DuckDB/fallback import is made.
2. Run `python -m pytest tests/test_h5i_selector_ic.py -q`; confirm expected missing module failures.
3. Implement injected DB factory and validated read-only SQL extraction; batch bars for requested range and use as-of valuation timestamps only when provable. Keep source availability and missingness separate from scores.
4. Re-run focused tests, then `python -m pytest tests/test_h5i_selector_ic.py tests/test_selector_ic_contract.py -q`.
5. Commit: `feat(ic): add read-only h5i PIT diagnostic panels`.

### Task 3 — Immutable non-production diagnostic bundle

**Files:** add `src/diagnostic_ic_bundle.py`, `tests/test_diagnostic_ic_bundle.py`.

1. RED: tests cover absolute-root requirement, repository/protected path and symlink rejection, collision/no-overwrite, canonical manifest identity, per-file SHA-256, manifest-last behavior, atomic failure cleanup, successful verify, tamper detection, and all three non-authoritative flags.
2. Run `python -m pytest tests/test_diagnostic_ic_bundle.py -q`; confirm failures before implementation.
3. Implement same-parent temporary bundle plus atomic final rename and strict final/partial collision refusal. Never import the Observation Epoch-bound evidence writer.
4. Re-run focused tests; include a test using a temp parent and prove existing bundle bytes remain unchanged after a repeated identity attempt.
5. Commit: `feat(ic): add immutable diagnostic staging bundle`.

### Task 4 — Explicit CLI, orchestration, and contract integration

**Files:** add `scripts/stage_h5i_selector_ic.py`, `tests/test_stage_h5i_selector_ic.py`; update test partition manifest only if CI classifies the new test specially (otherwise leave CI configuration untouched).

1. RED: CLI tests require every explicit path/date argument, reject default/protected output roots and reversed/invalid ranges, test missing DB/calendar/schema and unsupported factors, assert manifest identities/flags, and prove no path reference to `data/ic` or production writer is invoked.
2. Run `python -m pytest tests/test_stage_h5i_selector_ic.py -q`; confirm failures before implementation.
3. Implement CLI with no environment-derived DB/output defaults; require explicit calendar file and record SHA/provenance (unknown provenance remains an explicit caveat). Bound the requested signal range and include enough post-signal sessions only to settle the requested horizons.
4. Re-run all new tests together and `python scripts/stage_h5i_selector_ic.py --help`.
5. Commit: `feat(ic): add explicit h5i IC staging command`.

### Task 5 — Bounded local read-only integration and protected-state audit

**Files:** tests only if a contract gap appears; generated staging bundle must be under a fresh `%TEMP%` directory, never tracked or in the repository.

1. Preflight explicit local paths; record H5i/calendar presence, schemas, file identity, and read-only open. If required local H5i/calendar input is unavailable, stop integration and report `NOT_RUN` rather than inventing data.
2. Snapshot before-state hashes for `data/ic`, `data/weights.json`, frozen weight snapshot, selector weight policy, Observation Epoch, PaperBook/broker state; record current service PIDs without restarting anything.
3. Run a bounded real H5i range large enough to mature requested horizons; output only to a fresh external temp root. Keep the source DB connection read-only. No StockDB/H5i updates.
4. Verify bundle hashes/manifest, statuses and coverage; assert unsupported ROE/MF remain null/unsupported and `paperbook_fill_replay=false`.
5. Recompute protected-state hashes and compare before/after; report exact PASS/FAIL/NOT_RUN. Run focused tests plus full applicable regression under available supported environments, report skips separately.
6. Commit only code/docs/tests; exclude staging data and runtime/market artifacts.

### Task 6 — Independent review and handoff

1. Ask a fresh reviewer agent to inspect the completed branch against the approved spec and the five review-focus bullets; reviewer does not edit files.
2. Resolve every actionable finding with a failing regression first; rerun focused tests and applicable full suite.
3. Review `git diff --check`, branch diff, and protected-path no-diff list; verify no production consumer, service, state, weight, or `data/ic` path changed.
4. Report commit SHA(s), tests, local integration outcome, unsupported factors, and any residual limitation. Do not push, open/merge PR, deploy, restart services, or modify the user-owned main-checkout README without fresh authorization.

## Acceptance criteria

- All new pure and fake-source tests pass; no skip is reported as a pass.
- If local integration runs, H5i opens read-only and a verified immutable bundle is written outside repository/protected paths; otherwise state exactly why it is `NOT_RUN`.
- `roe` and `mf_net` remain explicitly unsupported; any other incomplete source remains incomplete/null.
- `data/ic`, weights, frozen selector snapshot, PaperBook/broker, Observation Epoch, service state, and behavior-sensitive production modules have no diff or changed hash.
- Full applicable test suites and independent review results are reported without promotions or claims of live/realized performance.
