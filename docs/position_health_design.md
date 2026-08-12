# Position Health and Trade Lifecycle Architecture

Status: Implemented through shadow rollout (Phases 1-3); score-driven trading remains gated
Audience: Strategy, execution, risk, monitoring, and reporting maintainers
Scope: Entry setup, open-position health, additions, reductions, exits, execution ownership, and reporting

## 1. Executive decision

Entry Setup and Position Health will be implemented as separate decision engines with separate lifecycles. They will initially run as logical components inside the existing supervisor process. They will not be deployed as independent broker-writing processes until the bot has a transactional state store and a single idempotent execution gateway.

The Position Health engine will become authoritative when the first share of an entry order fills. The Entry Setup assessment will then be retained as an immutable audit record. It can participate in a later add decision, but it will no longer determine whether the existing shares should be held.

All broker mutations will be serialized through one execution gateway. Entry Setup, Position Health, and portfolio-level risk controls will produce action intents rather than submitting or cancelling orders directly.

Decision priority is:

```text
Reconcile broker state
  > restore protection
  > exit
  > reduce
  > hold
  > add
  > enter
```

## 2. Context and problem statement

The current strategy loop combines flat-entry evaluation, pending-entry management, position management, stops, ladder additions, reductions, exits, and reentry. Candidate and managed strategy classes both delegate to the same `run_once` function.

Daily reporting also attaches a cached Entry Setup score to open positions. The resulting label answers whether an earlier chart qualified for entry, not whether the current position remains healthy. A losing position can therefore appear `Strong` when its entry-relative loss, current trend, or remaining payoff is unacceptable.

The current supervisor runs symbol watchers concurrently and also contains a portfolio margin reducer that can cancel sell orders and submit liquidation orders. Adding another independent process with direct broker access would create competing order owners, duplicate actions, stop-cancellation races, and potential oversells.

This design separates the decision domains while preserving one execution authority.

## 3. Goals

1. Give Entry Setup and Position Health unambiguous authority boundaries.
2. Switch open shares to health management on the first fill, including partial fills.
3. Calculate health from fresh holding-period data, actual broker position data, the active stop, and an immutable entry target.
4. Prevent adds when a position is deteriorating, reduced, unprotected, or stale.
5. Preserve the hard 6% reduction and 8% catastrophic-stop controls.
6. Make order operations idempotent and serialized per symbol.
7. Allow deterministic replay and backtesting of every assessment and action.
8. Ensure failure of scanning or reporting cannot remove protection from open positions.
9. Provide actionable and auditable daily reports.

## 4. Non-goals

1. This design does not optimize scoring weights from the existing backtest sample.
2. It does not replace broker-held catastrophic stops with software-only exits.
3. It does not permit multiple services to submit orders for the same symbol.
4. It does not enable health-qualified additions during the initial shadow rollout.
5. It does not treat Position Health as a forecast of absolute future return.

## 5. Architecture

```text
Market/Broker Data
       |
       v
+----------------------+       +------------------------+
| Entry Setup Engine   |       | Position Health Engine |
| flat symbols only    |       | open positions only    |
+----------+-----------+       +-----------+------------+
           | EntryAssessment               | HealthAssessment
           +---------------+---------------+
                           v
                +-----------------------+
                | Lifecycle Coordinator |
                | state + arbitration   |
                +-----------+-----------+
                            | ActionIntent
                            v
                +-----------------------+
                | Execution Gateway     |
                | sole broker writer    |
                +-----------+-----------+
                            v
                          Broker

Portfolio Risk Coordinator --> ActionIntent --> Lifecycle Coordinator
Reporting <---------------- persisted assessments and execution events
```

### 5.1 Initial deployment topology

All components will run inside `traderbot.monitoring.supervisor` using its existing scheduler and shared Alpaca client. Components are logically independent but share one process and one execution gateway.

This avoids multi-process state corruption while the repository still uses per-symbol JSON state.

### 5.2 Future deployment topology

Entry scanning and position monitoring may become independently supervised services only after:

