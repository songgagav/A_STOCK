# H5i PIT Selector-Factor IC Contract

**Status:** Draft for user review. The user approved the recommended architecture direction on 2026-10-21; this written specification must still be reviewed and approved before an implementation plan or product-code work begins.

## 1. Purpose

Define a trustworthy, point-in-time (PIT) contract for the factor IC curves consumed by the selector weight optimizer and premarket health gate. The current IC-curve health check fails because the official refresh path is not usable against the deployed H5i data source and does not cover the complete factor registry. The repair must establish what each curve means, where each historical input comes from, and which consumers may use it before any production curve is replaced.

Success is **not** “make `premarket:ic_curve` green by creating five files.” Success is a reproducible, versioned IC dataset whose signal values, universe, cutoff, forward labels, missingness, direction, and consumer interpretation are explicit and evidenced. If a source is unavailable or not PIT-safe, the result remains incomplete and the health gate remains blocked.

## 2. Non-negotiable boundaries

Throughout design and implementation:

- Do not change frozen selector weights, `data/weights.json`, the frozen weight snapshot, or automatic reweighting policy.
- Preserve `RANK_BY_FUSION=0`, DRL/Fusion `shadow`, paper broker, and Alpha `not_promotable`.
- Do not change live selector ranking, target construction, trading rules, costs, PaperBook, broker wiring, or Observation Epoch.
- Do not write to `data/ic/`, update a weight, relax/waive a health check, or count an observation day during the diagnostic and parity stages.
- Do not use retired DuckDB as a silent fallback. Missing input, invalid provenance, or insufficient coverage must produce an explicit incomplete/error status, never a neutral or zero IC curve.
- The existing H5i technical-factor diagnostic remains diagnostic-only. It is not an authoritative producer, does not satisfy `check_ic_curve()`, and its results must not be copied into the production curve files.

## 3. Verified repository facts and contract gaps

The deployed tree at `main@dd472e2ad5eba31d869cce3d5457d23c48a99a3c` has these relevant facts:

1. `weight_optimizer.ALPHA_FACTORS` contains five keys: `vol`, `mom_rev`, `pb_rev`, `roe`, and `mf_net`. Its current expected raw IC directions are `+1, -1, +1, +1, +1` respectively. The optimizer defaults to H5 IC while health currently checks H20 freshness.
2. `premarket_healthcheck.check_ic_curve()` checks all five keys, but `ic_curve_refresh.ALPHA_FACTORS` refreshes only `vol` and `mom_rev` and reads its latest date/window through the retired DuckDB path.
3. The current `ic_backtest.build_factor()` references `factor_library.FACTORS`, which is not defined in deployed `factor_library.py` (it defines `FACTOR_REGISTRY` instead). Replacing only the database connection would therefore not repair the current producer.
4. The live selector's current alpha component formulas are: lower 20-return annualized volatility scores higher; lower 20-day return scores higher for `mom_rev`; lower PB scores higher; higher ROE scores higher; and higher stock-level `net_flow / amount` scores higher.
5. The current historical selector weighted total includes `vol` and `mom_rev`, but not the live `pb_rev`, `roe`, or `mf_net` weighted terms. A component IC series is not a full historical replay or proof that historical and live selection/targets are identical.
6. H5i provides historical `daily_bars`, `valuation`, and `financials` access paths. `valuation` has an as-of query, but the `roe` source must be keyed to actual disclosure availability, not merely report period. The deployed `get_financials_asof()` contract alone is not accepted as proof of announcement-time PIT until its source fields and behavior are verified.
7. Individual-stock `money_flow` is not migrated to H5i. H5i `money_flow_estimate` is an industry snapshot and cannot substitute for stock-level flow. No `mf_net` historical curve is authoritative without a point-in-time stock-level source aligned to the live signal cutoff.
8. There is a source-orientation contract to resolve before any curve can reach the optimizer: the existing `mom_rev` optimizer entry loads a curve named `mom_20` and applies direction `-1`, whereas a curve computed directly from the live reverse-momentum score would already have the reverse sign. Applying both would invert the intended meaning. Volatility score orientation and the old curve meaning must likewise be evidenced, not inferred from filenames.
9. The official holiday/calendar file has an upstream-provenance gap. It may be used as a diagnostic input with that caveat, but it cannot silently establish an authoritative historical trading-session sequence.
10. The deployed Signal Freeze contract fixes the daily target snapshot at 09:25 Shanghai time. This is not automatically the same as the prior diagnostic's signal-at-close convention or the old `ic_backtest` t+1 label convention. The authoritative signal timestamp, last-known feature cut, and return-label entry/exit anchor must be reconciled with the Signal Snapshot, TargetContract, and PaperBook fill contract before an implementation can assign a precise horizon formula.

