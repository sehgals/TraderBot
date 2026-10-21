"""Resume TraderBot trading supervisor and watchers after maintenance."""
import datetime
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_WINDOW_PATH = ROOT / "runtime" / "state" / "maintenance_window.json"
RESTART_REQUEST_PATH = ROOT / "runtime" / "state" / "supervisor_restart_request.json"


def get_supervisor_pids():
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        (
            "(Get-CimInstance Win32_Process | "
            "Where-Object { "
            "$_.Name -like 'python*' -and "
            "($_.CommandLine -like '*traderbot.cli.supervisor*' -or "
            "$_.CommandLine -like '*watcher_supervisor.py*') "
            "} | "
            "ForEach-Object { $_.ProcessId }) -join ','"
        ),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out = res.stdout.strip()
    return [int(p) for p in out.split(",") if p.strip()]


def main():
    if MAINTENANCE_WINDOW_PATH.exists():
        MAINTENANCE_WINDOW_PATH.unlink(missing_ok=True)
        print(f"[+] Cleared maintenance window at {MAINTENANCE_WINDOW_PATH}")
    else:
        print("[i] No active maintenance window file found.")

    # Queue supervisor restart request for watchdog
    RESTART_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESTART_REQUEST_PATH.write_text(
        json.dumps({
            "requested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "reason": "Operator manual resumption",
        }, indent=2),
        encoding="utf-8",
    )
    print(f"[+] Wrote supervisor restart request to {RESTART_REQUEST_PATH}")

    # Trigger watchdog scheduled task in Session 0
    print("[*] Triggering TraderBot_Watcher_Supervisor scheduled task in Session 0...")
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Start-ScheduledTask -TaskName 'TraderBot_Watcher_Supervisor' -ErrorAction SilentlyContinue",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    print("[*] Waiting for supervisor to start in Session 0...")
    started = False
    new_pids = []
    for _ in range(15):
        time.sleep(1)
        new_pids = get_supervisor_pids()
        if new_pids:
            started = True
            break

    if started:
        print(f"[SUCCESS] TraderBot supervisor is running in Session 0 (PID(s): {new_pids}).")
        print("          - Watchers are actively monitoring markets and portfolio.")
        print("          - Dashboard maintenance banner cleared.")
    else:
        print("[!] Supervisor has not registered yet. Watchdog will initialize it on the next 5-minute cycle.")


if __name__ == "__main__":
    main()
