# Phase 5: Portfolio ranking and allocation

The supervisor coordinates new-candidate entries in two passes on a shared,
completed five-minute snapshot. The first pass refreshes each strategy plan with
order submission blocked. The portfolio allocator then ranks hard-safe candidates
and grants a bar-specific authorization only to selected symbols. The authorized
strategy reruns through the existing execution gateway and its original sizing,
cash, and risk controls.

Ranking uses setup score multiplied by expected reward/risk, less penalties for
existing sector and correlated-factor exposure. Stable secondary keys make the
decision deterministic. The audit log is `runtime/logs/portfolio_allocations.jsonl`
and the daily counters and most recent decision are persisted in
`runtime/state/portfolio_allocation_state.json`.

Controls under `portfolio_allocator` cover simultaneous positions, aggregate open
risk, sector and correlated-factor exposure, symbol notional, daily entries,
daily turnover, cash reserve, minimum score, and separation between neighboring
scores. Broker positions and open buy orders are reserved before new allocations.

The initial deployment uses `shadow_mode: true`: it produces rankings and audits
but cannot authorize an order. Set it to `false` only after reviewing complete
five-minute snapshots and validating allocation behavior against current account
exposure. Hard strategy gates and the execution gateway remain mandatory in
either mode.
