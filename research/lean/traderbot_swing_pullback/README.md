# QuantConnect swing pullback research

Copy `main.py` into the `main.py` of a new **Python** project in the
QuantConnect cloud IDE. Build, then run a backtest. No TraderBot credentials
or production service changes are needed.

The default **2026 year-to-date backtest** uses MU and QuantConnect's
regular-session, adjusted US equity minute data from 2026-01-01 through
2026-10-12, the latest completed trading session when this file was updated.
A 400-day
pre-start warm-up builds the 10-month, weekly, daily, and 15-minute history.
Warm-up data is excluded from the backtest's reported performance; orders are
allowed only in 2026. Edit the dates and symbol near the top of `initialize`
to run another experiment.

## Exact research rules

- Trend: latest *completed* weekly close above a rising 10-week EMA, and latest
  *completed* monthly close above a rising 10-month SMA.
- Daily setup: completed daily EMA20 rising over five bars; current 15-minute
  close is 0.5–1.5 daily ATR below the highest high of the previous five
  completed daily bars, no more than 3% below that high, and no lower than
  EMA20 minus 0.25 daily ATR.
- Entry: the prior 15-minute bar touched its EMA9 zone; the latest completed
  15-minute close is above EMA9 and the prior close. No entries after 15:30 ET.
  A market order is submitted after the signal bar closes.
- Size/stop: at most 0.25% of account equity in planned stop risk and 10% in
  notional. The protective stop is below the previous five completed daily
  lows by 0.1 daily ATR. Setups needing more than an 8% stop are skipped.
- Exit: broker-style stop order, or liquidation after two completed daily
  closes below EMA20 or loss of the completed weekly/monthly uptrend. A
  stopped position cannot reenter the same trading day.

This is a standalone hypothesis. It does not include TraderBot's full entry
score, Position Health, 2R partial exit, trailing runner, market/sector
filters, earnings checks, or portfolio allocator. QuantConnect's adjusted
bars can differ from TraderBot's raw Alpaca IEX bars. Compare variants on
the same QuantConnect data before comparing either one to live fills.

The research algorithm has not been executed in QuantConnect from this
workspace. Review the first backtest's order list and logs before relying on
any performance numbers.
