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
    save_json,
)
from traderbot.core_strategy_engine.strategies import (
    DEFAULT_STRATEGY_TYPE,
    resolve_strategy,
)


DEFAULT_WATCHERS_PATH = "config/watchers.json"
STRATEGY_CONFIG_DIR = Path("traderbot/core_strategy_engine/strategies/configs")
RUNTIME_STATE_DIR = Path("runtime/state")
RUNTIME_LOG_DIR = Path("runtime/logs")


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


def relative_config_path(project_root, path):
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def move_file_if_needed(source, destination):
    if source == destination:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    if destination.exists():
        return
    source.replace(destination)


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


def result_has_open_position(result):
    if result.get("position_qty", 0) > 0:
        return True
    reconciliation = result.get("reconciliation") or {}
    if reconciliation.get("position_qty", 0) > 0:
        return True
    return result.get("status") in {
        "managed",
        "reentry_filled_waiting_for_management",
    }


def managed_strategy_config(symbol, config):
    managed = dict(config)
    managed["symbol"] = symbol
    managed["strategy_type"] = "managed_dynamic_reentry"
    managed["strategy_name"] = f"Managed dynamic reentry for {symbol}"
    managed["dynamic_entry_enabled"] = False
    managed["dynamic_reentry_enabled"] = True
    managed.setdefault("dynamic_entry_notional", 5000)
    managed.setdefault("dynamic_market_filter_ignored_notional", 2500)
    managed.setdefault("min_cash_balance_percent", 20)
    return managed


def promote_new_watcher_if_bought(project_root, watchers_config_path, supervisor_config, watcher, strategy_config, result):
    if watcher.get("group") != "new" or not result_has_open_position(result):
        return None

    symbol = watcher["symbol"]
    lower_symbol = symbol.lower()
    managed_watchers = supervisor_config.setdefault("managed_watchers", [])
    if any(item.get("symbol") == symbol for item in managed_watchers):
        return None

    config_path = project_root / STRATEGY_CONFIG_DIR / f"{lower_symbol}_strategy_config.json"
    state_path = project_root / RUNTIME_STATE_DIR / f"{lower_symbol}_strategy_state.json"
    log_path = project_root / RUNTIME_LOG_DIR / f"{lower_symbol}_watcher.jsonl"

    save_json(config_path, managed_strategy_config(symbol, strategy_config))
    move_file_if_needed(watcher["state_path"], state_path)
    move_file_if_needed(watcher["log_path"], log_path)

    managed_entry = {
        "symbol": symbol,
        "config": relative_config_path(project_root, config_path),
        "state": relative_config_path(project_root, state_path),
        "log": relative_config_path(project_root, log_path),
    }
    managed_watchers.append(managed_entry)
    supervisor_config["new_watchers"] = [
        item for item in supervisor_config.get("new_watchers", [])
        if item.get("symbol") != symbol
    ]
    save_json(watchers_config_path, supervisor_config)

    watcher["group"] = "managed"
    watcher["config_path"] = config_path
    watcher["config_defaults"] = {}
    watcher["state_path"] = state_path
    watcher["log_path"] = log_path

    return managed_entry


def run_watcher(client, watcher, supervisor_config, clock):
    started = time.monotonic()
    strategy_config = dict(watcher.get("config_defaults", {}))
    if watcher.get("config_path"):
        strategy_config.update(load_json(watcher["config_path"], {}))
    strategy_config.setdefault("symbol", watcher["symbol"])
    strategy_config.setdefault(
        "strategy_type",
        "new_candidate_dynamic_entry"
        if watcher.get("group") == "new"
        else DEFAULT_STRATEGY_TYPE,
    )
    state = load_json(watcher["state_path"], {})
    symbol = strategy_config.get("symbol", watcher["symbol"])

    try:
        strategy_cls = resolve_strategy(strategy_config.get("strategy_type"))
        strategy = strategy_cls(client, strategy_config, state, clock=clock)
        result = strategy.run_once()
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
    return {**log_record, "strategy_config": strategy_config}


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

    watchers_config_path = Path(args.config).resolve()
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

            futures = {
                executor.submit(run_watcher, client, watcher, supervisor_config, clock): watcher
                for watcher in due
            }
            for future in concurrent.futures.as_completed(futures):
                watcher = futures[future]
                record = future.result()
                result = record["result"]
                promoted = promote_new_watcher_if_bought(
                    project_root,
                    watchers_config_path,
                    supervisor_config,
                    watcher,
                    record["strategy_config"],
                    result,
                )
                print(
                    json.dumps(
                        {
                            "timestamp": record["timestamp"],
                            "symbol": record["symbol"],
                            "strategy_type": record["strategy_config"].get("strategy_type"),
                            "status": result.get("status"),
                            "next_run_seconds": record["next_run_seconds"],
                            "promoted_to_managed": bool(promoted),
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
