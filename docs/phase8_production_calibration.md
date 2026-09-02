# Phase 8: Production calibration lifecycle

## Objective

Convert Phase 4's research-only weight calibration into a controlled offline
production lifecycle. The live trading process must never train or automatically
consume an unvalidated calibration result.

## Implementation scope

1. Add a weekly or monthly rolling-date calibration runner that operates after
   the market closes and uses only completed trading days.
2. Emit immutable, versioned calibration artifacts containing symbol, training
   cutoff, selected profile and weights, dataset/config hashes, out-of-sample
   metrics, and creation time.
3. Add acceptance gates for minimum out-of-sample trades, positive performance
   after costs, drawdown, profitable-window ratio, improvement over the active
   baseline, filter-data coverage, and maximum weight movement.
4. Add shadow evaluation in which candidate weights produce decisions and
   forward-return telemetry but cannot submit or modify broker orders.
5. Add an explicit promotion command. Training output must never directly edit
   `config/watchers.json` or the active production artifact.
6. Store active, candidate, and previous artifact versions. Promotion must use
   an atomic active-version change and retain the previous version for rollback.
7. Include the active calibration version and weights in watcher state, entry
   decisions, trade records, reports, and backtest provenance.
8. Add production monitors for signal frequency, rejection rate, fills,
   slippage, expectancy, profit factor, drawdown, and live-versus-training drift.
9. Add configurable rollback gates for drawdown, missing signals, abnormal
   rejection rates, stale/missing data, or artifact validation failure.
10. Schedule calibration separately from the production watcher. Any strategy
    deployment or restart must continue through `TraderBot_Watcher_Supervisor`
    as SYSTEM/ServiceAccount in session 0.

## Initial rollout policy

- Begin with monthly calibration and manual promotion.
- Require multiple profitable purged out-of-sample windows and an adequate trade
  sample; zero-trade or missing historical filter coverage cannot be promoted.
- Run candidate weights in shadow mode for multiple cycles before promotion.
- Limit each factor's change to 10-15 percentage points per promotion.
- Do not enable automatic promotion until repeated production evidence supports
  it; automatic rollback may be introduced earlier because it is risk-reducing.

## Acceptance criteria

- Training and live trading run in separate processes and scheduled tasks.
- No artifact can become active without schema, provenance, and acceptance-gate
  validation.
- A promoted version is attributable in every affected decision and trade.
- Interrupted promotion is atomic and leaves either the old or new valid version
  active.
- Rollback restores the previous artifact without directly launching a watcher
  under a signed-in account.
- Tests cover data leakage, insufficient samples, failed gates, atomic promotion,
  shadow non-execution, version attribution, and rollback.
