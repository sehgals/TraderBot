"""Gracefully shut down TraderBot trading supervisor and watchers for maintenance."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_WINDOW_PATH = ROOT / "runtime" / "state" / "maintenance_window.json"
SUPERVISOR_STATUS_PATH = ROOT / "runtime" / "state" / "supervisor_status.json"


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
    parser = argparse.ArgumentParser(description="Shut down TraderBot trading supervisor.")
    parser.add_argument("--hours", type=float, default=4.0, help="Target shutdown duration in hours (default: 4.0)")
    parser.add_argument("--auto-resume", action="store_true", help="Automatically resume when duration expires")
    parser.add_argument("--reason", default="Operator requested shutdown", help="Shutdown reason")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc)
    resume_dt = now + datetime.timedelta(hours=args.hours)

    payload = {
        "active": True,
        "paused_at": now.isoformat(),
        "target_duration_hours": args.hours,
        "auto_resume": args.auto_resume,
        "resume_at": resume_dt.isoformat(),
        "reason": f"{args.reason} ({args.hours:g}h target; manual resume required)" if not args.auto_resume else f"{args.reason} ({args.hours:g}h auto-resume)",
    }

    MAINTENANCE_WINDOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAINTENANCE_WINDOW_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[+] Maintenance window written to {MAINTENANCE_WINDOW_PATH}")
    print(f"    Paused at:             {payload['paused_at']}")
    print(f"    Target duration:       {args.hours:g} hours")
    print(f"    Target resume time:    {payload['resume_at']}")
    print(f"    Auto-resume policy:    {'Automatic' if args.auto_resume else 'Manual operator trigger required'}")

    # Trigger watchdog scheduled task in Session 0 to consume the maintenance window immediately
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

    # Wait up to 10 seconds for supervisor process to terminate
    print("[*] Verifying supervisor shutdown...")
    stopped = False
    for _ in range(10):
        time.sleep(1)
        pids = get_supervisor_pids()
        if not pids:
            stopped = True
            break

    if stopped:
        print("[SUCCESS] TraderBot supervisor has stopped completely.")
        print("          - All watcher evaluation loops are halted.")
        print("          - No new orders or entries will be submitted.")
        print("          - Existing broker stop-loss orders remain active on Alpaca.")
        print("          - Dashboard remains online in maintenance mode.")
    else:
        print(f"[!] Supervisor process still active (PID(s): {pids}). Watchdog will enforce termination on next cycle.")


if __name__ == "__main__":
    main()