- Lifecycle state is moved to a transactional store.
- The execution gateway is the only component with broker-write authority.
- Action intents have unique idempotency keys.
- Symbol-level ownership or leasing is enforced.
- Restart reconciliation is proven in integration tests.

Even after process separation, Entry Setup and Position Health must never write orders directly.

## 6. Component responsibilities

### 6.1 Market Context Provider

Responsibilities:

- Fetch and cache bars once per symbol and timeframe.
- Fetch QQQ market-regime data once per scheduler pass.
- Publish completed-bar identifiers and timestamps.
- Reject out-of-order or stale observations.
- Provide both entry and holding timeframes.

Initial timeframes:

- Entry Setup: completed 5-minute bars.
- Position Health: completed 60-minute bars.
- Execution and hard-loss controls: broker position/trade data every 15 to 30 seconds.

Position Health should not use an old `dynamic_entry_plan` as its current trend context.

### 6.2 Entry Setup Engine

The Entry Setup engine is a pure calculation with no broker mutations.

Conceptual interface:

```python
def evaluate_entry_setup(
    symbol,
    bars,
    market_context,
    prior_exit,
    account_risk,
) -> EntryAssessment:
    ...
```

It owns:

- Pullback and breakout qualification.
- Volume and trend checks for entry.
- Chase protection.
- Market-regime qualification.
- Initial target and initial catastrophic stop proposal.
- Entry quantity proposal subject to portfolio risk validation.

It does not own:

- Existing position retention.
- Stop replacement for an open position.
- Reductions or exits.
- Ladder or add execution.

### 6.3 Position Health Engine

The Position Health engine is also a pure calculation.

Conceptual interface:

```python
def evaluate_position_health(
    position,
    position_episode,
    health_bars,
    market_context,
    active_protection,
) -> HealthAssessment:
    ...
```

It owns calculation of:

- Entry-relative return.
- Downside-state score.
- Trend-health score.
- Remaining reward/risk score.
- Data freshness and completeness.
- Recommended action: `hold`, `watch`, `reduce`, `exit`, or `freeze`.

It does not submit the recommended action.

### 6.4 Lifecycle Coordinator

The Lifecycle Coordinator owns the position state machine and action arbitration.

It must:

- Reconcile stored state with broker positions, orders, and fills.
- Create a position episode at the first fill.
- Preserve the Entry Setup snapshot used for that episode.
- Decide whether unfilled entry quantity remains eligible.
- Apply hard safety overrides before weighted scores.
- Resolve conflicts among entry, health, and portfolio-risk intents.
- Permit no more than one mutating action per symbol per cycle.
- Send approved intents to the Execution Gateway.

### 6.5 Execution Gateway

The Execution Gateway is the sole broker writer.

It owns:

- Order submission, replacement, and cancellation.
- Deterministic `client_order_id` generation.
- Quantity and buying-power validation immediately before submission.
- Pending-order reconciliation before retry.
- Stop coverage reconciliation after every fill.
- Prevention of duplicate entries and oversells.
- Serialization of actions for each symbol.

The gateway provides at-least-once processing with idempotent effects. It must never assume an order failed solely because the API response was lost.

### 6.6 Portfolio Risk Coordinator

The existing margin reducer will be converted from a direct order writer to an intent producer.

Portfolio-risk intents have higher priority than health-based holds or additions. The Lifecycle Coordinator remains responsible for coordinating stop cancellation, liquidation, and restored protection.

### 6.7 Reporting

Reporting is read-only. It consumes persisted assessments and execution events.

Candidate tables show `Entry Setup Score`. Open-position tables show `Position Health`, operational state, recommended action, stop coverage, and data timestamp. The cached Entry Setup score may appear only as an audit field such as `Setup at Entry`.

## 7. Authority transition

### 7.1 Flat symbol

Entry Setup is authoritative while broker position quantity is zero.

A new entry requires:

