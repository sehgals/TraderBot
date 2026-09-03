# Phase 7: Diversified signal families

Five independently configurable families have been added:

- `relative_strength`: continuation versus the point-in-time sector benchmark.
- `low_volatility_trend`: positive trend with bounded ATR/price volatility.
- `post_earnings_drift`: positive drift after a known positive earnings surprise.
- `trend_mean_reversion`: pullback and reversal inside a rising long-term trend.
- `defensive_etf`: broad-market/defensive ETF fallback when no individual-stock
  allocation is selected.

All are disabled by default. Each has its own `risk_budget_percent`, model checks,
factor contributions, and attribution identifier. Post-earnings drift hard-blocks
when point-in-time event coverage or surprise data is absent. Defensive ETF
candidates are ranked after individual securities and rejected when another
candidate has already been selected.

Run every family separately before combination with `--only-entry-model`, for
example:

```powershell
py scripts/run_walk_forward_backtest.py --symbols PANW --start START --end END --output rs.json --only-entry-model relative_strength
```

Create three purged out-of-sample reports: existing models, the isolated new
model, and their proposed combination. Evaluate admission with:

```powershell
py scripts/evaluate_model_admission.py --existing existing.json --isolated isolated.json --combined combined.json --output admission.json
```

Admission requires an adequate isolated trade sample, positive isolated return
after estimated slippage, improved combined Sharpe ratio, and a measured return
correlation below the configured ceiling. Increased exposure alone cannot pass.
Models remain disabled until their individual admission result passes.