## 4. Approaches considered

### A. Retarget the legacy refresh to H5i

Change the old DuckDB query to H5i and add the three missing factor names. This is rejected: the producer currently references an absent factor registry, the score and direction contracts are inconsistent, and valid PIT sources for all five factors are not established. It could create production-looking files without authoritative semantics.

### B. Establish a versioned PIT factor contract and stage outputs separately — selected

Specify each factor's exact signal, source table, PIT availability rule, expected direction, eligible universe, forward-return labels, coverage requirements, and consumer mapping. Build only read-only extraction and isolated staging outputs while source parity is tested. Factors with a missing or unproven source remain `unsupported`/`incomplete`; no production consumer changes until the full contract is independently reviewed and separately approved.

This is the recommended approach because it makes partial evidence visible without allowing it to masquerade as production readiness. It may leave the current health FAIL in place until every active factor and consumer contract is proven.

### C. Relax the health gate while weights are frozen

Treat missing curves as warnings because the active weight snapshot is frozen. This is rejected for this work: it would suppress an active-factor evidence gap and change observation admission criteria. Any health policy change requires a separate explicit design and approval.

## 5. Proposed factor contract

The implementation plan must create a machine-readable or equivalently explicit per-factor contract before producing a curve. At minimum it must identify:

| Factor | Score meaning | Required historical source | Required PIT rule | Current status |
|---|---|---|---|---|
| `vol` | Low 20-return annualized volatility scores higher; live formula in `factor_library.score_factor` | H5i daily bars | Only closed bars available by `decision_cutoff`; verify return source, 20-observation window, `ddof`, annualizer, and clipping exactly | Implementable candidate; parity with legacy `vol` curve and optimizer sign is unresolved |
| `mom_rev` | Lower 20-day return scores higher; live formula in `factor_library.score_factor` | H5i daily bars | Only closed bars available by `decision_cutoff`; verify 20-bar endpoint and score/curve direction | Implementable candidate; current optimizer uses `mom_20` with a negative direction, so consumer sign must not be double-applied |
| `pb_rev` | Lower PB scores higher | H5i `valuation` | Latest valid valuation with effective timestamp no later than `decision_cutoff`; preserve missingness and stale-source rules | Candidate only; historical selector/optimizer parity and full source coverage require proof |
| `roe` | Higher ROE scores higher | H5i `financials` | The disclosure/availability timestamp must be no later than `decision_cutoff`; report-period date alone is insufficient | Blocked until announcement-time PIT is verified |
| `mf_net` | Higher stock-level net-flow/amount ratio scores higher | Historical point-in-time stock-level money-flow snapshots | Snapshot capture must precede or equal the selector's documented signal cutoff; no future refresh may leak into `t` | Blocked: no H5i stock-level history currently exists; industry `money_flow_estimate` is not a substitute |

The legacy diagnostic output for `vol` and `mom_rev` is a comparison input only. Its all-bar-eligible universe and local-calendar caveat must not be relabeled as exact live-selector or optimizer parity.

## 6. Data flow and output boundary

The proposed producer has four logical stages:

1. **Preflight:** resolve explicit H5i path, verified session calendar identity, factor-contract version, date range, source schemas, decision cutoff, and label-anchor convention. Open H5i read-only. Reject a missing source or an unverified calendar for authoritative mode.
2. **As-of factor panel:** compute each factor's cross-sectional values using only rows known by the exact decision cutoff. Keep `signal_date`, `last_closed_bar_date`, and `decision_cutoff` distinct. Retain source row counts, missingness, effective timestamps, and per-date coverage. Never use the current active-symbol list or a latest-value snapshot to rewrite historical membership.
3. **Forward labels and IC:** the first implementation plan must first settle the mapping from the 09:25 signal to the first eligible label price using the existing Signal Snapshot, TargetContract, and PaperBook semantics. The previous diagnostic's close-at-`t` then `t+1 ... t+h` convention is not presumed authoritative for a premarket signal. Once the anchor is approved, define exact horizon sessions and labels, require every constituent session and return to be present, and record the formula in the run identity. Calculate cross-sectional Spearman IC with average ranks; fewer than five valid pairs, constant ranks, immature horizons, or incomplete source rows yield explicit null/status values, never zero.
4. **Staging bundle:** write outputs only under a caller-selected non-production staging root. The bundle contains factor data, a manifest, source/code/schema/calendar identities, score and direction versions, return formula, horizon settlement dates, per-date counts, missingness, and output hashes. Finalization is atomic and immutable; repeat identity verifies byte-for-byte or fails.

