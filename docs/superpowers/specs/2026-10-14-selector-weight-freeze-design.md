# Selector Weight Freeze for Paper/Shadow Observation

**Status:** Draft for user review. The conversational design is approved; implementation is not authorized until this written specification is reviewed and approved.

## 1. Purpose and constraints

The paper/shadow observation window needs one stable, auditable selector-weight vector. Today the live selector can change its effective weights through two independent paths: `data/weights.json` is rewritten by the daily optimizer, and `factor_library._apply_factor_health()` can zero factors as new IC curves arrive. Stopping only one path is insufficient.

At the Oct 14, 2026 read-only check, `selector_weights(as_of=None)` matched the Epoch v2 frozen vector exactly (maximum absolute delta `0.0`), even though the raw `weights.json` SHA had changed. The replacement snapshot must capture the effective vector, not restore or bless the changed raw file. The nine captured values are:

| Factor | Frozen weight |
|---|---:|
| signal | 0.42489270386266104 |
| trend | 0.17501192179303773 |
| govern | 0.199928469241774 |
| liquidity | 0.10002384358607537 |
| vol | 0.025035765379113027 |
| mom_rev | 0.0 |
| pb_rev | 0.025035765379113027 |
| roe | 0.025035765379113027 |
| mf_net | 0.025035765379113027 |

The sum is 1 within floating-point tolerance. Zero is valid for a disabled factor; negative, non-finite, missing, or extra factor entries are invalid.

Keep all existing production boundaries unchanged:

```text
RANK_BY_FUSION=0
DRL_PLAN_MODE=shadow
FUSION_WEIGHT_MODE=shadow
TRADE_BROKER=paper
alpha_evidence_status=not_promotable
no automatic promotion
```

Do not alter factor scores, target-weight allocation, risk limits, order cadence, costs, slippage, broker wiring, StockDB/H5i lineage, or historical evidence. StockDB repair is a separate authorized maintenance task and is not part of this design.

## 2. Approaches considered

1. **Stop only the daily JSON writer.** Rejected: live factor-health gating still changes effective weights when IC data changes, and the CLI can separately write or delete `weights.json`.
2. **Disable factor health and restore/pin `weights.json`.** Rejected: direct writer paths and fallback behavior remain authoritative; configuration drift could silently resume adaptive weighting, and raw-file identity would still be confused with effective weights.
3. **Use a validated immutable effective-weight snapshot behind an explicit frozen policy.** Selected: one snapshot is the sole live authority; optimizer output becomes suggestion-only; the live health overlay is bypassed; persisted selections and target snapshots carry the same identity; invalid inputs fail closed. Adaptive behavior remains available when explicitly configured outside the observation window.

## 3. Policy and snapshot contract

### Policy selection

Add an explicit `SELECTOR_WEIGHT_MODE=adaptive|frozen` setting with **no implicit default**. Every production/research/test launcher that resolves selector weights must declare its mode; an unset or unknown value is a configuration error, never an implicit switch to adaptive. Non-observation workflows retain the existing dynamic behavior only when they explicitly set `adaptive`. A new observation Epoch may be frozen only when the effective runtime mode is `frozen`.

In `frozen` mode, require `SELECTOR_WEIGHT_SNAPSHOT_SHA256` to be a 64-character lowercase hexadecimal digest. The snapshot is stored under `data/evolution/selector_weights/` using that digest as its filename (`<sha256>.json`). Hash the exact canonical UTF-8 JSON bytes with SHA-256; the reader recomputes and compares the digest before parsing weights. The file is create-once, written atomically, and never replaced by runtime code. A Windows ReadOnly attribute is an operational aid, not the integrity check.

The schema-v1 snapshot contains:

```json
{
  "schema_version": 1,
  "policy": "selector-weight-freeze/v1",
  "captured_at": "ISO-8601 timestamp with timezone",
  "weights": { "signal": 0.0 },
  "provenance": {
    "source_mode": "adaptive-effective-selector",
    "weights_json_sha256": "...",
    "factor_health_enabled": true,
    "factor_health_input_sha256": {}
  }
}
```

