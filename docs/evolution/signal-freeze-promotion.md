# Signal Freeze Shadow Promotion Record

This document is the runtime evidence ledger for promoting the 09:25 signal
freeze from the default `shadow` mode to paper-only `enforce` mode.

It is deliberately not an automated pass flag. Every row must be filled from
an actual trading day after the daemon has run, and every observed difference
must have exactly one classification.

## Promotion gate

Promotion requires all of the following:

- five consecutive actual trading days observed;
- no difference classified as `unexplained`;
- every difference has one of `expected_timing`, `data_refresh`,
  `source_disagreement`, or `unexplained`;
- reviewer and evidence references are recorded for every day;
- `TRADE_BROKER=paper` is confirmed;
- no broker credential, order API, or real-account side effect is enabled;
- a human approves the promotion and records the approval in
  `config/signal_freeze_mode.json`.

If any `unexplained` difference appears, the consecutive-day counter resets to
zero on the following trading day. Do not modify freeze code during this
observation window; investigate and record the difference first.

## Difference classifications

| Classification | Meaning |
|---|---|
| `expected_timing` | A candidate arrived after 09:25 within the defined late window. |
| `data_refresh` | The upstream source refreshed after the snapshot was written. |
| `source_disagreement` | Two declared upstream sources produced different values. |
| `unexplained` | No verified explanation is available; blocks promotion. |

## Daily evidence

| Trading day | Snapshot status/hash | Late candidates | Differences | Classification(s) | Reviewer | Evidence path | Notes |
|---|---|---:|---:|---|---|---|---|
| YYYY-MM-DD |  |  |  |  |  |  |  |
| YYYY-MM-DD |  |  |  |  |  |  |  |
| YYYY-MM-DD |  |  |  |  |  |  |  |
| YYYY-MM-DD |  |  |  |  |  |  |  |
| YYYY-MM-DD |  |  |  |  |  |  |  |

## Paper-only promotion approval

Complete only after the five rows above are real and contain no
`unexplained` classification.

```text
mode: enforce
promoted_at: <ISO-8601 timestamp with Asia/Shanghai offset>
promoted_by: <human reviewer>
evidence: docs/evolution/signal-freeze-promotion.md#daily-evidence
TRADE_BROKER: paper
real-broker-side-effects: disabled
```

The corresponding control record must contain the same approval evidence:

```json
{
  "mode": "enforce",
  "promoted_at": "<ISO-8601 timestamp with Asia/Shanghai offset>",
  "promoted_by": "<human reviewer>",
  "evidence": "docs/evolution/signal-freeze-promotion.md#daily-evidence"
}
```

Until this section is completed, the repository remains in its default
`shadow` mode. Phase F is a separate change and must not be enabled by filling
this template alone.