The staging schema may preserve the existing horizon set `{1, 3, 5, 10, 20}` for comparison, but it must carry a new semantics/version identity and must not use production filenames in `data/ic/` as an implicit authority. The bundle manifest must state `diagnostic_only=true`, `authoritative=false`, and `observation_eligible=false` until all later promotion gates are satisfied.

## 7. Missingness, failures, and promotion boundary

- A factor with no PIT source, unverified availability time, invalid schema, or inadequate coverage is `unsupported` or `incomplete`, with a reason and counts. It is not assigned a neutral synthetic history.
- A date/horizon without all required forward sessions is `not_mature` or `incomplete`; it is not settled and is not omitted to shorten the holding period.
- Invalid/missing observations are separately counted from valid neutral scores. No forward fill, zero fill, last-known-current value, or industry-to-stock substitution is permitted.
- A mismatch between live selector score, historical reconstruction, legacy curve orientation, or optimizer expected direction is a failed parity gate. It requires a versioned consumer/contract decision; it must not be fixed by taking absolute IC/ICIR or flipping signs opportunistically.
- The existing refresh command must remain out of production until its missing registry/data-source issues are repaired and the staging results have passed review. No direct `data/ic/` write is part of the first implementation slice.
- Any eventual authoritative refresh requires a separate rollout approval after tests, independent review, fresh source/coverage evidence, an explicit production-write plan/rollback, and confirmation that frozen weights and all paper/shadow modes remain unchanged.

## 8. Validation requirements

The implementation plan must cover at least:

- Unit tests for every score's bounds, direction, exact windows, invalid values, and neutral-versus-missing accounting.
- PIT boundary tests: a source row with availability at `t` may be used; a row available after `t` must not affect the score at `t`.
- Exact signal-time and forward-label tests proving the decision cutoff, last-known feature date, first eligible label price, H1/H5/H20 session mapping, horizon-specific maturity, and missing-intermediate-session handling. These tests must enforce the approved premarket/execution convention, not copy the close-signal diagnostic convention.
- Rank tests for average ties, fewer than five pairs, non-finite data, and constant ranks.
- Factor-source tests proving `pb_rev` is date-as-of, `roe` uses verified disclosure availability, and `mf_net` refuses industry-level or non-PIT substitutes.
- Consumer-direction tests proving curve storage orientation and `FACTOR_EXPECTED_DIRECTIONS` are applied exactly once; specifically cover `mom_rev`/`mom_20`.
- Integration tests against a bounded read-only H5i range, with per-factor coverage and missingness report. Existing production curves are comparison-only, not an assumed golden output where their generation code is broken or semantics are undocumented.
- Protected-state checks before/after confirming unchanged `data/ic/`, weights, frozen selector snapshot, PaperBook/broker state, and Epoch. The initial implementation's output must remain outside protected production paths.
- Focused and full applicable regression suites; report PASS/FAIL/SKIP/NOT_RUN. Missing dependencies or skipped real-data coverage are not passes.

## 9. Release gates and sequencing

1. User reviews and approves this written design.
2. Only then create a detailed implementation plan and obtain approval of that plan and execution method.
3. Implement in an isolated worktree with tests first. The first code slice must be diagnostic/staging-only and cannot satisfy the production health gate.
4. Independently review the resulting implementation and resolve all findings. Keep PR #24's existing diagnostic branch separate; do not merge it by implication of this spec.
5. Reassess the blocked `pb_rev`, `roe`, `mf_net`, calendar, and optimizer-direction contracts with real source evidence. If any remains unproven, report the curve set incomplete and retain the health FAIL.
6. Any change to the authoritative writer, optimizer consumer, health gate, live selector, production deployment, or Observation Epoch requires a separate explicit approval and a new acceptance review.

## 10. Explicit non-goals

This specification does not authorize:

- changing selector or historical selection behavior;
- changing weights, enabling auto-reweighting, or promoting Alpha/Fusion/DRL;
- using industry money-flow as per-stock `mf_net`;
- downloading/backfilling historical money-flow data from an unreviewed source;
- relaxing health checks or freezing a replacement Epoch;
- updating current authoritative IC curves, restarting a service, rebooting the PC, or beginning the five-day observation count.
