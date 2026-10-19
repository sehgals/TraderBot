# ORCL swing pullback: weekly entry, monthly size

Copy `main.py` into a QuantConnect Python project's `main.py` and run a new
backtest. It is configured for ORCL from 2025-01-01 through 2026-10-12, with
400 calendar days of warm-up before the start date. It is research code and
does not change the production TraderBot.

The completed weekly close must be above its 10-week EMA, and that EMA must
be above its value four completed weeks earlier. This is the higher-timeframe
entry gate. The completed monthly close above a rising 3-month SMA is a sizing
signal: matching setups use the full planned quantity; other setups use half
that quantity, rounded down. Four completed months are required before any
entry. Full planned size is capped at 0.25% of equity in stop risk and 10% of
equity in notional; half size halves those planned caps. Actual losses can
exceed planned stop risk because stops can slip or gap.

The daily dip and 15-minute EMA reclaim entry setup is unchanged. A position
exits when its protective stop fills or after two consecutive completed daily
closes below EMA20. The weekly and monthly entry signals do not cause an exit.
The order tags distinguish full-size and half-size entries, and the final log
line `SWING_RESEARCH_COUNTS` reports their order counts and how often each
trend condition passed. The research algorithm has not been run in QuantConnect
from this workspace.
