# Phase C: DRL V2 semantic contract

Status: C1/C2 implementation slice complete; historical fixture validation passed; model-loadability and reward-naming cleanup remain.

## Scope

This phase repairs DRL research semantics while keeping the production boundary in
shadow mode. It does not change selector ranking, target weights, PaperBook,
broker behavior, or automatic promotion.

## Time axis

All training paths use a chronological `train / purge / validation` split. The
purge interval immediately before the validation tail is excluded from both
sets. An environment observes market state only through `t-1` and receives the
reward associated with timestamp `t`.

Gym randomness is owned by the environment seed. `FactorWeightEnv` and
`FactorValueEnv` expose explicit episode ends so validation rows are not used by
the learning rollout.

## Reward contract

An aggregate vn.py or attribution score is metadata only. It cannot be written
back into a completed rollout after PPO learning. External reward components may
enter PPO only as finite, per-step arrays with an explicit weight mapping and
are combined inside `env.step`.

## PIT factor state

The production six-factor path no longer consumes the old market-average-return
pseudo-IC matrix. It requires historical `selection.json` records containing a
full `pool_snapshot` with the six factor values. A five-trading-day forward label
is required for Rank IC and long-short factor return; immature labels are
excluded, never zero-filled. The resulting IC state is shifted by the label
maturity horizon before being exposed to the decision-time environment.

The dynamic fusion-factor loader uses the same contract for
`pb_inv`, `ep`, `ocf_ps`, `roe_yy_chg`, and `gp4`: average-tie Rank IC,
direction-aware top-minus-bottom return, explicit coverage, and a matured PIT
state.

## Incumbent/challenger

The current model pointer is captured before the degradation resolver can advance
it to the challenger. Post-training validation therefore compares the new
candidate with the actual prior incumbent rather than reading the newly written
pointer back and comparing the candidate with itself.

## Risk semantics

Risk-First penalties cannot infer portfolio exposure from observation columns.
Only an explicit environment action-to-weight mapping may supply the auxiliary
penalty. The environment-level penalty remains the authoritative path otherwise.

## Verification boundary

The implementation has focused contract, environment, risk integration,
degradation, h5i migration, compile checks, and a complete historical
`pool_snapshot` fixture. The fixture verified 20 aligned matured rows, excluded
the five immature tail rows, and recovered positive known Rank IC values. No
promotion or production reward integration is authorized by this result.
