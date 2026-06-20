import argparse
import concurrent.futures
import datetime
import json
import os
import signal
import time
import traceback
from pathlib import Path

from traderbot.core_strategy_engine.engine import (
    AlpacaClient,
    load_env,
    load_json,
    run_once,
    save_json,
)


DEFAULT_WATCHERS_PATH = "config/watchers.json"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso_now():
    return utc_now().isoformat()


def resolve_path(project_root, path):
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return project_root / candidate


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, sort_keys=True))
        file.write("\n")


def load_supervisor_config(path):
    config_path = Path(path).resolve()
    project_root = config_path.parent.parent if config_path.parent.name == "config" else config_path.parent
    config = load_json(config_path, {})
    watchers = []
    watchers.extend(
        {**watcher, "group": watcher.get("group", "managed")}
        for watcher in config.get("managed_watchers", config.get("watchers", []))
    )
    watchers.extend(
        {**watcher, "group": watcher.get("group", "new")}
        for watcher in config.get("new_watchers", [])
    )
    if not watchers:
        raise ValueError(f"No watchers configured in {config_path}")

    seen_symbols = set()
    prepared = []
    for index, watcher in enumerate(watchers):
        symbol = watcher.get("symbol")
        group = watcher.get("group", "managed")
        if not symbol:
            raise ValueError(f"Watcher #{index + 1} is missing symbol")
        if symbol in seen_symbols:
            raise ValueError(f"Duplicate watcher symbol: {symbol}")
        seen_symbols.add(symbol)

        config_file = watcher.get("config")
        state_file = watcher.get("state")
        defaults_key = f"{group}_watcher_defaults"
        config_defaults = {
            **config.get(defaults_key, {}),
            **watcher.get("config_defaults", {}),
        }
        if not config_file and not config_defaults:
            raise ValueError(f"{symbol} watcher must define config or {defaults_key}")
        if not state_file:
            raise ValueError(f"{symbol} watcher must define state")

        prepared.append(
            {
                "symbol": symbol,
                "group": group,
                "config_path": resolve_path(project_root, config_file)
                if config_file
                else None,
                "config_defaults": config_defaults,
                "state_path": resolve_path(project_root, state_file),
                "log_path": resolve_path(
                    project_root, watcher.get("log", f"{symbol.lower()}_watcher.jsonl")
                ),
                "enabled": watcher.get("enabled", True),
                "next_run_at": 0.0,
                "failures": 0,
            }
        )

    return project_root, config, prepared


def seconds_until_next_open(clock):
    next_open = clock.get("next_open")
    if not next_open:
        return None
    next_open_at = datetime.datetime.fromisoformat(next_open)
    now_at = datetime.datetime.now(next_open_at.tzinfo)
    return max(0, (next_open_at - now_at).total_seconds())


def poll_seconds_for_result(result, strategy_config, supervisor_config, clock):
    if result.get("status") == "market_closed_sleeping":
        fallback = supervisor_config.get(
            "default_market_closed_poll_seconds",
            strategy_config.get("market_closed_poll_seconds", 900),
        )
        until_open = seconds_until_next_open(clock)
        if until_open is None:
            return fallback
        return min(fallback, max(5, until_open + 2))

    if result.get("status") == "temporary_error":
        return strategy_config.get(
            "error_poll_seconds",
            supervisor_config.get("default_error_poll_seconds", 30),
        )

    return strategy_config.get(
        "poll_seconds", supervisor_config.get("default_poll_seconds", 30)
    )


def run_watcher(client, watcher, supervisor_config, clock):
    started = time.monotonic()
    strategy_config = dict(watcher.get("config_defaults", {}))
    if watcher.get("config_path"):
        strategy_config.update(load_json(watcher["config_path"], {}))
    strategy_config.setdefault("symbol", watcher["symbol"])
    state = load_json(watcher["state_path"], {})
    symbol = strategy_config.get("symbol", watcher["symbol"])

    try:
        result = run_once(client, strategy_config, state, clock=clock)
        save_json(watcher["state_path"], state)
        watcher["failures"] = 0
    except Exception as exc:
        watcher["failures"] += 1
        result = {
            "status": "temporary_error",
            "symbol": symbol,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "retrying": True,
        }

    elapsed = round(time.monotonic() - started, 3)
    next_delay = poll_seconds_for_result(result, strategy_config, supervisor_config, clock)
    watcher["next_run_at"] = time.monotonic() + next_delay

    log_record = {
        "timestamp": iso_now(),
        "symbol": symbol,
        "duration_seconds": elapsed,
        "failures": watcher["failures"],
        "next_run_seconds": next_delay,
        "result": result,
    }
    append_jsonl(watcher["log_path"], log_record)
    return log_record


def list_watchers(watchers):
    for watcher in watchers:
        enabled = "enabled" if watcher["enabled"] else "disabled"
        config_name = (
            watcher["config_path"].name if watcher.get("config_path") else "<defaults>"
        )
        print(
            f"{watcher['symbol']}: {enabled}, "
            f"group={watcher['group']}, "
            f"config={config_name}, "
            f"state={watcher['state_path'].name}, "
            f"log={watcher['log_path'].name}"
        )


def due_watchers(watchers):
    now = time.monotonic()
    return [
        watcher
        for watcher in watchers
        if watcher["enabled"] and watcher["next_run_at"] <= now
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_WATCHERS_PATH)
    parser.add_argument("--once", action="store_true", help="Run one pass and exit.")
    parser.add_argument("--list", action="store_true", help="List configured watchers.")
    args = parser.parse_args()

    project_root, supervisor_config, watchers = load_supervisor_config(args.config)
    os.chdir(project_root)

    if args.list:
        list_watchers(watchers)
        return 0

    load_env()
    client = AlpacaClient()
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    max_concurrency = max(1, int(supervisor_config.get("max_concurrency", 3)))
    scheduler_sleep = max(1, int(supervisor_config.get("scheduler_sleep_seconds", 2)))

    for index, watcher in enumerate(watchers):
        watcher["next_run_at"] = 0.0 if args.once else time.monotonic() + index

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        while not stop_requested:
            due = due_watchers(watchers)
            if not due:
                if args.once:
                    break
                time.sleep(scheduler_sleep)
                continue

            try:
                clock = client.clock()
            except Exception:
                clock = {
                    "is_open": False,
                    "timestamp": iso_now(),
                    "next_open": None,
                }

            futures = [
                executor.submit(run_watcher, client, watcher, supervisor_config, clock)
                for watcher in due
            ]
            for future in concurrent.futures.as_completed(futures):
                record = future.result()
                result = record["result"]
                print(
                    json.dumps(
                        {
                            "timestamp": record["timestamp"],
                            "symbol": record["symbol"],
                            "status": result.get("status"),
                            "next_run_seconds": record["next_run_seconds"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

            if args.once:
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
