# Phase 2 Baseline Analysis

The baseline workflow measures the current strategy without changing production
entry, exit, sizing, or risk behavior. It uses completed bars, creates orders
after the signal bar closes, and permits fills only on a later symbol bar.

## Single-period baseline

```powershell
& 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  scripts\run_risk_control_backtest.py `
  --symbols PANW AZN `
  --output runtime\reports\backtests\phase2_baseline.json `
  --estimated-slippage-bps 5
```

The report contains:

- CAGR, total return, maximum drawdown, Sharpe, and Sortino;
- profit factor, expectancy, win rate, average win, and average loss;
- gross turnover and a configurable slippage estimate;
- average cash and gross exposure plus time above 25%, 50%, and 75% exposure;
- candidate evaluation, qualification, selection, and rejection rates by model;
- performance attribution by market regime, sector benchmark, and model;
- forward returns of rejected candidates after 1, 3, 6, and 12 completed bars;
- entry-filter rejection counts; and
- Git revision, dirty-worktree status, configuration hash, and dataset hashes,
  bar counts, timeframes, and date ranges.

## Walk-forward baseline

```powershell
& 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  scripts\run_walk_forward_backtest.py `
  --symbols PANW AZN `
  --start 2025-01-01T00:00:00Z `
  --end 2026-01-01T00:00:00Z `
  --train-days 90 `
  --test-days 30 `
  --purge-days 1 `
  --estimated-slippage-bps 5 `
  --output runtime\reports\backtests\phase2_walk_forward.json
```

Training intervals supply indicator history only. Entries are restricted to
the purged out-of-sample test interval. Each window contains its own baseline
statistics and rejected-signal diagnostics; the report also contains stitched
fixed-notional aggregate metrics.

## Interpretation constraints

- Rejected-candidate forward returns are diagnostics only. Future closes are
  read after the simulated decision and never affect order generation.
- The slippage estimate is a sensitivity adjustment, not a fill model.
- Historical spread, earnings, and corporate-action filters are tested only
  when point-in-time datasets are supplied to the walk-forward command.
- Raw Alpaca/IEX bars are requested with `adjustment=raw`. The report hashes
  the exact returned OHLCV series so two runs can detect data changes.
- Sharpe and Sortino use daily portfolio returns and 252-day annualization.
- A small number of trades or daily observations is not statistically
  sufficient, even when a ratio is present.