- Setup score at least 85.
- `active_signal` status.
- Data no older than two completed entry bars.
- Mandatory market, trend, reward/risk, volume, and chase gates.
- No blocking pending order or cooldown.
- Cash, notional, and portfolio-risk approval.

### 7.2 Entry pending with zero fills

Entry Setup continues to own the order. It may replace or cancel the pending limit order as the plan changes.

### 7.3 First partial fill

The first executed share creates a `PositionEpisode`. Position Health immediately becomes authoritative for filled shares.

The coordinator must:

1. Record the immutable setup snapshot and target.
2. Verify broker-held stop coverage for the filled quantity.
3. Calculate initial health.
4. Re-evaluate whether the remaining buy quantity may stay open.

Cancel the unfilled remainder if setup score falls below 85, setup status is no longer active, health falls below 65, market regime fails, or remaining reward/risk falls below 1.5.

### 7.4 Fully open position

Position Health remains authoritative until broker quantity returns to zero. A new Entry Setup may only be used as one input to a qualified add decision.

### 7.5 Flat after exit

Close the current episode, preserve it in the ledger, clear holding-specific state, and return authority to Entry Setup after cooldown and exit-ledger checks.

## 8. Position state machine

```text
FLAT
  -> ENTRY_PENDING
  -> OPEN_PARTIAL
  -> OPEN
  -> ADD_PENDING
  -> OPEN
  -> REDUCE_PENDING
  -> REDUCED
  -> EXIT_PENDING
  -> CLOSED
  -> FLAT
```

Additional operational substates:

- `UNPROTECTED`
- `DATA_STALE`
- `RECONCILING`
- `BROKER_INCONSISTENT`

Safety substates block entries and additions.

## 9. Position Health calculation

```text
Health Score = 45% Downside State
             + 35% Trend Health
             + 20% Remaining Reward/Risk
```

### 9.1 Downside State

Use broker average entry price after all fills:

```text
entry_return = current_price / average_entry_price - 1
```

Piecewise score:

| Entry-relative return | Downside score |
|---:|---:|
| 0% or better | 100 |
| -3% | 70 |
| -6% | 30 |
| -8% or worse | 0 |

Interpolate linearly between thresholds.

### 9.2 Trend Health

Initial 60-minute trend weights:

| Condition | Points |
|---|---:|
| Price above EMA21 | 25 |
| EMA9 above EMA21 | 20 |
| EMA21 slope positive | 20 |
| Price above EMA50 | 15 |
| Five-day relative strength versus QQQ positive | 10 |
| QQQ market regime favorable | 10 |

### 9.3 Remaining Reward/Risk

```text
remaining_reward = max(original_target - current_price, 0)
remaining_risk = max(current_price - effective_stop, minimum_price_increment)
remaining_r = remaining_reward / remaining_risk
rr_score = clamp(50 * remaining_r, 0, 100)
```

The original target is stored at entry and cannot be moved merely to improve the score.

After the original target is reached, the reward/risk component may score 100 only when the active stop protects a profit. Otherwise it remains low and prompts profit review.

### 9.4 Health labels

| Score | State | Default interpretation |
|---:|---|---|
| 80-100 | Healthy | Hold; potentially add-eligible |
| 65-79 | Stable | Hold; no add |
| 45-64 | Watch | Hold with closer monitoring |
| 25-44 | At Risk | Reduction candidate |
| 0-24 | Critical | Exit candidate |
| unavailable | Unavailable | Freeze discretionary actions |

### 9.5 Hard overrides

Hard overrides take precedence over the weighted score:

- Missing or undersized broker stop: `UNPROTECTED`.
- Entry-relative loss at or below 6%: reduce 50% once.
- Entry-relative loss at or below 8%: broker catastrophic exit.
- Price at or below intended stop: `EXIT_PENDING`.
- Reduction or exit already pending: no other action.
- Stale health data: freeze additions and score-based mutations.
- Price below EMA21 and EMA50 with a negative EMA21 slope: cap state at `At Risk`.

## 10. Addition policy

An addition is both a new allocation and a change to an existing position. It therefore requires both engines to approve it.

