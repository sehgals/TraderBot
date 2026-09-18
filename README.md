# TraderBot

Alpaca paper-trading bot for managed positions, dynamic entries, watcher supervision, monitoring, and reentry research.

The code is organized as a Python package with top-level compatibility scripts for existing scheduled tasks and operator habits.

## Layout

```text
TraderBot/
  config/
    settings.yaml
    credentials.example.yaml
    credentials.yaml
    watchers.json

  traderbot/
    core_strategy_engine/
      engine.py
      indicators.py
      dynamic_entry.py
      reentry.py
      stops.py
      state.py
      models.py
      strategies/
        base.py
        managed_position.py
        dynamic_reentry.py
        new_candidate_entry.py
        configs/

    broker/
      alpaca_client.py
      orders.py
      positions.py
      market_clock.py

    data/
      market_data.py
      ledger.py
      persistence.py
      paths.py

    monitoring/
      supervisor.py
      watchdog.py
      alerts.py
      logging.py

    backtester/
      reentry_backtest.py
      simulator.py
      reports.py

    cli/
      strategy.py
      supervisor.py
      monitor.py
      backtest.py

  runtime/
    state/
    logs/

  tests/
    unit/
    integration/
    fixtures/
```

## Module Ownership

`core_strategy_engine` owns trading decisions and state transitions. Strategy-specific rules and configuration belong under `core_strategy_engine/strategies`.

`broker` owns Alpaca API integration, order submission, position lookup, and market-clock access.

`data` owns persistence, market-data loading, fill-ledger access, and path resolution.

`monitoring` owns the watcher scheduler, watchdog restart flow, alerts, and operational logs.

`backtester` owns offline simulations and reports. It should reuse strategy and data logic but never submit live orders.

`cli` owns thin command-line entrypoints only.

## Entry Points

Preferred package commands:

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m traderbot.cli.strategy
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m traderbot.cli.supervisor --list
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m traderbot.cli.supervisor --once
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m traderbot.cli.supervisor
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m traderbot.cli.backtest --start 2026-06-08T00:00:00Z --timeframe 5Min
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m traderbot.cli.check_alpaca_connection
```

## Watcher Supervisor

`traderbot.monitoring.supervisor` runs all configured watchers from `config/watchers.json`. It replaces deprecated per-symbol PowerShell watcher tasks and reuses one market-clock check per scheduler pass.

The supervisor checks for watcher-list edits between completed scheduler batches.
Add a symbol to `new_watchers` with its `state` and `log` paths to have it picked
up automatically on the next pass. Unchanged watchers retain their schedules;
added or changed entries become due immediately. Removed or disabled entries
stop being scheduled after the current batch finishes. Invalid edits retain the
last valid list and emit `watcher_list_reload_failed` until corrected. Successful
reloads emit `watcher_list_reloaded`. Watcher defaults also reload; global
execution controls and scheduler settings still require a service restart.
Existing services require one restart to load this code update.

The production bot must always run through `TraderBot_Watcher_Supervisor` as the Windows `SYSTEM` service account (`ServiceAccount` logon, highest privileges). It must not run under an interactive user account. Apply or repair this required configuration from an Administrator PowerShell window with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\Admin\Documents\TraderBot\scripts\configure_watcher_supervisor_task.ps1
```

`traderbot.monitoring.watchdog` is the watchdog used by `TraderBot_Watcher_Supervisor`. It checks for the Python watcher supervisor process and restarts it directly when needed.

When a `new_watchers` candidate becomes an open position, the supervisor promotes it automatically:

- Creates `traderbot/core_strategy_engine/strategies/configs/{symbol}_strategy_config.json`
- Moves state to `runtime/state/{symbol}_strategy_state.json`
- Moves logs to `runtime/logs/{symbol}_watcher.jsonl`
- Removes the symbol from `new_watchers`
- Adds the symbol to `managed_watchers`
- Enables `dynamic_reentry_enabled` and disables `dynamic_entry_enabled`
- Enables the managed `reentry_enabled` lifecycle gate

Managed re-entry can be rolled out without submitting orders by setting
`managed_reentry.observe_only` in `config/watchers.json`. In observe-only mode,
watchers continue refreshing and reporting re-entry plans, but shared eligibility
returns `reentry_observe_only` and execution cannot submit a re-entry order.

Audit or migrate every existing managed strategy config with:

```text
python scripts/migrate_managed_reentry.py --config config/watchers.json
python scripts/migrate_managed_reentry.py --config config/watchers.json --apply
```

