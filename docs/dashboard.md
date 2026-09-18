# Read-only dashboard

The default mode uses one background collector for all browser tabs. Every 20
seconds it retrieves account balances, positions, recent orders, the market clock,
and trading calendar through GET requests. Slow requests can delay a cycle.
Each section keeps its last successful data and timestamp on failure; failures or
data older than 60 seconds are marked stale. Only selected fields reach browsers.

An initial HTTP snapshot is followed by server-sent events every five seconds.
Refreshing the view or opening another tab does not request new broker data.
Browsers automatically reconnect after stream errors and online/offline changes;
a 15-second heartbeat timeout also reconnects stalled streams. Last-known values
remain visible with a disconnected notice.

Health is read from saved reports and configured watcher state, without running
strategy evaluation or writing state. Holdings must match quantity and average
entry price before inheriting an assessment. Parsed evaluation timestamps select
the newest assessment; bar timestamps break ties. Future timestamps are rejected.
The display separates price-fetch time, health-bar time, evaluation time, and source.
Fresh prices do not refresh health. Closed markets use the last session close from
the trading calendar, including holidays and early closes. Stop coverage belongs
to the assessment timestamp; it is not a live reconciliation of protective orders.

Missing credentials leave saved-report values available with stale indicators;
raw exception messages and credentials are never sent to browsers. Credentials
are loaded from the repository's `.env` only in live mode. The collector has no
write endpoints and never imports the execution gateway.

## Saved-report mode

The local React/FastAPI viewer displays the latest `daily_report_YYYY-MM-DD.json`
from `runtime/reports/daily`, selected by report date. It shows equity, cash,
buying power, daily return, holdings, health components, stop coverage, and health
bar timestamps. The header identifies the report date and report-file save time.
Fresh/complete labels in score details describe the assessment at report time.

With `--offline`, the viewer does not load credentials, contact the broker, evaluate strategies,
read watcher state, or write reports. Refresh report reloads the saved file; it
does not regenerate a report.

## Run from PowerShell in the repository

```powershell
$DashboardPython = 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $DashboardPython -m pip install --target runtime/dashboard-deps -r traderbot/dashboard/requirements.txt
& $DashboardPython -m traderbot.cli.dashboard
# Saved-report mode instead:
# & $DashboardPython -m traderbot.cli.dashboard --offline
```

Open <http://127.0.0.1:8765>. Stop the foreground viewer with Ctrl+C. Use
`--port 8766` if the default port is occupied. The viewer always binds to loopback.
The production bot and watchdog continue through their existing SYSTEM task.

Only explicitly listed fields and four frontend assets are served. Unknown file
paths and write methods are rejected. React assets are bundled locally (18.3.1,
MIT license in `static/vendor/LICENSE-react.txt`); no CDN is contacted by the page.
Missing or malformed reports produce a visible message. The latest broken report
is not silently replaced with an older one. Malformed/nonfinite numeric values
display as unavailable, and raw account error messages are not exposed.

## Verification

```powershell
& $DashboardPython -m pip install --target runtime/dashboard-test-deps -r traderbot/dashboard/requirements-test.txt
$env:PYTHONPATH = 'runtime/dashboard-test-deps;runtime/dashboard-deps'
& $DashboardPython -m unittest discover -s tests/unit -p 'test_dashboard*.py'
# With the offline dashboard running on port 8765 and Microsoft Edge installed:
& $DashboardPython scripts/verify_dashboard.py
# Independent fake-broker SSE/reconnection checks on temporary port 8767:
& $DashboardPython scripts/verify_dashboard_live.py
# Isolated release checks on temporary port 8768 (no running viewer required):
& $DashboardPython scripts/verify_dashboard_release.py
```

Browser checks save desktop/mobile screenshots under `runtime/`. Tests use
temporary reports and simulated browser responses, leaving production files intact.
Service startup and restart verification are described below.

## Watcher operations

Live mode includes recorded entry candidates, recent broker orders, alert history,
and watcher activity. Symbol filters, an attention filter, expandable candidate
blockers, and a show-all toggle support inspection. Candidates exclude held and
disabled symbols. Orders include status filtering and their broker fetch time.

