# Hourly policy and swing research

Position-health reductions and exits use the same confirmation helper in live
execution and portfolio simulation. A sequence requires fresh, complete assessments
on adjacent completed hourly bars, with the same action and position episode.
Repeated polls and older bars do not advance the count. Missing hourly intervals,
overnight gaps, action changes, and new episodes start a new sequence. Older saved
confirmation state starts over when first evaluated under this policy.

`position_health.entry_gate_mode` accepts `off`, `shadow`, or `enforce` (default
`off`). The checked-in watcher configuration uses `shadow`: it records diagnostics
without blocking entries. Enforcement blocks breakout-continuation and
pullback-reclaim candidates when hourly data is missing/stale or price is below
both EMA21 and EMA50 while EMA21 is falling. It runs before candidate selection
and is checked again before order submission. Other entry models keep their
existing policies.

The portfolio backtester supports `--research-variant`:

| Variant | Behavior |
| --- | --- |
| `confirmation_only` | Shared confirmation policy, hourly entry gate off |
| `hourly_gate` | Enforced hourly entry gate |
| `hourly_swing` | Gate plus wider hourly structure/ATR stops |
| `hourly_swing_slow_trail` | Same stops, trailing begins at 2R |

For example, using the repository's Python interpreter:

```powershell
py -m scripts.run_risk_control_backtest --symbols MSFT --start 2026-01-01T00:00:00Z --end 2026-09-01T00:00:00Z --research-variant hourly_swing_slow_trail --output runtime/reports/hourly-swing.json
```

The command fetches historical data using the configured Alpaca credentials.
Research configuration is copied in memory. Swing stops use the wider of the
original stop, 2.5 hourly ATR, and the six-bar hourly low minus 0.1 ATR; stops beyond
the configured catastrophic limit are rejected. Targets stay fixed. Reward/risk,
weighted score, and quantity are recalculated before selection and sizing.
Ordinary initial-stop calculations do not override the research stop; the
catastrophic floor and hard adverse-reduction policy still apply. These variants
are loaded only by the backtester.

Reports include the selected variant, configuration snapshot, data provenance,
average holding hours, exit-reason counts, and same-market-day reentries after
stop exits. Hourly input uses only bars completed by each five-minute decision
close. Enforced variants require hourly symbol data; active position health also
requires benchmark data. The initial indicator warmup can block entries.

Validation uses synthetic offline data. No historical performance claim or live
activation follows from these tests. The existing simulation approximates market
fills at completed-bar closes and stop gaps at the worse of open or stop price.
