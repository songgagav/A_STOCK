# Phase B：策略契约一致性修复

## Scope

Phase B only repairs policy and data-contract boundaries. It does not add
Phase C DRL training semantics, connect a real production data stream, promote
any strategy, or change the Alpha baseline.

The preserved baseline remains:

```text
RANK_BY_FUSION=0
Alpha evidence=not_promotable
TRADE_BROKER=paper
```

## Runtime modes

Both modes are read at runtime and default to `shadow`:

```text
DRL_PLAN_MODE=off|shadow|enforce
FUSION_WEIGHT_MODE=off|shadow|enforce
```

`DRL_PLAN_MODE=shadow` may run DRL and write an explicitly marked plan, but
Realtime and Backtest cannot consume it. `enforce` additionally requires an
explicit `promotion.approved=true` field; dependency availability is never a
promotion signal. `off` skips DRL training and plan production.

Fusion-derived target weights are authoritative only in
`FUSION_WEIGHT_MODE=enforce`. `shadow` and `off` retain the baseline target
weight contract. Enforce-mode Fusion missing coverage or exceptions produce a
blocked result instead of silently falling back to legacy.

## Weight contract

`weight_optimizer.load_authoritative_weights()` is the only dynamic-weight
loader. It reads the nested `weights.json["weights"]` object and rejects
malformed, non-finite, negative, or zero-sum payloads. Selector and DRL base
weights use that same loader/fallback path.

ICIR strength is signed against an explicit expected factor direction. The
optimizer no longer uses `abs(ICIR)` to turn a wrong-direction factor into a
positive weight. Target weights are finite, strictly positive, and normalized
to sum to one before entering an authoritative signal snapshot.

## Ranking and TargetContract

Selector, Fusion blending, and Fusion-derived target weighting use average-tie
percentile ranks. Missing Fusion scores follow one policy: neutral outside
enforce, blocked in enforce.

Target rows are normalized through `strategy_contract.normalize_target_contract`
without dropping `target_weight`, `source_signal`, `source`, or provenance
fields. Signal snapshots validate the same contract, and Backtest preserves it
through `_norm_targets()`.

## Verification

The Phase B focused contract tests cover mode defaults, shadow DRL blocking,
nested weights, signed ICIR, normalization, average ranks, Fusion blocking,
snapshot rejection, DRL base-weight loading, realtime shadow behavior, and
TargetContract preservation.

Full regression results for this implementation:

| Runtime | Result |
|---|---|
| `.venv310` | 2754 passed, 52 skipped, 17 warnings |
| `.venv314` | 2760 passed, 46 skipped, 17 warnings |

No Phase C DRL work is included. The next operational step is validation on
real historical trading-day Evidence Bundles before any promotion decision.
