# Phase 6: Regime-dependent exposure bands

Phase 6 classifies the market from completed QQQ five-minute bars available at
the allocation timestamp. Favorable requires price above EMA21 above EMA50 with
a nonnegative five-bar EMA21 slope; defensive is the inverse; all other valid
states are neutral. Missing or insufficient data defaults to defensive.

The configured 0-25%, 25-55%, and 50-80% bands are research bands and the
portfolio allocator remains in shadow mode. Maximums are hard allocation
ceilings. Minimums are telemetry only (`below_target_band`) and never cause an
order, improve a score, reduce the minimum score, or bypass a safety check.

Backtests apply bands only with `--apply-regime-exposure-bands`. Run otherwise
identical banded and unbanded tests, then compare them with:

```powershell
py scripts/compare_regime_exposure_backtests.py --unbanded UNBANDED.json --banded BANDED.json --output COMPARISON.json
```

Acceptance requires an adequate banded trade sample, improved Sharpe ratio,
non-worsening maximum drawdown, and positive P&L after estimated slippage.
Higher utilization alone cannot pass. Keep live shadow mode enabled until a
purged out-of-sample comparison passes these gates.
