#!/usr/bin/env bash
set -euo pipefail

# Install Linux systemd schedules for TraderBot.
# Run from the repo root on the Linux host:
#   chmod +x scripts/schedule_linux_tasks.sh
#   sudo ./scripts/schedule_linux_tasks.sh

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$USER}}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
WATCHERS_CONFIG="${WATCHERS_CONFIG:-$PROJECT_DIR/config/watchers.json}"
LOG_DIR="$PROJECT_DIR/runtime/logs"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

SUPERVISOR_SERVICE="traderbot-supervisor.service"
WATCHDOG_SERVICE="traderbot-watchdog.service"
WATCHDOG_TIMER="traderbot-watchdog.timer"
REPORT_SERVICE="traderbot-daily-report.service"
REPORT_TIMER="traderbot-daily-report.timer"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root, for example: sudo $0" >&2
  exit 1
fi

if [[ ! -f "$WATCHERS_CONFIG" ]]; then
  echo "Watchers config not found: $WATCHERS_CONFIG" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found. Set PYTHON_BIN=/path/to/python and rerun." >&2
  exit 1
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$LOG_DIR"

cat > "$SYSTEMD_DIR/$SUPERVISOR_SERVICE" <<EOF
[Unit]
Description=TraderBot watcher supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON_BIN -m traderbot.cli.supervisor --config $WATCHERS_CONFIG
Restart=always
RestartSec=10
StandardOutput=append:$LOG_DIR/watcher_supervisor.out.log
StandardError=append:$LOG_DIR/watcher_supervisor.err.log

[Install]
WantedBy=multi-user.target
EOF

cat > "$SYSTEMD_DIR/$WATCHDOG_SERVICE" <<EOF
[Unit]
Description=TraderBot supervisor watchdog

[Service]
Type=oneshot
User=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=/bin/systemctl start $SUPERVISOR_SERVICE
EOF

cat > "$SYSTEMD_DIR/$WATCHDOG_TIMER" <<EOF
[Unit]
Description=Run TraderBot watchdog every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=15s
Unit=$WATCHDOG_SERVICE

[Install]
WantedBy=timers.target
EOF

cat > "$SYSTEMD_DIR/$REPORT_SERVICE" <<EOF
[Unit]
Description=TraderBot daily report
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON_BIN -m traderbot.cli.reports --config $WATCHERS_CONFIG
StandardOutput=append:$LOG_DIR/daily_report.out.log
StandardError=append:$LOG_DIR/daily_report.err.log
EOF

cat > "$SYSTEMD_DIR/$REPORT_TIMER" <<EOF
[Unit]
Description=Run TraderBot daily report after market close

[Timer]
OnCalendar=Mon..Fri 16:15:00
Persistent=true
Unit=$REPORT_SERVICE

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "$SUPERVISOR_SERVICE"
systemctl enable --now "$WATCHDOG_TIMER"
systemctl enable --now "$REPORT_TIMER"

echo "Installed TraderBot Linux schedules."
echo "Supervisor: systemctl status $SUPERVISOR_SERVICE"
echo "Watchdog:   systemctl list-timers $WATCHDOG_TIMER"
echo "Reports:    systemctl list-timers $REPORT_TIMER"
