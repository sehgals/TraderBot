# Entry safety and reentry decisions

Market data uses ALPACA_DATA_FEED (default iex). Quote metadata and liquidity
measurements identify the actual feed; IEX is labeled single-exchange coverage.
Changing feeds requires the corresponding broker data entitlement. There is no
silent fallback or multiplication of IEX volume to approximate consolidated data.

Quotes are checked for finite positive prices, crossed markets, timestamp validity,
and age. Excessive/invalid spreads receive one additional fetch. Both attempts are
recorded. Optional entry_filters.spread.rejected_quote_conditions can reject
specified provider condition codes. Execution always requires a valid quote.

Liquidity keeps the existing risk-profile intraday thresholds. The preferred key
is entry_filters.liquidity.minimum_intraday_dollar_volume; the old
minimum_average_dollar_volume is accepted for compatibility. This metric is a
matched-time historical per-bar mean, not daily volume. Completed daily close-times-
volume estimates are separately recorded with sample size. A daily filter is enabled
only by explicitly setting minimum_daily_dollar_volume, with minimum_daily_sample_size
(default 5). No unvalidated daily threshold is silently assigned. Daily and calendar
lookups are cached for the current Eastern date; failed requests are retried later.

Reentry retains exit history, cooldowns, same-Eastern-date loss restrictions and
entry limits. The exit-price cap expires when the broker calendar confirms a later
regular session opening and the new completed setup is at or after that opening.
Missing calendar/exit timestamps retain the cap. The old above-exit price floor is
removed. Entry counters use Eastern dates instead of UTC dates.

Allocator refreshes stale score-qualified candidates to a common completed-bar
cutoff without submitting orders, recording candidates that remain stale or fail
refresh. Execution authorization remains tied to the selected bar.

Immediately before submit/replacement, a fresh quote and rounded limit/stop/target
geometry are checked. Reward/risk must remain >=1, or the configured higher floor.
The rounded limit must satisfy any active exit cap, and breakout asks must remain
within the configured ATR chase limit. Limit prices are not raised to match asks.
Ambiguous submit responses are reconciled using the deterministic client-order ID.

Dashboard diagnostics distinguish score qualification, hard blockers, missing data,
quote age at evaluation, measured limits, feed coverage and reentry-policy expiry.