The actual file includes all nine factor keys and captured values. Canonical serialization sorts keys, uses compact separators, UTF-8, and rejects NaN/Infinity. Snapshot creation computes the current live effective vector once through the existing adaptive resolver, captures the source file/health-input hashes and capture time, writes to a new digest-named path without overwrite, and prints the resulting digest for configuration and Epoch binding. A failed/partial write must leave no authoritative snapshot.

### Resolution semantics

- `selector_weights(as_of=None)` in explicitly configured `adaptive` mode retains current dynamic-file loading, static fallback, and factor-health overlay.
- `selector_weights(as_of=None)` in `frozen` mode reads only the digest-selected snapshot. It does not read `weights.json`, static `SCORE_WEIGHTS`, latest IC curves, or `_apply_factor_health()`.
- Frozen-mode absence of the expected digest/file, byte-hash mismatch, schema error, factor-key mismatch, invalid number, or non-unit sum raises a typed selector-weight policy error. It must never return a static/default/adaptive fallback.
- Historical calls with `as_of=<date>` preserve the existing point-in-time logic and continue to apply factor health only through that date. The live freeze must not make historical results use today's frozen vector.
- Keep the existing dictionary-returning selector API compatible. Add a resolver/context API for production selection to obtain both the weights and their identity in one read, preventing a race between reading values and recording provenance.

## 4. Optimizer output and mutation boundaries

Use one shared persistence function for `run_daily.self_closed_loop()` and `weight_optimizer` CLI writes so no writer can bypass the policy.

- In `adaptive` mode, preserve the current `data/weights.json` behavior.
- In `frozen` mode, optimizer results are immutable, content-addressed proposals under `data/weight_proposals/<trading-day>/<proposal-sha256>.json`. They are explicitly non-authoritative and cannot be read by the live selector.
- The daily feedback record identifies the proposal path/hash and labels it `proposal_only`; it must not label proposal weights as effective weights. Optimizer failures remain non-blocking for the rest of the daily pipeline, as today.
- CLI `--write` in frozen mode creates a proposal only. CLI `--reset` in frozen mode returns a clear error and does not delete `data/weights.json` or the frozen snapshot.
- No code path in frozen mode may overwrite or delete the snapshot, or overwrite/delete `data/weights.json` as an authoritative selector update. Do not restore the old `weights.json` bytes during this work.

## 5. Selection, signal-freeze, and runtime behavior

The selected weights must remain identifiable through every route that can supply a live target:

1. Live `RotationSelector.select()` records `selector_weight_identity` with mode and snapshot SHA in the returned selection artifact. It obtains weights and identity atomically through the new resolver/context API. Historical selection is unchanged.
2. In frozen mode, `load_targets()` may reuse a same-day or cross-day selection only if its recorded mode and snapshot SHA match the active frozen policy. Unidentified or mismatched selection artifacts are skipped; the loader may then compute a fresh live selection using the verified frozen snapshot. DRL plans remain blocked by the existing `DRL_PLAN_MODE=shadow` contract.
3. Signal snapshot schema v2 includes the same `selector_weight_identity` in both its input payload and integrity hash. A schema-v1 signal snapshot remains readable for legacy adaptive operation but cannot authorize frozen-mode target consumption. The snapshot reader receives the expected policy identity and rejects absent or mismatched identity.
4. `realtime_engine` applies a selector-weight readiness gate independently of the existing signal-freeze `shadow/enforce` setting. If the frozen snapshot is invalid or no target/selection artifact can prove the matching identity, reject new selection-driven targets and block target-driven rebalancing. Preserve current PaperBook state and keep independent emergency/risk protections active; never fall back to a dynamic, static, DRL, or pre-freeze selection as a substitute.
5. A valid existing PaperBook position is not liquidated merely because the selector snapshot is unavailable. Recovery requires correcting the snapshot/config and passing the normal identity preflight; no automatic same-session recovery after a failure latch.

## 6. Evidence, Epoch identity, and operator visibility

