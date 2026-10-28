# Paper Stability Fix Design

Date: 2026-10-26

## Goal

Repair the paper/simulation evidence and runtime observability chain without changing selection, ranking, target weights, matching, trading rules, DRL behavior, or broker mode. Real trading remains out of scope until the paper path is stable and separately approved.

## Non-negotiable safety boundary

- `TRADE_BROKER=paper` remains the only permitted broker mode.
- `DRL_PLAN_MODE=shadow`, `FUSION_WEIGHT_MODE=shadow`, and `RANK_BY_FUSION=0` remain unchanged.
- No code in this phase may connect to, submit to, or enable a real broker.
- No automatic promotion, pointer update, selector update, target-plan update, or production-config update is allowed.
- Service reloads, if needed after verification, must be controlled and paper-only.

## Observed failures

1. A reconstructed `PaperBook` does not rehydrate `trades_today` from the current-day `trades_history`, so the close summary can report zero trades while `live_state` contains fills.
2. IC-curve refresh still attempts the retired legacy DuckDB path; the current primary market store is H5i/StockDB. Existing factor support and missing fundamental inputs must be explicit rather than silently treated as fresh.
3. Live-price fallback reaches an H5i reference price, but source-attempt failures are not sufficiently visible in the runtime receipt. This prevents distinguishing a live feed outage from an intentional reference fallback.
4. The optional P08 simulator has no current-day frame because its runner is not managed by the daemon. A missing frame must remain explicit and must not be misreported as a successful simulation or silently repaired by mutating production behavior.

## Chosen approach

Use narrow contract fixes with test-first implementation:

1. Add a regression test for current-day PaperBook trade rehydration, then restore the in-memory daily ledger from the persisted history.
2. Add H5i-backed IC refresh coverage for the currently supported price factors and make unsupported/missing inputs return explicit blocked/unavailable evidence. Do not fabricate factor values or downgrade missing inputs to zero.
3. Add source-attempt provenance and failure reasons to the paper live-state diagnostics while preserving the existing fallback order and prices.
4. Keep P08 as an optional observer. Record `NO_FRAME`/not-running explicitly and do not auto-start it from the trading daemon in this phase.
5. Add paper-only guard tests and run focused tests followed by the full regression suite.

## Rejected approaches

- Changing health checks only: this would hide the ledger and data-source defects.
- Rewriting the realtime engine or strategy selection: excessive risk and outside scope.
- Auto-starting P08 or connecting a real broker: not authorized and incompatible with the paper-stability gate.
- Treating missing IC curves, live quotes, or fills as zeros: destroys evidence semantics and could create false readiness.

## Acceptance criteria

- A restored PaperBook and the close summary expose the same current-day trade count.
- IC refresh no longer depends on a missing legacy DuckDB file for H5i-supported factors, and unavailable factors remain explicitly non-ready.
- Runtime evidence identifies live-source attempts, fallback source, and failure reason without changing target or execution behavior.
- No-fill and no-frame states remain explicit, not successful zero-valued results.
- Focused and full tests pass; protected strategy/broker paths show no unintended diff.
- The branch and runtime remain paper-only throughout this work.