Operational files are shared across tabs and checked at most every five seconds
(holdings changes also refresh candidate selection). Each log read is limited to
128 KiB and its last 40 complete records; JSON sources are limited to 2 MiB.
Partial or malformed records are skipped. Unreadable or partially written sources
retain previous values with source issues; empty replacement logs clear activity.
Rotation is detected on the next check. This is a recent window, not a full archive.

Watcher status uses event timestamps and the recorded next-run interval, including
the longer closed-market delay. Configured default intervals provide fallbacks.
An overdue event includes a grace period of 60 to 300 seconds. Assessment and event
timestamps remain separate from the time files were checked. Missing sources and
empty views are explicitly identified; alerts describe historical events.
Supervisor process health is always marked `Not verified`: recent file activity
does not establish that a process is running. Raw exception and watchdog message
text are excluded from browser responses.

## Phase 5 release verification and use

Phase 5 visual and accessibility work is implemented. Start the foreground viewer
with the PowerShell command above, open <http://127.0.0.1:8765>, and reload an
existing tab to load updated assets. Stop with Ctrl+C; restart using the same
command. When the service occupies port 8765, use `--port 8766` for a foreground viewer.

Use Tab to reach filters, scrollable tables, and detail summaries. Arrow keys
scroll a focused table horizontally; Enter toggles focused details. Small screens
keep the page within the viewport while tables scroll to reveal all columns.
Print uses a light landscape layout and expands details automatically, restoring
the prior expansion state afterwards. Printing includes the currently filtered
rows; clear filters and choose Show all operational rows for the full loaded view.

Release checks use temporary fixtures and cover desktop, 320/390/768px layouts,
keyboard navigation, health expansion, loading, unavailable sources, stale values,
refresh failure, retry, and print styles. The live suite separately verifies SSE,
shared refresh, reconnection, and operational controls. Screenshots are saved as
`runtime/dashboard-release-{desktop,mobile,print,stale}.png`.

Verification uses headless Microsoft Edge because the in-app browser could not
initialize. Print CSS was inspected through browser print-media emulation; physical
printer output and a full assistive-technology audit were not tested. Live broker
availability and dashboard service startup are outside these fixture checks.

## SYSTEM dashboard service (Phase 4)

The dedicated `TraderBot_Dashboard` scheduled task runs
`pythonw.exe -m traderbot.cli.dashboard_service --port 8765` from the repository
directory. It uses SYSTEM, ServiceAccount logon, highest privileges, and an
at-startup trigger; no signed-in desktop or visible console is required. It binds
only to <http://127.0.0.1:8765>. The production bot retains its separate
`TraderBot_Watcher_Supervisor` task.

Install dependencies using the commands above, then run from an administrator
PowerShell in this repository:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/configure_dashboard_task.ps1 -Start
Get-ScheduledTask -TaskName TraderBot_Dashboard
Stop-ScheduledTask -TaskName TraderBot_Dashboard
Start-ScheduledTask -TaskName TraderBot_Dashboard
# Verify identity, HTTP, loopback binding, and a dashboard-only stop/start:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/verify_dashboard_service.ps1
```

For a restart, stop the dashboard task, wait until it is Ready, and start it again.
The verification script performs this sequence and confirms the supervisor PID
stays unchanged. `-Install` first configures and starts the dashboard task. The
verification script checks the default port 8765; the installer also supports a
custom `-Port` and `-Pythonw` path. Stop the task before rerunning configuration.

Service startup/errors and HTTP access logs go to
`runtime/logs/dashboard-service.log`, rotating at 5 MiB with three backups.
The verification report is `runtime/dashboard-service-verification.json`.
The task has no execution time limit, ignores duplicate starts, runs on battery,
and retries failed exits three times at one-minute intervals. It does not install
dependencies automatically. After editing Python code or dependencies, restart
the dashboard task; after editing frontend assets, reload the browser tab.
Boot startup is configured without rebooting the production machine. Task queries
and management require administrator PowerShell; a non-elevated query may report
the task as missing even while its local URL responds.
