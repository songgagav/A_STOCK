# Data-Source Batch 5–6 Implementation Plan

## Scope

- Batch 5: real h5i sink/probe adapter around existing `bars_ingest`/`h5i_sync`.
- Batch 6: read-only source health collector, dashboard endpoint, and DB monitor card.
- Explicitly excluded: signal freeze, PaperBook, daemon lifecycle, source network adapters,
  h5i schema changes, and staging cleanup.

## Execution ledger

- [x] Batch 5 RED tests for canonical-to-h5i mapping, failure states, probe hash, and missing runtime.
- [x] Batch 5 implementation and `.venv310`/`.venv314` focused verification.
- [x] Batch 6 RED tests for blocked/ready/invalid sidecar status.
- [x] Batch 6 implementation and focused dashboard contract verification.
- [ ] Full regression in both interpreters.
- [ ] Self-review, commit, push, and update Draft PR #13.

## Review focus

- No h5i schema or existing writer changes.
- Non-unit adjustment factors fail closed.
- Missing h5i is visible as `blocked`, never an empty successful result.
- Dashboard endpoint is additive; existing API shapes remain unchanged.