All conditions must pass:

1. Position Health is at least 80.
2. A fresh Entry Setup score is at least 85 and status is active.
3. Current price is at or above broker average entry.
4. Remaining reward/risk after the proposed add is at least 1.5.
5. Price is above EMA21 and EMA50 with positive EMA21 slope.
6. QQQ market regime is favorable.
7. No adverse reduction has occurred during the episode.
8. No add, reduction, or exit order is pending.
9. The existing stop is fully covering the position.
10. The stop will remain unchanged or tighten after the add.
11. The resulting position remains within the notional cap.
12. Resulting dollars at risk remain within the symbol risk budget.

Initial limits:

- Maximum one discretionary add per episode.
- Maximum add size of 50% of initial filled quantity.
- Default symbol risk budget of 0.75% of account equity.
- No averaging down.

Risk-sized add quantity:

```text
available_symbol_risk = symbol_risk_budget - current_risk_to_stop
add_qty = floor(available_symbol_risk / (add_price - active_stop))
```

The lower result from risk sizing, notional caps, cash availability, and the 50% add cap is used.

## 11. Reduction and exit policy

### 11.1 Hard reduction

At an entry-relative loss of 6%, submit a one-time market reduction for 50% of current quantity. Disable additions and ladder buys for the remainder of the episode. Restore stop coverage after the fill.

### 11.2 Health reduction

When Health remains between 25 and 44 for two distinct completed health bars, reduce 25% to 50% according to configured policy. A hard 6% reduction does not wait for bar confirmation.

### 11.3 Exit

Exit conditions:

- Broker catastrophic stop at an 8% entry-relative loss.
- Health below 25 for two distinct completed health bars.
- Structural trend failure with price below EMA21 and EMA50, negative EMA21 slope, and remaining reward/risk below 0.5.
- Protection cannot be restored.
- Broker state indicates a safety-critical inconsistency.

Existing profitable trailing-stop behavior remains valid. Stops may only remain unchanged or ratchet upward.

## 12. Data contracts

### 12.1 EntryAssessment

```json
{
  "assessment_id": "uuid",
  "symbol": "WAT",
  "as_of": "2026-08-11T15:55:00Z",
  "bar_id": "WAT:5Min:2026-08-11T15:55:00Z",
  "score": 92,
  "status": "qualified",
  "mode": "dynamic_breakout_continuation",
  "limit_price": 400.0,
  "target_price": 424.0,
  "initial_stop_price": 368.0,
  "requested_qty": 12,
  "blockers": [],
  "model_version": "entry-v1"
}
```

### 12.2 PositionEpisode

```json
{
  "episode_id": "WAT-20260811-001",
  "symbol": "WAT",
  "state": "OPEN",
  "state_version": 7,
  "opened_at": "2026-08-11T16:01:04Z",
  "entry_assessment_id": "uuid",
  "entry_setup_score": 92,
  "entry_mode": "dynamic_breakout_continuation",
  "original_target_price": 424.0,
  "initial_qty": 12,
  "current_qty": 12,
  "average_entry_price": 400.0,
  "active_stop_order_id": "uuid",
  "active_stop_price": 368.0,
  "active_stop_qty": 12,
  "add_count": 0,
  "adverse_reduction_completed": false,
  "pending_action_id": null
}
```

### 12.3 HealthAssessment

```json
{
  "assessment_id": "uuid",
  "episode_id": "WAT-20260811-001",
  "symbol": "WAT",
  "as_of": "2026-08-11T17:00:00Z",
  "bar_id": "WAT:1Hour:2026-08-11T17:00:00Z",
  "score": 72,
  "state": "stable",
  "downside_score": 80,
  "trend_score": 65,
  "reward_risk_score": 60,
  "entry_return_percent": -1.5,
  "remaining_r": 1.2,
  "recommended_action": "hold",
  "reasons": ["ema21_slope_negative"],
  "data_complete": true,
  "model_version": "health-v1"
}
```

### 12.4 ActionIntent