The first command is a dry run. The apply command backs up every changed strategy
config under `runtime/backups` before enforcing the managed lifecycle settings.

Task Scheduler should run the watchdog with `pythonw.exe` so no console window flashes:

```text
Program:
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe

Arguments:
-m traderbot.cli.monitor

Start in:
C:\Users\Admin\Documents\TraderBot
```

The watchdog starts the long-running supervisor as:

```text
pythonw.exe -m traderbot.cli.supervisor --config C:\Users\Admin\Documents\TraderBot\config\watchers.json
```

## Strategy Behavior

Managed strategies support:

- Broker-held 8% catastrophic protection for every filled share.
- One-time 50% adverse reduction at a 6% entry-relative loss.
- Trailing floor that only moves upward.
- Optional DCA ladder buys.
- Dynamic ledger-aware reentry after exits.
- Dynamic market-action-only entries for new candidate symbols.
- Shadow-mode Position Health assessments using downside state, 60-minute trend, and remaining reward/risk.
- Entry RVOL uses matched Eastern-time-of-day dollar volume from up to 20 prior sessions, avoiding the opening/closing-volume bias of a rolling 20-bar average.
- Dynamic entries require favorable QQQ and sector-benchmark regimes by default.
- Dynamic entry admission, 0.5%-of-equity sizing, initial protection, and R-based trailing share one structural stop; entries require at least 1.5:1 expected reward/risk.
- Pullback-reclaim and breakout-continuation entries are evaluated and scored independently, then classified by a deterministic single-entry arbiter.
- The first fill freezes the originating entry model and risk contract; Position Health owns all later discretionary adds, reductions, and exits.
- Legacy ladder levels are observation-only and cannot submit post-fill buys.

Dynamic plans write `dynamic_entry_plan` into strategy state with pullback/breakout levels, price-action context, ledger caps, and blockers.

The first fill creates a position episode and transfers decision authority from Entry Setup to Position Health. Daily reports use Position Health for open holdings; cached entry scores are retained only for entry attribution. Score-driven reductions and exits remain disabled while `position_health.shadow_mode` is `true`.

See [Position Health and Trade Lifecycle Architecture](docs/position_health_design.md) for component ownership, state transitions, scoring, execution safety, tests, and rollout criteria.

## Backtesting

The reentry backtester is implemented under `traderbot.backtester`. Run it through the package CLI:

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m traderbot.cli.backtest --start 2026-06-08T00:00:00Z --timeframe 5Min
```

It compares static reentry rules with the dynamic reentry model using Alpaca fills, IEX bars, QQQ market-regime checks, EMA/VWAP/ATR indicators, and ledger-aware price caps.

The risk-control research harness uses a unified portfolio simulation. Signals
are observed at a completed bar close, day-limit orders become fill-eligible on
the next symbol bar, and every symbol shares one cash and equity balance. Its
JSON output includes portfolio return, drawdown, exposure, rejected-order
counts, per-symbol attribution, and trade-level signal and fill timestamps.
Live and research paths share the dynamic plan, initial/catastrophic floor,
hard-reduction, sizing, re-entry ledger, cooldown, and Position Health policy.
The harness requires matching hourly stock and benchmark data whenever live
Position Health actions are enabled, so an active production rule cannot be
silently omitted from a backtest.

## Runtime Files

Runtime files live outside the importable package:

- Strategy state JSON files: `runtime/state/`
- Watcher JSONL logs: `runtime/logs/`
- Supervisor stdout/stderr logs: `runtime/logs/`
- Candidate watcher state/logs: `runtime/state/candidates/` and `runtime/logs/candidates/`

## Deprecated Files

The old per-symbol PowerShell watcher scripts, legacy `.log` watcher files, Tastytrade scanner prototype, and AI scanner config are deprecated. They should stay deleted and should not be referenced by new code.

Deprecated examples:

- `run_mrvl_bot.ps1`
- `run_amkr_bot.ps1`
- `start_strategy_watcher.ps1`
- `start_mrvl_watcher_scheduled.ps1`
- `start_amkr_watcher.ps1`
- `monitor_watchers.ps1`
- `mrvl_watcher.log`
- `mrvl_watcher.out.log`
- `mrvl_watcher.err.log`
- `*_watcher.log`
- `tastytrade_ai_scanner.py`
- `ai_infra_scanner_config.json`

## Remaining Refactor Work

1. Replace facade modules with fully extracted implementations.
2. Introduce typed strategy config/state models.
3. Add focused unit tests before changing strategy behavior.
4. Convert JSON strategy configs to YAML if runtime validation is added.
