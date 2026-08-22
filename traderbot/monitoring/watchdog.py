import datetime
import json
import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "runtime" / "logs"
LOG_PATH = LOG_DIR / "watcher_monitor.log"
ALERT_PATH = LOG_DIR / "watcher_monitor_alerts.jsonl"
RESTART_REQUEST_PATH = PROJECT_ROOT / "runtime" / "state" / "supervisor_restart_request.json"
WATCHER_SUPERVISOR_TASK = "TraderBot_Watcher_Supervisor"
PYTHONW_EXE = (
    Path(r"C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python")
    / "pythonw.exe"
)
WATCHERS_CONFIG = PROJECT_ROOT / "config" / "watchers.json"
SUPERVISOR_OUT = LOG_DIR / "watcher_supervisor.out.log"
SUPERVISOR_ERR = LOG_DIR / "watcher_supervisor.err.log"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def local_now():
    return datetime.datetime.now().astimezone().isoformat()


def write_monitor_log(message):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(f"{local_now()} {message}\n")


def write_alert(symbol, task_name, message):
    payload = {
        "timestamp": local_now(),
        "symbol": symbol,
        "task_name": task_name,
        "message": message,
    }
    ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, separators=(",", ":")))
        file.write("\n")

    user = os.environ.get("USERNAME")
    if not user:
        return

    try:
        result = subprocess.run(
            ["msg", user, "/TIME:60", f"TraderBot alert: {message}"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=NO_WINDOW,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            write_monitor_log(f"Windows msg notification failed for {symbol}: {details}")
    except Exception as exc:
        write_monitor_log(f"Windows msg notification failed for {symbol}: {exc}")


def get_supervisor_process_ids():
    command = [
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
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(details or "Process check for watcher_supervisor.py failed")
    output = result.stdout.strip()
    return [int(value) for value in output.split(",") if value.strip()]


def get_supervisor_process_count():
    return len(get_supervisor_process_ids())


def is_supervisor_running():
    return get_supervisor_process_count() > 0


def consume_restart_request():
    if not RESTART_REQUEST_PATH.exists():
        return False
    request = json.loads(RESTART_REQUEST_PATH.read_text(encoding="utf-8"))
    expected_pid = int(request["expected_pid"])
    supervisor_pids = get_supervisor_process_ids()
    if expected_pid not in supervisor_pids:
        raise RuntimeError(
            f"restart request PID {expected_pid} is not a named watcher supervisor; "
            f"found {supervisor_pids}"
        )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"Stop-Process -Id {expected_pid} -Force",
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(details or f"failed to stop supervisor PID {expected_pid}")
    RESTART_REQUEST_PATH.unlink()
    write_monitor_log(f"Consumed restart request and stopped supervisor PID {expected_pid}.")
    start_supervisor()
    write_monitor_log("Restart request completed; started a fresh watcher supervisor.")
    return True


def start_supervisor():
    if not PYTHONW_EXE.exists():
        raise FileNotFoundError(f"Python executable not found: {PYTHONW_EXE}")
    if not WATCHERS_CONFIG.exists():
        raise FileNotFoundError(f"Watchers config not found: {WATCHERS_CONFIG}")

    SUPERVISOR_OUT.parent.mkdir(parents=True, exist_ok=True)
    stdout = SUPERVISOR_OUT.open("a", encoding="utf-8")
    stderr = SUPERVISOR_ERR.open("a", encoding="utf-8")
    subprocess.Popen(
        [
            str(PYTHONW_EXE),
            "-m",
            "traderbot.cli.supervisor",
            "--config",
            str(WATCHERS_CONFIG),
        ],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        creationflags=NO_WINDOW,
    )


def main():
    os.chdir(PROJECT_ROOT)

    try:
        if consume_restart_request():
            return 0
    except Exception as exc:
        write_monitor_log(f"Failed to consume supervisor restart request: {exc}")
        return 4

    try:
        running = is_supervisor_running()
    except Exception as exc:
        write_monitor_log(f"Failed to inspect named Python watcher supervisor process: {exc}")
        return 2

    if running:
        write_monitor_log("Named Python watcher supervisor is running.")
        return 0

    message = "Named Python watcher supervisor was down; restarting supervisor module."
    write_monitor_log(message)
    write_alert("ALL", WATCHER_SUPERVISOR_TASK, message)

    try:
        start_supervisor()
        write_monitor_log(
            f"Restart result for Python watcher supervisor: started {PYTHONW_EXE}"
        )
    except Exception as exc:
        write_monitor_log(f"Restart result for Python watcher supervisor: {exc}")
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