```json
{
  "action_id": "health:WAT:WAT-20260811-001:reduce-1",
  "episode_id": "WAT-20260811-001",
  "symbol": "WAT",
  "action": "reduce",
  "qty": 6,
  "priority": 80,
  "reason": "hard_loss_reduction_6pct",
  "assessment_id": "uuid",
  "expected_state_version": 7,
  "created_at": "2026-08-11T17:02:00Z"
}
```

## 13. Persistence and concurrency

### 13.1 Phase-one persistence

Continue per-symbol JSON state while all mutating components remain in one supervisor process. Persist assessments and lifecycle transitions atomically using temporary-file replacement.

The supervisor must enforce a per-symbol lock around reconciliation, coordination, execution, and state persistence.

### 13.2 Transactional persistence prerequisite

Before splitting processes, introduce SQLite with WAL mode and tables for:

- `position_episodes`
- `entry_assessments`
- `health_assessments`
- `action_intents`
- `broker_orders`
- `fills`
- `lifecycle_events`

State updates use optimistic concurrency:

```sql
UPDATE position_episodes
SET state = ?, state_version = state_version + 1
WHERE episode_id = ? AND state_version = ?;
```

A zero-row update means another worker changed the episode. The action must be abandoned and recalculated from current broker state.

### 13.3 Idempotency

Every action has a deterministic key. Before submitting, the gateway searches stored and broker orders by that key.

Example keys:

```text
entry:{symbol}:{assessment_id}
stop:{episode_id}:{state_version}
add:{episode_id}:{add_number}
reduce:{episode_id}:{reduction_number}
exit:{episode_id}
portfolio-reduce:{episode_id}:{request_id}
```

## 14. Scheduler behavior

### 14.1 Fast risk cycle

Every 15 to 30 seconds while the market is open:

1. Snapshot positions and open orders.
2. Resolve pending actions.
3. Verify stop coverage.
4. Apply 6% and 8% hard controls.
5. Repair protection before discretionary work.

Outside market hours, reconcile and restore eligible GTC stop protection, but defer software market reductions until the market is open.

### 14.2 Health cycle

On each new completed 60-minute bar:

1. Calculate a fresh health assessment.
2. Persist the assessment.
3. Update consecutive-bar counters.
4. Propose confirmed score-based reductions or exits.
5. Evaluate add eligibility last.

Repeated polling of the same bar cannot increment confirmation counters.

### 14.3 Entry cycle

On each new completed 5-minute bar, evaluate only flat symbols and symbols explicitly eligible for an add assessment.

## 15. Failure behavior

| Failure | Required behavior |
|---|---|
| Entry Setup unavailable | No new entries; open positions unaffected |
| Health market data stale | Block additions and score-based actions; retain hard broker protection |
| Position monitoring unavailable | Broker stops remain active; watchdog raises a critical alert |
| Broker response lost | Reconcile by client order ID before retrying |
| State persistence unavailable | Block new mutations; do not cancel valid protection |
| Reporting unavailable | No trading impact |
| Market feeds disagree | Freeze additions and score-based actions |
| Restart | Reconstruct episodes from broker positions, open orders, fills, and stored ledger |
| Stop quantity below position quantity | Mark unprotected and repair before any other discretionary action |

Health unavailability does not automatically liquidate a protected position. It disables discretionary adds and score-based actions while hard broker stops remain authoritative.

## 16. Observability and alerts

Every assessment and action must include:

- Symbol and episode ID.
- Model version.
- Source bar ID and timestamp.
- Input completeness.
- Component scores.
- Hard overrides.
- Recommended and approved actions.
- Rejection reason when an intent is denied.
- Broker order ID and client order ID.
- State version before and after mutation.

Critical alerts:

- Unprotected open position.
- Stop quantity mismatch.
- Position monitor heartbeat missing.
- Duplicate or conflicting sell orders.
- Broker quantity inconsistent with lifecycle state.
- Execution intent unresolved beyond a configured timeout.
- Health data stale for more than two health bars.

