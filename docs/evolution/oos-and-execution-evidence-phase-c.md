# Phase C: Continuous OOS and execution evidence

## Scope

This slice is an offline evidence path. It does not change selector ranking,
target weights, PaperBook state, broker routing, realtime behavior, DRL plan
consumption, or promotion.

The `oos_dataset` builder accepts an explicit ordered `trade_days` list and an
explicit `OOSDayInput` for every day. An available day must provide the exact
bundle path, bundle id, and manifest hash. Other days remain rows with an
explicit status such as `pending_maturity`, `missing`, or `blocked`; no day is
discovered from a directory scan and no date is inferred from the clock.

The final dataset is staged in a temporary directory, hashes its raw day index
and derived metrics, and writes `manifest.json` last as the commit marker. A
finalized dataset is never overwritten; a changed input produces a different
dataset identity.

The explicit historical run for `2026-09-22` through `2026-09-24` finalized
dataset `1459dfbfe6c31288c3552c8ed41b5e84b8697d946074aff540c354e4de8b42a3`
under `data/evidence_phase_c/oos/`. It contains three available daily rows and
records the current Phase B state as `DRL_PLAN_MODE=shadow`,
`FUSION_WEIGHT_MODE=shadow`, `RANK_BY_FUSION=0`, and
`drl_plan_mode_contract=implemented_default_shadow`.

## Daily holdings and cost evidence

`daily_execution_evidence` adapts explicitly supplied PaperBook-like position
artifacts and target-plan artifacts into the Phase A contracts:

- target-weight turnover compares previous actual holdings with the new target,
  including `CASH`;
- planned turnover uses explicit order rows and a fixed reference-equity
  denominator;
- executed turnover uses fills only and remains null with `not_applicable` or
  `blocked` status when no fill ledger exists;
- transaction cost replay accepts canonical fill rows only. Legacy
  `trades_history` rows missing order/fill identity or decision/order fields are
  preserved as raw evidence and reported `blocked`, never guessed into
  realized costs.

## Explicit 2026-10-09 opening check

The read-only check used the explicit `2026-10-09` input and the following
artifacts:

- `data/drl/20261008/target_plan.json`, whose `consume_day` is `2026-10-09`;
- `data/live_state.json`, still dated `2026-10-08`;
- `data/daily/20261008/paper_book.json`, dated `2026-10-08`;
- the explicit `data/daily/20261008/daily_summary.json` and empty trades file.

The result is `blocked`, not a successful-open assertion: the live artifact is
one day behind, reports `snapshot_status=invalid` with
`snapshot_write_failed:ValueError`, and has `feed_error=spot empty`. The prior
close was empty (`equity=98282.72`, `cash=98282.72`, `market_value=0`, no
positions and no trades today). Effective `DRL_PLAN_MODE` and
`FUSION_WEIGHT_MODE` are both `shadow`; `TRADE_BROKER` remains `paper`.

This is an observation result only. No plan, PaperBook, pointer, selector, or
broker artifact was written.

## 2026-10-09 pre-open recheck

The earlier blocked opening report remains an immutable historical observation
of the `2026-10-08` artifacts. A separate read-only pre-open recheck at
01:58 Asia/Shanghai verified the current runtime without writing a plan,
position, order, fill, selector, PaperBook, or broker artifact:

- `premarket_healthcheck.py`: `OK=14 WARN=4 FAIL=0`;
- StockDB endpoint: `127.0.0.1:7899`, `engine_day=20261008`,
  `expected_day=20261008`, `lag_trading_days=0`;
- `AStockDaemon` and `AStockStockdb`: `Running/Automatic`;
- Dashboard `/api/health`: HTTP 200;
- `TRADE_BROKER=paper`, with `DRL_PLAN_MODE` and `FUSION_WEIGHT_MODE`
  retaining their default `shadow` behavior.

This is pre-open readiness only. The 08:30 engine start, 09:25 snapshot,
and 09:30 session remain unobserved until their scheduled same-day artifacts
exist. The four WARNs remain typed observations (orderbook representation,
IC-curve freshness, decision-component availability, and the prior position
gap); they are not converted into a fabricated all-clear.

## Remaining evidence limitation

The historical `2026-09-24` replay produced a cash-inclusive target-weight
turnover of `0.8390459541` using fixed reference equity `100000.0`, but its
planned/executed turnover and cost replay are `not_applicable` because the
explicit daily orders/fills artifact is empty. A complete simulated or real
cost result requires a canonical fill ledger with the fields defined by
`evidence_cost.py`.

## DRL V2 completion notes

`drl_degrade.version_usable` now rejects corrupt model archives using a
lightweight ZIP integrity check. `drl_model_validation.validate_model_loadability`
performs an optional injected full loader probe without mutating the model
artifact. The explicit `data/drl/20261008/model.zip` passed the canonical
`.venv310` Stable-Baselines PPO load probe with no side effects.

Execution and attribution Sortino values in DRL metadata are named diagnostic
metrics. They enter PPO gradients only when an explicitly aligned
`reward_components` schedule is supplied.
