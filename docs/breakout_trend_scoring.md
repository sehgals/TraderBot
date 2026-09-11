# Breakout trend scoring

The breakout model divides its stock-trend weight equally among five conditions:
price above VWAP, price above EMA9, EMA9 above EMA21, EMA21 at or above EMA50,
and ATR-normalized EMA21 slope. With the default weight of 20, each earns 4 points.

Slope is `(EMA21 now - EMA21 five completed bars ago) / current ATR14`.
The default threshold is 0.20 ATR, inclusive. Configure it with
`entry_models.breakout.minimum_ema21_slope_atr`. This is an initial engineering
default, not a performance-validated threshold. The breakout model no longer uses
`minimum_ema21_slope`; pullback scoring still uses its existing percentage rule.

Normalization uses the indicator's actual ATR, not the price-risk ATR floor.
Missing, nonfinite, zero, or negative ATR earns no slope points. The other four
conditions retain their own points. The 80-point entry threshold and hard safety
checks remain in force.

New assessments record component-atr-v2, slope in ATR units, EMA change, ATR,
individual results and points, and assessment time. The dashboard shows those
units; existing percentage-based saved assessments retain their original units
until replaced by a fresh evaluation.

## Breakout distance and overextension

Distance is (completed close minus previous 20-bar high) divided by the preceding
completed bar's actual ATR14. Price-action credit rises linearly from zero at
resistance to full credit at `entry_models.breakout.full_breakout_score_atr`
(default 0.50). Overextension credit is full through that distance and falls
linearly to zero at `entry_models.breakout.maximum_chase_atr` (default 1.50).
A close at or below resistance, invalid baseline ATR, or distance strictly greater
than the chase limit blocks the breakout regardless of score. Equality at the
chase limit is allowed, with zero overextension credit. These limits apply to the
completed assessment bar, not a fresh execution quote. Pullback rules are unchanged.
New dashboard assessments expose both scores, resistance, baseline ATR and gates.