Suggested operational metrics:

- Open positions fully protected percentage.
- Time from first fill to stop confirmation.
- Health calculation latency.
- Health-state distribution.
- Hard and score-based reductions per day.
- Duplicate intents suppressed.
- Broker reconciliation corrections.
- Average and maximum adverse excursion by health bucket.

## 17. Configuration

Proposed global defaults:

```json
{
  "position_health": {
    "enabled": true,
    "shadow_mode": true,
    "timeframe": "1Hour",
    "max_data_age_bars": 2,
    "healthy_score": 80,
    "stable_score": 65,
    "watch_score": 45,
    "at_risk_score": 25,
    "reduction_confirmation_bars": 2,
    "exit_confirmation_bars": 2,
    "hard_reduction_loss_percent": 6,
    "hard_reduction_fraction": 0.5,
    "catastrophic_stop_loss_percent": 8,
    "minimum_add_health_score": 80,
    "minimum_add_setup_score": 85,
    "minimum_add_remaining_r": 1.5,
    "max_adds_per_episode": 1,
    "max_add_fraction_of_initial_qty": 0.5,
    "max_symbol_risk_percent": 0.75
  }
}
```

Symbol-specific overrides require documented out-of-sample evidence. Defaults should not be optimized separately for every ticker.

## 18. Proposed package layout

```text
traderbot/
  core_strategy_engine/
    entry_setup.py
    position_health.py
    assessments.py
    lifecycle/
      coordinator.py
      models.py
      transitions.py
      policies.py
  broker/
    execution_gateway.py
    intents.py
    reconciliation.py
    idempotency.py
  data/
    market_context.py
    lifecycle_store.py
  monitoring/
    portfolio_risk.py
    supervisor.py
    reports.py
```

This layout preserves the repository's existing ownership model: strategy decisions and lifecycle transitions remain in `core_strategy_engine`, broker mutations remain in `broker`, persistence remains in `data`, and portfolio scheduling and reporting remain in `monitoring`.

## 19. Testing strategy

### 19.1 Unit tests

- Downside-score boundary and interpolation tests.
- Trend-score component tests.
- Remaining reward/risk tests.
- Hard override precedence.
- First-fill authority handoff.
- Partial-fill remainder cancellation.
- Add qualification and rejection reasons.
- One-time reduction enforcement.
- Consecutive distinct-bar confirmation.
- Stop quantity repair.
- Idempotent order retry.
- State-version conflict handling.

### 19.2 Integration tests

- Partial entry fill followed by cancellation.
- Partial fill followed by additional fills and stop resizing.
- Hard reduction with an existing stop.
- Lost broker response followed by reconciliation.
- Margin-reduction intent competing with a health add.
- Restart with an open position and missing state.
- Restart with state but no broker position.
- Concurrent intents for the same symbol.

### 19.3 Historical replay

Replay every position snapshot and measure:

- Stop-before-target probability by health bucket.
- Next-session and next-five-session maximum adverse excursion.
- Subsequent return by bucket.
- Realized expectancy.
- Drawdown.
- Turnover and slippage.
- Monotonicity from Healthy through Critical.

Use purged walk-forward evaluation and keep validation symbols or periods outside weight selection.

### 19.4 Failure-injection tests

- Broker timeouts and rate limits.
- Duplicate fill events.
- Out-of-order order updates.
- Stale market bars.
- State write failures.
- Supervisor restart during a partial fill.
- Execution restart after submission but before response persistence.

## 20. Rollout plan

### Phase 0: Baseline capture

- Preserve current behavior and collect health-model input snapshots.
- Record existing entry, add, reduction, and exit decisions for comparison.

### Phase 1: Pure Health Engine

- Implement health calculation and contracts.
- Run in shadow mode.
- Do not change orders.
- Replace report `Strong` labels only after freshness and completeness are verified.

### Phase 2: Lifecycle Coordinator

- Introduce explicit episodes and first-fill handoff.
- Move decision arbitration out of the monolithic strategy loop.
- Preserve existing execution behavior.

