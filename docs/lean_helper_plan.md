# LEAN helper plan

## Purpose

Use LEAN as an independent research and backtest companion. Production orders,
watchers, and the Windows `TraderBot_Watcher_Supervisor` task remain under
TraderBot. The helper rejects live deployment in `initialize`.

## First experiment

`research/lean/traderbot_helper/main.py` runs one liquid US equity (MU) on
regular-session minute data consolidated into completed five-minute and hourly
bars. The entry is a simplified breakout: close above the preceding 20
five-minute highs, above a 21-bar EMA, with volume at least 1.5 times the
preceding 20-bar mean. It sizes at no more than 1% account risk or 10% notional,
using a 1.5 hourly ATR initial stop. It sells half at +2R and trails the runner
at 2.5 hourly ATR below the highest price observed after the partial fill.
It tags orders and writes up to 50 fill events to the LEAN log with `TB_EVENT`
to stay within the Free tier log allowance.

This isolates TraderBot's breakout and winner-exit concepts. It does not
reproduce TraderBot's full setup score, regime filters, quote checks, position
health decisions, portfolio allocator, or exact broker order state. Treat its
returns as research results, not a TraderBot performance estimate.

## Run and compare

1. Start with a Free QuantConnect organization and create a Python project in
   the cloud IDE. Replace its `main.py` with
   `research/lean/traderbot_helper/main.py`. This file is self-contained; an
   older `rules.py` in the cloud project can be deleted. If the cloud copy has
   additional guards or experiments, carry those changes forward instead of
   overwriting them. No brokerage credentials are needed for this backtest.
2. Run the project as a cloud backtest using QuantConnect's free US equity
   minute data. The strategy uses one symbol and minute-to-hour resolutions,
   which fit the Free tier. Keep the LEAN CLI and local Docker path as an
   optional later step if local development becomes worthwhile.
3. Start with 2024 as development data and reserve 2025 as a holdout period.
   Compare entry timestamps, order quantities, initial risk, partial exits,
   stop changes, final exits, drawdown, and turnover against TraderBot's own
   backtester on matched bars. Align timezone, regular hours, raw price
   normalization, fees, slippage, and fill assumptions first.
4. Investigate mismatched decisions one event at a time. Only after that,
   expand to more symbols and walk-forward periods. Keep LEAN results in a
   separate research report; do not wire LEAN outputs into production orders.

## Acceptance gates

- No future bar or unfinished hourly ATR is used to decide an entry or exit.
- Every filled entry has a protective stop; while a partial sale is pending,
  the remaining runner stays covered by a stop.
- On matched data, explain every material difference between LEAN and
  TraderBot signals before comparing aggregate returns.
- The out-of-sample period remains separate from parameter tuning.

## Setup limits

The `lean` CLI is not currently installed in this workspace. It requires the
Quant Researcher tier; local backtests also need historical data, and local
dataset downloads may carry separate charges or license terms. The Free tier
supports the cloud experiment above. Do not export QuantConnect market data
to TraderBot outside its license terms.
