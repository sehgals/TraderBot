# Dashboard implementation phases

Token figures below are proposed work budgets, not measured usage or guarantees.
Each phase includes implementation, focused verification, and a handoff describing
files changed, checks passed, limitations, and the next phase. Stop at each phase
boundary for review. Do not deploy or start a later phase automatically.

## Current checkpoint

- Phase 1 is implemented and verified. The FastAPI backend and locally served
  React interface are under `traderbot/dashboard`, with a launcher at
  `traderbot/cli/dashboard.py`. See `docs/dashboard.md` for operating instructions.
- Optional Python dependencies are installed in `runtime/dashboard-deps`, separate
  from the bot's Python dependencies. React assets are served locally.
- The viewer reads only saved reports. Broker polling, watcher reads, and SSE
  from the initial prototype were removed to keep Phase 1 independently reviewable.
- At the Phase 1 checkpoint, no dashboard task was installed. A temporary viewer was
  started for verification on port 8765.
- Production bot configuration has not been changed.
- Five backend tests passed for report fidelity, field allowlisting, missing/bad
  reports, refresh, numeric validation, and restricted routes.
- Desktop and mobile rendering were inspected with local headless Edge after the
  in-app browser tool failed to initialize. Browser checks passed for filters,
  details, empty/error states, responsive width, and no external requests.
- Screenshots: `runtime/dashboard-desktop.png` and `runtime/dashboard-mobile.png`.
- Phase 2 is complete: shared broker cache, per-section stale indicators, SSE,
  reconnection, read-only orders, and timestamped report/watcher health selection.
- Live broker data was verified for all three current positions. Thirteen backend
  tests passed, along with browser checks for two tabs sharing the same refresh,
  SSE updates, disconnection/reconnection, live rendering, and mobile width.
- Live-mode screenshots: `runtime/dashboard-live.png` and
  `runtime/dashboard-live-mobile.png`.
- Phase 3 is complete and verified (2026-09-07). Existing candidate, order,
  alert-history, and watcher-activity views were audited and completed. Holdings
  changes invalidate candidate caching; malformed activity timestamps raise source
  issues; error polling uses the configured fallback interval.
- All 23 dashboard backend tests passed, including bounded tails, partial writes,
  malformed records, rotation, missing sources, closed-market timing, candidate
  cache invalidation, and timestamped/allowlisted alerts. Fake-broker browser checks
  passed for shared refresh, SSE/reconnect, operational filters, blockers, empty
  states, and mobile width. Mobile screenshot inspected:
  `runtime/dashboard-live-test.png`.
- Activity never verifies process health. The task principal was checked as
  SYSTEM / ServiceAccount / Highest; a pythonw.exe process was observed in session
  0, but its supervisor command line was not visible, so its identity remains
  unverified. No production service code or configuration was changed.
- Phase 5 was requested separately and implemented on 2026-09-07 before Phase 4.
  Added keyboard-focusable scrolling tables, responsive filters, explicit
  loading state, and light landscape print styling with automatic detail expansion
  and restoration. Desktop/mobile/print screenshots were visually inspected.
- Phase 5 verification: 23 backend tests passed; live browser regression passed;
  isolated release checks cover keyboard access, filters, health details, loading,
  stale values, missing reports, failure/retry, responsive widths, and print styles.
  See `scripts/verify_dashboard_release.py` and `docs/dashboard.md` for reproduction
  and limitations. No service installation, commit, or push was performed.
- Phases 1, 2, 3, and 5 were committed and pushed as `c9e53c6` at the user's request.
- Phase 4 is complete (2026-09-07). `TraderBot_Dashboard` was installed and started
  as SYSTEM / ServiceAccount / Highest, using pythonw.exe and an at-startup trigger.
  The service captures rotating logs and binds only to `127.0.0.1:8765`.
- Elevated service verification passed: dashboard PID 29232 was replaced by 7728
  during a dashboard-only restart, and both ran as SYSTEM in session 0. The
  production supervisor remained PID 5768, verified as SYSTEM in session 0 with
  SYSTEM / ServiceAccount / Highest task configuration. No enforcement change was
  needed. HTTP returned 200 and the live snapshot returned available.
- Verification evidence: `runtime/dashboard-service-verification.json`. The
  installer and repeatable verifier are `scripts/configure_dashboard_task.ps1`
  and `scripts/verify_dashboard_service.ps1`. All 23 dashboard backend tests passed.
  Boot behavior is configured but was not tested with a machine reboot.
  Phase 4 changes have not been committed or pushed.

## Phase 1 — Read-only portfolio viewer

Suggested budget: 5,000 tokens.

Deliver a locally served page using the latest saved report. Show equity, cash,
buying power, positions, health, stop coverage, and source timestamps. Keep broker
polling disabled for this acceptance step. Verify that only dashboard assets and
explicit snapshot fields are exposed; no credentials or arbitrary files.

Acceptance: the page agrees with the saved report; empty and missing reports have
clear states; no trading calls or production-state writes occur. Record a desktop
visual check when browser tooling is available.

## Phase 2 — Live broker data and reconnect handling

Suggested budget: 6,000 tokens.

Implement the shared 20-second account, position, order, and market-clock refresh.
Serve snapshots and SSE updates to all tabs from a single cache. Keep last-known
values on failure, with accurate timestamps and stale indicators. Define ordering
for saved-report versus watcher health using parsed timestamps and assessment
evaluation time. Never label old health as current just because prices refreshed.

Acceptance: test successful refresh, partial broker failure, market closure,
missing credentials, disconnect/reconnect, position removal, and concurrent tabs.
No per-browser broker polling and no trading actions.

## Phase 3 — Watcher activity and operational visibility

Suggested budget: 5,000 tokens.

Finish candidate, order, alert, and watcher activity views. Handle partial JSON
writes, malformed records, log rotation, and absent files. Separate activity-based
status from verified process health. Respect the bot's longer closed-market poll
interval when deciding whether activity is overdue.

Acceptance: bounded log reads; informative empty states; source timestamps;
no false process-running claims based only on recent files.

## Phase 4 — Dashboard service and startup

Suggested budget: 4,000 tokens.

Create a dedicated dashboard scheduled task under SYSTEM, separate from
`TraderBot_Watcher_Supervisor`. Bind to loopback only, run without a visible
console, capture logs, and document start/stop/restart and dependency installation.
Verify the existing bot task retains SYSTEM, ServiceAccount, highest privileges,
and its pythonw.exe supervisor remains in service session 0. Use
`scripts/configure_watcher_supervisor_task.ps1` if enforcement is necessary.

Acceptance: dashboard starts independently and survives a dashboard-only restart;
its URL responds; production supervisor identity and session are verified.

## Phase 5 — Visual polish and release verification

Suggested budget: 4,000 tokens.

Verify desktop/mobile layouts, keyboard access, filters, expanded health details,
loading and stale states, and print styling. Run the focused regression suite,
document limitations and operating instructions, and prepare a reviewable diff.
Commit or push only when requested.

Acceptance: inspected browser rendering, passing regression checks, documented
local URL and startup procedure, and a concise release handoff.

## Budget management

Suggested total: 24,000 tokens across five separate work phases. These estimates
include verification and handoff but may change when failures require diagnosis.
Existing prototype work should be reused rather than rebuilt.

For an explicit enforced token budget, request one phase with its token budget.
At a phase boundary, report completion honestly; if checks are unfinished, preserve
the checkpoint and describe what remains instead of declaring the phase complete.