### Phase 3: Execution Gateway

- Route every entry, stop, add, reduction, exit, and margin action through one gateway.
- Add idempotency and symbol serialization.

### Phase 4: Health reductions and exits

- Enable score-based actions with conservative confirmation.
- Keep 6% and 8% hard controls unchanged.
- Compare live shadow recommendations with actual outcomes.

### Phase 5: Health-qualified additions

- Disable legacy averaging-down ladders.
- Enable additions only after out-of-sample and shadow validation.
- Start with one add at no more than 25% of initial quantity before allowing 50%.

### Phase 6: Transactional store and process separation

- Introduce SQLite lifecycle storage.
- Move Position Health to an independently supervised service if operational evidence justifies it.
- Keep the execution gateway as the sole broker writer.

## 21. Acceptance criteria

The implementation is complete when:

1. Open positions never display Entry Setup labels as current health.
2. The first partial fill creates a position episode and receives stop protection.
3. Health assessments use fresh holding-period data and actual broker quantities.
4. No add is possible below Health 80, after a reduction, or without full stop coverage.
5. Hard 6% reduction and 8% catastrophic-stop behavior remains enforced.
6. Only one component can mutate broker orders.
7. Duplicate action delivery cannot create duplicate effective orders.
8. Margin reduction cannot race position-stop management.
9. Reports expose component scores, action state, stop coverage, and data timestamp.
10. Restart reconciliation reconstructs a safe state from broker truth.
11. Unit, integration, replay, and failure-injection suites pass.
12. Shadow results demonstrate monotonic deterioration in realized outcomes across health buckets before score-based trading is enabled.

## 22. Principal architectural risks

### Competing order ownership

The largest implementation risk is allowing health management, the current strategy loop, and the margin reducer to mutate the same orders. The execution gateway must be established before enabling new health-driven orders.

### False precision in the score

The initial 45/35/20 weights are policy priors, not calibrated probabilities. Reports must call the value a score rather than a predicted success probability.

### Timeframe mismatch

Using five-minute entry indicators to judge positions held for days creates noise and turnover. Holding health requires a slower primary trend timeframe.

### State corruption during process separation

JSON state has no cross-process transaction or compare-and-swap protection. Independent services must wait until transactional persistence is available.

### Stop gaps

Software reductions require coordination with existing stops. Broker-held catastrophic protection remains mandatory, and the execution gateway must minimize and observe any cancel/replace window.

## 23. Final decision summary

- Separate Entry Setup and Position Health as pure domain engines.
- Switch authority at the first fill.
- Keep one supervisor process during the first implementation phases.
- Add a Lifecycle Coordinator for state and priority arbitration.
- Establish one Execution Gateway before enabling health-driven trades.
- Convert margin reduction to an intent producer.
- Run Position Health in shadow mode before activating score-based actions.
- Permit independent processes only after transactional state and idempotent execution are complete.

## 24. Implementation status

Implemented:

- Typed Entry, Position Health, and Action Intent contracts.
- Pure Position Health calculation with component scores and hard overrides.
- First-fill position episodes persisted in per-symbol state.
- Fresh 60-minute health context with relative strength versus QQQ.
- Distinct completed-bar confirmation tracking.
- Shadow-mode score-driven reduce and exit arbitration.
- One serialized execution gateway shared by strategy and portfolio-risk activity.
- Deterministic client order IDs for hard and health-driven reductions.
- Position Health fields and labels in daily reports.
- Global health defaults in `config/watchers.json`.
- Focused health, lifecycle, execution, risk-control, and reporting tests.

Intentionally gated or deferred according to this design:

- `shadow_mode` remains enabled; health-score actions are not live.
- Health-qualified additions remain disabled.
- SQLite lifecycle persistence is deferred until multi-process separation.
- Entry scanning and health monitoring remain logical components in one supervisor process.
- The margin reducer uses the shared execution gateway and symbol transaction lock; conversion to a durable persisted intent queue remains a later transactional-store migration.
