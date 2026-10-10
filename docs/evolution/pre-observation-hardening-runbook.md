# Hardening and real-day observation handoff

## Freeze prerequisites

1. Review the hardening branch, its complete test results, and GitHub checks at the same commit SHA.
2. Choose the deployed code SHA explicitly. Confirm the runtime interpreter and process identity point to that checkout.
3. Record configuration hash, experiment hash, stable data-lineage identity, and explicit production state. New epochs require the implemented default-shadow DRL contract.
4. Build and verify one Observation Epoch v2. Freeze that exact epoch, configuration, and deployment identity before counting days.

Do not deploy a development checkout automatically. A GitHub branch or passing CI alone does not prove the running daemon uses that code. Main and PR #17 remain separate integration decisions.

Keep `RANK_BY_FUSION=0`, `DRL_PLAN_MODE=shadow`, `FUSION_WEIGHT_MODE=shadow`, `TRADE_BROKER=paper`, and `alpha_evidence_status=not_promotable`. New epoch construction requires `drl_plan_mode_contract=implemented_default_shadow`. Evidence producers reject missing/non-shadow state. No automatic promotion is enabled.

## Contract migration before deployment

- Supply explicit stable `data_lineage_identity` to EvidenceBundleRequest and OOSDatasetRequest. Never derive it from a daily market/snapshot hash. Historical v1/pre-epoch artifacts remain verification-only and are not rewritten.
- Historical PPO features/prior are neutral to today's LLM brief. `final_weights`/`training_weights` remain learned/versioned weights; `inference_weights` is the separately labeled current overlay. Missing brief evidence dates/provenance remain null/unknown.
- Reward schema v2 records normalized three-component weights, prior active weights/provenance, actual-generation `generated_on`, separate `processing_day`, `effective_from`, and calendar provenance. Historical daily backfills do not backdate generation. Unknown next session remains pending; regenerate only with authoritative session evidence. Resolved schedules must be supported by the cached authoritative next session at read time. Legacy/invalid config produces a traced normalized degraded default; that active fallback provenance survives a pending rewrite. The config is not a full historical ledger; training before its generation day degrades instead of using future weights. The normal pipeline still does not invent aligned PPO reward components.
- Held prices from H5i/DuckDB/unknown reference sources are explicitly degraded. Aggregate realtime `data_ts` is null when requested symbols include references/missing prices; state `updated_at` is not a quote timestamp. Sina spot retains its real source name. Unknown source is never labeled H5i from configured BAR_STORE alone.
- An absent live target list can only use a ready canonical same-day snapshot receipt validated by schema/content hash and matching advertised hash. A freeze snapshot must identify the expected Shanghai trade day and have matching `generated_at`/`generated_at_utc` values inside 09:25:00–09:25:59 Shanghai time. Missing/mismatched receipts block portfolio construction, even if a DRL plan exists.
- When a 09:25 snapshot is built from a DRL plan, its source tier and artifact digest must refer to the actual `data/drl/<source-day>/target_plan.json` consumed, not a same-day `selection.json` decoy.
- New Epoch v2 bundle/dataset verification cross-checks duplicated manifest identity fields and aliases (daily data hash, config/experiment hashes, calendar identity, trade days, snapshot/source-artifact hashes) against their canonical identity objects. Per-day artifact hashes remain outside the stable epoch identity.
- Dividend holding days are exact only for a complete recorded single-date acquisition basis. Legacy/cross-date/invalid bases have null holding days and explicitly approximate conservative 20% withholding. The existing 20%/10%/0% schedule, aggregate entitlement, and T+1 mechanics are unchanged; no tax true-up is implemented.

## Qualifying a trading day

Use an explicit authoritative trading-calendar session, with its source/version recorded. At 09:25 Shanghai time require a ready signal snapshot with verified content hash, expected day, dual timestamps proving the Shanghai 09:25 freeze minute, and canonical target contract. Record price provenance and health; held positions valued from reference prices require a degraded classification and cannot be silently described as fully realtime.

At close, preserve explicit snapshot, daily market, positions, orders, fills, and review artifacts. Their hashes belong to the day's immutable Evidence Bundle, while the stable lineage and epoch remain constant. Use canonical fills for simulated/realized cost calculations. Missing fills remain missing/not_applicable/blocked, never fabricated realized costs.

Verify each finalized bundle and append an explicit OOS day input. Review production differences against the frozen baseline, including the explanation for any allowed correctness difference. Count only days satisfying the taskbook's complete qualification criteria. A missing/degraded/blocked day remains visibly classified; it cannot be replaced by a historical replay to manufacture a consecutive window.

## Window completion

After five consecutive qualified real sessions, verify a single epoch across all bundles and OOS rows, review cash-inclusive turnover and cost evidence, and record a human promotion decision separately. Runtime remains rank legacy, DRL/Fusion shadow, paper broker, and Alpha not_promotable until explicit approval.

## Deferred work

- PR #13 Data Router and unrelated PR #15 dashboard/runtime changes require their own review; vendor executables/binaries require provenance and licensing evidence.
- A full per-acquisition lot ledger and historical dividend-entitlement reconstruction are outside this pass. Aggregate/legacy holdings with uncertain acquisition basis must expose approximate/unknown dividend evidence. The existing latest-buy-date entitlement filter can still miss an old eligible lot after an after-ex-date add-on; this pass does not claim that case repaired.
- Changing PPO reward architecture, enabling production DRL/Fusion, and broker integration remain outside this pass.
- Five real observation days cannot be completed by offline tests on 2026-10-10. No existing historical artifact is relabeled as a new-epoch qualified observation day.
