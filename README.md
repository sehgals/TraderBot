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

`traderbot.monitoring.watchdog` is the watchdog used by `TraderBot_Watcher_Supervisor`. It checks for the Python watcher supervisor process and restarts it directly when needed.

When a `new_watchers` candidate becomes an open position, the supervisor promotes it automatically:

- Creates `traderbot/core_strategy_engine/strategies/configs/{symbol}_strategy_config.json`
- Moves state to `runtime/state/{symbol}_strategy_state.json`
- Moves logs to `runtime/logs/{symbol}_watcher.jsonl`
- Removes the symbol from `new_watchers`
- Adds the symbol to `managed_watchers`
- Enables `dynamic_reentry_enabled` and disables `dynamic_entry_enabled`

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

- Initial floor stop below entry.
- Trailing floor that only moves upward.
- Optional DCA ladder buys.
- Dynamic ledger-aware reentry after exits.
- Dynamic market-action-only entries for new candidate symbols.

Dynamic plans write `dynamic_entry_plan` into strategy state with pullback/breakout levels, price-action context, ledger caps, and blockers.

## Backtesting

The reentry backtester is implemented under `traderbot.backtester`. Run it through the package CLI:

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m traderbot.cli.backtest --start 2026-06-08T00:00:00Z --timeframe 5Min
```

It compares static reentry rules with the dynamic reentry model using Alpaca fills, IEX bars, QQQ market-regime checks, EMA/VWAP/ATR indicators, and ledger-aware price caps.

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
