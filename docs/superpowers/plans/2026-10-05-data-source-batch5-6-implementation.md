# Data-Source Batch 5–6 Implementation Plan

## Scope

- Batch 5: real h5i sink/probe adapter around existing `bars_ingest`/`h5i_sync`.
- Batch 6: read-only source health collector, dashboard endpoint, and DB monitor card.
- Follow-up completion: real network adapters, opt-in daemon scheduling, shadow/enforce
  guardrails, occupied-unknown human review, and conservative staging retention.
- Still excluded: signal freeze, PaperBook, h5i schema changes, and real-money execution.

## Execution ledger

- [x] Batch 5 RED tests for canonical-to-h5i mapping, failure states, probe hash, and missing runtime.
- [x] Batch 5 implementation and `.venv310`/`.venv314` focused verification.
- [x] Batch 6 RED tests for blocked/ready/invalid sidecar status.
- [x] Batch 6 implementation and focused dashboard contract verification.
- [x] Full regression in both interpreters (focused regression; full-suite counts recorded
  in the PR update after execution).
- [x] Self-review, commit, push, and update Draft PR #13 with the verified local baseline.

## Follow-up completion ledger

- [x] Baostock, mootdx, and ZZShare adapters with lazy optional dependencies.
- [x] Unified canonical normalization and quality gate before staging.
- [x] Daemon hook is explicit opt-in and runs once per trade day.
- [x] Shadow mode stages only; enforce requires an explicit confirmation string.
- [x] `occupied_unknown` creates an audit marker and has a CLI/manual resolution path.
- [x] Staging cleanup defaults to 30 days and preserves staged/uncertain/orphaned data.
- [x] Baostock: five historical trading days in isolated shadow returned
  `shadow_staged`, coverage `1.0`.
- [x] ZZShare: five historical trading days in isolated shadow returned
  `shadow_staged`, coverage `1.0`.
- [ ] mootdx: dependency is installed, but the real client returned no rows for the
  sampled historical symbol; Router correctly returned `blocked`. Do not promote.
- [ ] Continuous production five-day shadow observation and manual promotion remain
  external operational work; the daemon switch stays disabled by default.

## Review focus

- No h5i schema or existing writer changes.
- Non-unit adjustment factors fail closed.
- Missing h5i is visible as `blocked`, never an empty successful result.
- Dashboard endpoint is additive; existing API shapes remain unchanged.
- Router changes are not consumed by signal freeze or PaperBook unless an operator
  explicitly enables the production switch.
