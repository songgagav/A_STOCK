# Pre-Observation Hardening

## Scope and baseline

Implement the user's latest A_STOCK taskbook on `feature/pre-observation-hardening`, starting at PR #17 commit `17b7bf2a8ba7b5c687215abf40957c3c48a3f777`. The user confirmed the proposed implementation on 2026-10-10 and previously authorized uploading the verified work to GitHub.

The result is an auditable candidate for a new canonical freeze. Real observation requires five consecutive qualified trading days after that freeze; tests, historical replay, and backfilled artifacts never count as observation days.

## Binding constraints

Keep `RANK_BY_FUSION=0`, `DRL_PLAN_MODE=shadow`, `FUSION_WEIGHT_MODE=shadow`, `TRADE_BROKER=paper`, and Alpha `not_promotable`. No automatic promotion. Preserve selection, target weighting, fee/slippage, rebalance cadence, risk limits, and broker wiring except the explicitly requested correctness fixes below. PR #17, existing branches, main, historical bundles, and production runtime data are preserved. Upload the new branch and create a reviewable PR; merging remains a separate decision.

## Runtime time and evidence receipt: T01–T02

Interpret naive runtime datetimes as Asia/Shanghai and convert aware instants into that timezone at both `run_tick` and `_freeze_or_load_targets`. A 09:25 call must persist and read a ready, valid snapshot. Cover naive 09:24/09:25/09:26 and equivalent UTC/Shanghai instants.

Carry per-code price provenance through runtime reporting: `akshare_spot`, `h5i_reference`, `h5i_reference_pool`, and `h5i_reference_held`. Reference prices must not claim realtime freshness; held-position reference use makes health DEGRADED. Preserve the existing candidate/watchlist universe and fallback eligibility.

When embedded live targets are absent, portfolio reporting may consume a referenced authoritative signal snapshot only through the existing schema/content-hash validator, with matching trading day and advertised hash. Report `source=signal_snapshot` and accurate target/held/pending-buy counts. Invalid, missing, or mismatched receipt evidence blocks construction and cannot fall back to a DRL plan.

## Observation identity: T03–T05

New epochs use schema v2. Identity binds code SHA, canonical config identity, stable data lineage, and production state. Stable lineage describes source, schema, routing, universe, and trading-calendar source/version; its hash excludes daily market, snapshot, positions, orders, and fills files. Daily artifact hashes remain in immutable bundles. Distinct daily artifacts with unchanged lineage preserve epoch identity.

Production state includes rank mode, DRL plan mode, Fusion weight mode, broker, Alpha evidence status, and DRL plan contract. New epochs require `implemented_default_shadow`; mode/config/code/lineage changes produce new identities. Preserve verification of existing v1 epochs and legacy Phase A bundles; do not rewrite them. Continue rejecting mixed-epoch OOS datasets.

## Historical DRL isolation: T06

Historical PPO observations use neutral sentiment/stance and an unmodified historical/base prior. Today's LLM brief cannot alter either. Any retained current inference overlay executes after training and has separate weights and provenance: `training_brief_used=false`, `training_weights`, `inference_weights`, and current-brief metadata. Evidence must not present overlay weights as PPO-learned weights. Preserve the established reward/model architecture.

## Reward configuration: T07–T09

All reward-weight paths normalize finite, nonnegative values with positive total to sum one, including default `0.6/0.4/0.15`. Explicit invalid inputs fail or produce a traced degraded/default result. Preserve `attr_weight` through LLM parsing, incremental learning, daily orchestration, and persisted configuration.

Record `generated_on` and the next actual trading-session `effective_from`. Resolve using the existing authoritative calendar; unknown calendar means null and `pending_calendar_resolution`, never a guessed weekday. Use the existing atomic JSON writer. Corrupt, partial, and schema-invalid reads must expose default/degraded provenance. Daily orchestration must truthfully report whether writing succeeded.

## Dividend evidence: T10

For a position with a provable single acquisition basis, holding days are `ex_date - buy_date`, covering under 30, 30–364, and at least 365 days. Invalid and future dates cannot become a precise zero-day history. Aggregate positions with multiple acquisitions or insufficient acquisition evidence have explicit approximate/unknown basis. Retain existing rate schedule and execution architecture; a full tax-lot ledger is deferred.

## Verification and delivery

Each fix has a focused regression that fails before the source change and passes afterward. Task reviews check requirements and code quality. Final verification includes applicable core, DRL, h5i, and security checks, reporting PASS/FAIL/SKIP/NOT_RUN honestly. Existing warning/skip baselines remain distinguishable from newly introduced failures.

Commit the implementation in reviewable slices, push only the new branch, and verify the remote SHA and CI. A runbook records freeze prerequisites, observation qualification, pending real-day evidence, and deferred PR #13/#15 integration. Freeze/start of runtime observation must use actual deployment identity and evidence; it cannot be asserted from offline tests.