- Record `selector_weight_mode=frozen` and `selector_weights_snapshot_sha256` as explicit fields in the observation's `experiment_identity`. Compute `experiment_hash` from the canonical experiment payload excluding the `experiment_hash` field itself (avoiding a circular digest), then include the full mapping in `epoch_id`; a changed mode or snapshot digest must change the new Epoch identity. An observation-specific preflight must require these fields without narrowing the generic Epoch builder used by research fixtures. Add a create-only freeze command that writes/verifies the replacement at `data/evolution/epochs/<epoch_id>.json`. The existing Oct 11 Epoch v2 record remains byte-for-byte untouched.
- Every new observation Evidence Bundle and daily runtime receipt carries the bound experiment identity. Daily optimizer proposal hashes may be recorded as diagnostics, but never enter the effective selector identity.
- The dashboard `/api/weights` continues returning `weights` as the effective weights for compatibility. In frozen mode those are read from the verified snapshot and accompanied by `mode`, `snapshot_sha256`, and `effective_source=frozen_snapshot`; proposal weights appear in a separate `proposal` field. An unreadable snapshot reports `ok=false`, not a plausible empty/default map.
- Any selection artifact without the matching weight identity cannot be used to qualify a day. No Oct 12–14 artifact is backfilled; the old Epoch stays at zero qualified days. A new five-day count begins only after code/config/snapshot deployment, StockDB→H5i freshness, runtime identity, replacement Epoch, and real-session preflight all pass.

## 7. Failure handling

| Failure | Required behavior |
|---|---|
| Missing/invalid mode | Configuration error; do not silently default to or reinterpret as adaptive. |
| Frozen mode without expected SHA or snapshot file | Typed policy error; no static/dynamic fallback and no new selection. |
| Snapshot SHA/schema/weights invalid | Fail closed; mark health degraded/blocked, do not create or consume a new target. |
| Optimizer proposal write fails | Record feedback error; continue unrelated daily work, but never update authoritative weights. |
| Cached selection/signal snapshot lacks matching identity | Reject it for frozen mode; reselect only through the verified frozen resolver, otherwise block. |
| Epoch identity lacks frozen mode or snapshot digest | Refuse to start a new observation Epoch. |

Errors must be observable in the daily summary, selector/runtime health receipt, and logs without exposing unrelated secrets or silently changing policy.

## 8. Verification and rollout gates

Focused tests must prove:

1. Snapshot validation accepts the captured 9-key vector (including zero), and rejects missing/extra keys, negative/NaN/Infinity values, bad sum, malformed schema, missing file, and byte-hash mismatch.
2. In frozen mode, changing `weights.json`, IC curves, factor-health flags, or optimizer results does not change the live effective vector; no static fallback is used on any failure.
3. Historical `as_of` selection retains its existing as-of IC behavior and does not consume the live snapshot.
4. Daily and CLI optimizer paths write proposal-only records in frozen mode; `weights.json` and the snapshot remain unchanged; `--reset` is rejected without deletion.
5. Selection artifacts carry policy identity; matching artifacts are reusable, while pre-freeze/mismatched artifacts are rejected. Signal-snapshot v1 remains supported in adaptive mode and cannot pass frozen-mode validation; v2 hash binding detects identity tampering.
6. Invalid frozen policy blocks new target-driven rebalancing while preserving PaperBook state and independent safety controls.
7. Changing the policy mode or snapshot digest changes the Epoch identity; missing freeze identity blocks a new observation Epoch; legacy Epoch and evidence verification remain compatible.
8. Dashboard reports frozen effective weights separately from proposals and fails visibly on invalid snapshot.

Run the focused regressions first, then CI-equivalent core/DRL/h5i/security suites. Do not deploy or alter machine configuration during an active market session. The implementation branch must be pushed and checked on GitHub after review and local verification; deployment and new Epoch freeze remain separate operational gates.

## 9. Explicit non-goals

- No promotion of Alpha, Fusion ranking/weights, DRL plan, or a live broker.
- No changes to factor formulas, ranking, risk limits, fees, slippage, target allocation, or rebalance schedule.
- No restore/overwrite of `data/weights.json`, no rewrite of the Oct 11 Epoch, and no backfill of observation days.
- No StockDB updater run, LevelDB file repair, recurring task creation, or data-source fallback as part of this selector-weight change.
