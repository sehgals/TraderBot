# Phase 4: Weighted entry scoring

Entry qualification keeps the Phase 3 hard safety gates and replaces the
equal-count soft score with a transparent 0-100 weighted factor score.

Default factors are price action (25%), stock trend (20%), market/sector regime
(15%), volume/liquidity quality (15%), reward/risk geometry (15%), and relative
strength/execution quality (10%). Candidate state records each factor score,
its normalized weight, and its contribution to the total.

Weights may be overridden globally with `entry_score_weights` or per model with
`entry_models.<model>.score_weights`. Missing factors are excluded and remaining
weights are normalized to 100%; hard failures always block regardless of score.

Walk-forward calibration is opt-in:

```powershell
py scripts/run_walk_forward_backtest.py --symbols PANW --start 2025-09-01T00:00:00Z --end 2026-09-01T00:00:00Z --output runtime/reports/backtests/panw_phase4.json --calibrate-entry-weights
```

For every window, the candidate profiles are evaluated only on the training
interval. The best training return minus drawdown profile is frozen and then
evaluated on the purged out-of-sample test interval. The report records all
training results, the selection cutoff, and the selected weights.
