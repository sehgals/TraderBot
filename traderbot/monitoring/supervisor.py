import argparse
import concurrent.futures
import datetime
import json
import os
import signal
import time
import traceback
from contextlib import nullcontext
from zoneinfo import ZoneInfo
from pathlib import Path

from traderbot.core_strategy_engine.engine import (
    AlpacaClient,
    calculate_indicators,
    completed_market_bars,
    iso_utc,
    load_env,
    load_json,
    save_json,
    refresh_dynamic_plan_only,
    timeframe_minutes,
    parse_alpaca_time,
    record_exit_intent,
)
from traderbot.core_strategy_engine.strategies import (
    DEFAULT_STRATEGY_TYPE,
    resolve_strategy,
)
from traderbot.broker.execution_gateway import ExecutionGateway
from traderbot.portfolio_allocator import allocate_candidates, classify_exposure_regime
from traderbot.shadow_deployment import apply_promotion_stage, update_shadow_ledger
from traderbot.data.tastytrade_collector import collect_tastytrade_comparison


DEFAULT_WATCHERS_PATH = "config/watchers.json"
STRATEGY_CONFIG_DIR = Path("traderbot/core_strategy_engine/strategies/configs")
RUNTIME_STATE_DIR = Path("runtime/state")
RUNTIME_LOG_DIR = Path("runtime/logs")
MARGIN_REDUCTION_STATE = RUNTIME_STATE_DIR / "margin_reduction_state.json"
MARGIN_REDUCTION_LOG = RUNTIME_LOG_DIR / "margin_reduction.jsonl"
POSITION_HEALTH_ALERT_LOG = RUNTIME_LOG_DIR / "position_health_alerts.jsonl"
PORTFOLIO_ALLOCATION_STATE = RUNTIME_STATE_DIR / "portfolio_allocation_state.json"
PORTFOLIO_ALLOCATION_LOG = RUNTIME_LOG_DIR / "portfolio_allocations.jsonl"
TASTYTRADE_COLLECTION_LOG = RUNTIME_LOG_DIR / "tastytrade_collection.jsonl"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso_now():
    return utc_now().isoformat()


def record_tastytrade_collection(project_root, future):
    try:
        result = future.result()
    except Exception as exc:
        result = {
            "status": "tastytrade_collection_error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    append_jsonl(
        project_root / TASTYTRADE_COLLECTION_LOG,
        {"timestamp": iso_now(), "result": result},
    )
    print(json.dumps({"timestamp": iso_now(), **result}, sort_keys=True), flush=True)


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


class WatcherListReloader:
    """Reload watcher definitions between batches, retaining runtime scheduling."""

    def __init__(self, path):
        self.path = Path(path)
        self.fingerprint = self._fingerprint()

    def _fingerprint(self):
        stat = self.path.stat()
        return stat.st_mtime_ns, stat.st_size

    def reload(self, config, watchers):
        try:
            fingerprint = self._fingerprint()
            if fingerprint == self.fingerprint:
                return config, watchers
            _, incoming, prepared = load_supervisor_config(self.path)
            if self._fingerprint() != fingerprint:
                raise ValueError("Watcher configuration changed while being read")
            previous = {item["symbol"]: item for item in watchers}
            runtime_keys = {"next_run_at", "failures"}
            for index, item in enumerate(prepared):
                old = previous.get(item["symbol"])
                if old and all(old.get(key) == value for key, value in item.items()
                               if key not in runtime_keys):
                    prepared[index] = old
            # Only watcher definitions/defaults reload; execution controls remain
            # the startup settings until the service is restarted.
            updated = dict(config)
            for key in ("watchers", "managed_watchers", "new_watchers",
                        "managed_watcher_defaults", "new_watcher_defaults"):
                updated.pop(key, None)
                if key in incoming:
                    updated[key] = incoming[key]
            self.fingerprint = fingerprint
            print(json.dumps({"timestamp": iso_now(), "status": "watcher_list_reloaded",
                              "symbols": [item["symbol"] for item in prepared]}), flush=True)
            return updated, prepared
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            print(json.dumps({"timestamp": iso_now(), "status": "watcher_list_reload_failed",
                              "error": str(exc)}), flush=True)
            return config, watchers


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
    managed["reentry_enabled"] = True
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


def run_watcher(client, watcher, supervisor_config, clock, config_overrides=None):
    started = time.monotonic()
    strategy_config = dict(watcher.get("config_defaults", {}))
    if watcher.get("config_path"):
        strategy_config.update(load_json(watcher["config_path"], {}))
    strategy_config["position_health"] = {
        **(supervisor_config.get("position_health") or {}),
        **(strategy_config.get("position_health") or {}),
    }
    strategy_config["winner_management"] = {
        **(supervisor_config.get("winner_management") or {}),
        **(strategy_config.get("winner_management") or {}),
    }
    strategy_config["entry_filters"] = {
        **(supervisor_config.get("entry_filters") or {}),
        **(strategy_config.get("entry_filters") or {}),
    }
    allocator_settings = supervisor_config.get("portfolio_allocator") or {}
    if allocator_settings.get("enabled", False) and watcher.get("group") == "new":
        strategy_config["portfolio_allocation_observed"] = True
        promotion_stage = allocator_settings.get(
            "promotion_stage",
            "shadow" if allocator_settings.get("shadow_mode", True) else "full",
        )
        if promotion_stage != "shadow":
            strategy_config["portfolio_allocation_required"] = True
    strategy_config.update(config_overrides or {})
    if watcher.get("group") == "managed":
        managed_reentry = supervisor_config.get("managed_reentry") or {}
        strategy_config["reentry_observe_only"] = managed_reentry.get(
            "observe_only",
            strategy_config.get("reentry_observe_only", False),
        )
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


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def allocation_inputs(client, records, supervisor_config, watchers, project_root):
    settings = supervisor_config.get("portfolio_allocator") or {}
    account = client.account()
    equity = max(0.0, _float(account.get("equity")))
    positions = client.positions()
    incumbent_order_ids = {
        str(record.get("result", {}).get("reentry_order_id"))
        for record in records
        if record.get("result", {}).get("status") == "dynamic_reentry_order_submitted"
        and record.get("result", {}).get("reentry_order_id")
    }
    open_buy_orders = [
        order for order in client.open_orders()
        if order.get("side") == "buy" and str(order.get("id")) not in incumbent_order_ids
    ]
    benchmark_by_symbol = (
        (supervisor_config.get("position_health") or {}).get("benchmark_by_symbol") or {}
    )
    factor_by_symbol = settings.get("correlated_factor_by_symbol") or {}
    symbol_exposure = {}
    sector_exposure = {}
    factor_exposure = {}
    aggregate_risk = 0.0
    state_by_symbol = {
        watcher["symbol"]: load_json(watcher["state_path"], {}) for watcher in watchers
    }
    for position in positions:
        symbol = str(position.get("symbol") or "").upper()
        notional = abs(_float(position.get("market_value"))) or abs(
            _float(position.get("qty")) * _float(position.get("current_price"))
        )
        sector = str(benchmark_by_symbol.get(symbol) or "Unmapped")
        symbol_exposure[symbol] = symbol_exposure.get(symbol, 0) + notional
        sector_exposure[sector] = sector_exposure.get(sector, 0) + notional
        factor = str(factor_by_symbol.get(symbol) or sector)
        factor_exposure[factor] = factor_exposure.get(factor, 0) + notional
        state = state_by_symbol.get(symbol, {})
        price = _float(position.get("current_price"))
        stop = _float(state.get("active_stop_price"))
        qty = abs(_float(position.get("qty")))
        aggregate_risk += qty * max(0.0, price - stop) if stop > 0 else notional * 0.06
    for order in open_buy_orders:
        symbol = str(order.get("symbol") or "").upper()
        notional = _float(order.get("qty")) * _float(order.get("limit_price"))
        sector = str(benchmark_by_symbol.get(symbol) or "Unmapped")
        symbol_exposure[symbol] = symbol_exposure.get(symbol, 0) + notional
        sector_exposure[sector] = sector_exposure.get(sector, 0) + notional
        factor = str(factor_by_symbol.get(symbol) or sector)
        factor_exposure[factor] = factor_exposure.get(factor, 0) + notional
        aggregate_risk += notional * 0.06

    today = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    allocation_state = load_json(project_root / PORTFOLIO_ALLOCATION_STATE, {})
    if allocation_state.get("date") != today:
        allocation_state = {"date": today, "entries": 0, "turnover": 0.0}
    candidates = []
    for record in records:
        config = record["strategy_config"]
        if not (
            config.get("portfolio_allocation_observed")
            or config.get("portfolio_allocation_required")
        ):
            continue
        plan = (record.get("result") or {}).get("dynamic_plan") or {}
        limit_price = _float(plan.get("limit_price"))
        risk_per_share = _float(plan.get("risk_per_share"))
        target_notional = _float(config.get("dynamic_entry_notional"), 5000)
        symbol_cap = equity * _float(
            settings.get(
                "maximum_symbol_notional_percent",
                config.get("max_symbol_notional_percent", 15),
            )
        ) / 100
        notional = min(target_notional, symbol_cap) if symbol_cap > 0 else target_notional
        candidates.append({
            "symbol": record["symbol"],
            "score": plan.get("setup_score"),
            "expected_reward_risk": plan.get("expected_reward_risk"),
            "notional": notional,
            "risk_dollars": notional * risk_per_share / limit_price if limit_price > 0 else 0,
            "sector": benchmark_by_symbol.get(record["symbol"], "Unmapped"),
            "factor": factor_by_symbol.get(
                record["symbol"], benchmark_by_symbol.get(record["symbol"], "Unmapped")
            ),
            "hard_safety_passed": not (plan.get("hard_blockers") or []),
            "candidate_as_of": plan.get("last_bar_time"),
            "model_id": plan.get("model_id") or plan.get("classified_model_id"),
            "fallback_only": plan.get("fallback_only", False),
            "limit_price": limit_price,
            "last_price": plan.get("last_price"),
        })
    market_regime = "defensive"
    regime_data_status = "unavailable_conservative_default"
    regime_as_of = None
    candidate_times = [
        item["candidate_as_of"] for item in candidates if item.get("candidate_as_of")
    ]
    if candidate_times:
        try:
            decision_at = datetime.datetime.fromisoformat(
                max(candidate_times).replace("Z", "+00:00")
            )
            completed_at = decision_at + datetime.timedelta(minutes=5)
            start = iso_utc(decision_at - datetime.timedelta(days=7))
            end = iso_utc(completed_at)
            raw_market = client.stock_bars("QQQ", start, end, "5Min")
            market_bars = calculate_indicators(
                completed_market_bars(raw_market, "5Min", now=completed_at)
            )
            market_regime = classify_exposure_regime(market_bars, decision_at)
            regime_as_of = market_bars[-1]["t"].isoformat() if market_bars else None
            regime_data_status = "point_in_time_completed_5min_bars"
        except Exception as exc:
            regime_data_status = f"unavailable_conservative_default:{type(exc).__name__}"
    gross_exposure = sum(symbol_exposure.values())
    portfolio = {
        "as_of": max(candidate_times) if candidate_times else None,
        "equity": equity,
        "cash": _float(account.get("cash")),
        "position_count": len({
            str(item.get("symbol") or "").upper()
            for item in positions + open_buy_orders if item.get("symbol")
        }),
        "aggregate_open_risk": aggregate_risk,
        "daily_entries": allocation_state["entries"],
        "daily_turnover": allocation_state["turnover"],
        "symbol_exposure": symbol_exposure,
        "sector_exposure": sector_exposure,
        "factor_exposure": factor_exposure,
        "gross_exposure": gross_exposure,
        "regime": market_regime,
        "regime_as_of": regime_as_of,
        "regime_data_status": regime_data_status,
    }
    return candidates, portfolio, allocation_state


def run_portfolio_allocator(client, records, supervisor_config, watchers, project_root, clock):
    settings = supervisor_config.get("portfolio_allocator") or {}
    promotion_stage = settings.get(
        "promotion_stage", "shadow" if settings.get("shadow_mode", True) else "full"
    )
    if not settings.get("enabled", False):
        return None, []
    if not any(
        record.get("strategy_config", {}).get("portfolio_allocation_observed")
        or record.get("strategy_config", {}).get("portfolio_allocation_required")
        for record in records
    ):
        return None, []
    # Refresh stale/high-quality candidates on one completed-bar cutoff without submitting orders.
    refreshed_records = []
    refresh_rejections = []
    snapshot_now = datetime.datetime.fromisoformat(str(clock["timestamp"]).replace("Z", "+00:00")) if clock.get("timestamp") else datetime.datetime.now(datetime.timezone.utc)
    watcher_by_symbol = {watcher["symbol"]: watcher for watcher in watchers}
    for record in records:
        cfg = record.get("strategy_config", {})
        plan = (record.get("result") or {}).get("dynamic_plan") or {}
        if not (cfg.get("portfolio_allocation_observed") or cfg.get("portfolio_allocation_required")):
            refreshed_records.append(record)
            continue
        minutes = timeframe_minutes(cfg.get("dynamic_timeframe", "5Min"))
        cutoff = snapshot_now.replace(second=0, microsecond=0) - datetime.timedelta(minutes=snapshot_now.minute % minutes)
        expected = cutoff - datetime.timedelta(minutes=minutes)
        if parse_alpaca_time(plan.get("last_bar_time")) != expected and _float(plan.get("setup_score")) >= _float(cfg.get("minimum_entry_setup_score"), 80):
            watcher = watcher_by_symbol.get(record["symbol"])
            if watcher:
                try:
                    with client.symbol_transaction(record["symbol"]) if hasattr(client, "symbol_transaction") else nullcontext():
                        state_data = load_json(watcher["state_path"], {})
                        plan = refresh_dynamic_plan_only(client, {**cfg, "entry_snapshot_end": cutoff.isoformat()}, state_data) or {}
                        save_json(watcher["state_path"], state_data)
                    record = {**record, "result": {**record.get("result", {}), "dynamic_plan": plan}}
                except Exception:
                    refresh_rejections.append({"symbol":record["symbol"],"reason":"candidate_refresh_failed"})
                    continue
        if parse_alpaca_time(plan.get("last_bar_time")) != expected:
            refresh_rejections.append({"symbol":record["symbol"],"reason":"candidate_bar_mismatch_or_stale"})
            continue
        refreshed_records.append(record)
    records = refreshed_records
    candidates, portfolio, state = allocation_inputs(
        client, records, supervisor_config, watchers, project_root
    )
    bar_times = sorted({
        item.get("candidate_as_of") for item in candidates if item.get("candidate_as_of")
    })
    if len(bar_times) != 1 or any(
        not item.get("candidate_as_of") for item in candidates
    ):
        decision = {
            "as_of": iso_now(), "controls": settings, "ranked": [],
            "selected": [], "rejected": [], "candidate_bar_times": bar_times,
            "refresh_rejections": refresh_rejections,
            "status": "incomplete_candidate_snapshot",
            "shadow_mode": promotion_stage == "shadow",
            "promotion_stage": promotion_stage, "executions": [],
        }
        append_jsonl(
            project_root / PORTFOLIO_ALLOCATION_LOG,
            {"timestamp": iso_now(), "decision": decision, "portfolio": portfolio},
        )
        return decision, []
    bar_key = bar_times[0]
    if state.get("last_allocated_bar") == bar_key:
        return {
            "as_of": iso_now(), "controls": settings, "ranked": [],
            "selected": [], "rejected": [], "candidate_bar_times": bar_times,
            "status": "candidate_snapshot_already_allocated",
            "shadow_mode": promotion_stage == "shadow",
            "promotion_stage": promotion_stage, "executions": [],
        }, []
    decision = allocate_candidates(candidates, portfolio, settings)
    decision = apply_promotion_stage(decision, settings)
    decision["candidate_bar_times"] = bar_times
    decision["status"] = "allocation_completed"
    decision["refresh_rejections"] = refresh_rejections
    execution_records = []
    if promotion_stage != "shadow":
        watcher_by_symbol = {watcher["symbol"]: watcher for watcher in watchers}
        for selected in decision["selected"]:
            watcher = watcher_by_symbol.get(selected["symbol"])
            if not watcher or not selected.get("candidate_as_of"):
                continue
            record = run_watcher(
                client, watcher, supervisor_config, clock,
                config_overrides={
                    "portfolio_allocation_authorized": True,
                    "portfolio_allocation_as_of": selected["candidate_as_of"],
                    "dynamic_entry_notional": min(
                        selected["notional"],
                        float(settings.get("small_notional_dollars", 1000)),
                    ) if promotion_stage == "small_notional" else selected["notional"],
                },
            )
            execution_records.append(record)
            if record["result"].get("status") == "dynamic_reentry_order_submitted":
                state["entries"] += 1
                state["turnover"] = round(state["turnover"] + selected["notional"], 2)
                stage_entries = state.setdefault("stage_live_entries", {})
                stage_entries[promotion_stage] = stage_entries.get(promotion_stage, 0) + 1
                save_json(project_root / PORTFOLIO_ALLOCATION_STATE, state)
    decision["shadow_mode"] = promotion_stage == "shadow"
    decision["promotion_stage"] = promotion_stage
    decision["incumbent_executions"] = [
        {"symbol": record["symbol"], "status": record["result"].get("status")}
        for record in records
        if record["result"].get("status") == "dynamic_reentry_order_submitted"
    ]
    decision["shadow_summary"] = update_shadow_ledger(state, decision, settings)
    decision["executions"] = [
        {"symbol": record["symbol"], "status": record["result"].get("status")}
        for record in execution_records
    ]
    append_jsonl(
        project_root / PORTFOLIO_ALLOCATION_LOG,
        {"timestamp": iso_now(), "decision": decision, "portfolio": portfolio},
    )
    save_json(project_root / PORTFOLIO_ALLOCATION_STATE, {
        **state, "last_allocated_bar": bar_key, "last_decision": decision,
    })
    return decision, execution_records


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


def record_position_health_alert(project_root, watcher, result):
    health = result.get("position_health") or {}
    state = health.get("state")
    reasons = health.get("reasons") or []
    alertable = state in ("Critical", "Unprotected") or any(
        str(reason).startswith("health_context_error") for reason in reasons
    )
    if not alertable:
        watcher.pop("last_health_alert_key", None)
        return None

    alert_key = (
        state,
        health.get("bar_id"),
        health.get("recommended_action"),
        tuple(sorted(reasons)),
    )
    if watcher.get("last_health_alert_key") == alert_key:
        return None
    watcher["last_health_alert_key"] = alert_key
    record = {
        "timestamp": iso_now(),
        "symbol": watcher["symbol"],
        "state": state,
        "score": health.get("score"),
        "recommended_action": health.get("recommended_action"),
        "bar_id": health.get("bar_id"),
        "as_of": health.get("as_of"),
        "reasons": reasons,
    }
    append_jsonl(project_root / POSITION_HEALTH_ALERT_LOG, record)
    return record


def position_margin_rank(position):
    try:
        total_plpc = float(position.get("unrealized_plpc", 0))
    except (TypeError, ValueError):
        total_plpc = 0.0
    try:
        day_plpc = float(position.get("unrealized_intraday_plpc", 0))
    except (TypeError, ValueError):
        day_plpc = 0.0
    return (total_plpc, day_plpc, position.get("symbol") or "")


def run_margin_reducer(client, supervisor_config, project_root, clock):
    config = supervisor_config.get("auto_margin_reduction", {})
    if not config.get("enabled", False):
        return {"status": "disabled"}

    account = client.account()
    cash = float(account.get("cash", 0) or 0)
    margin_used = max(0.0, -cash)
    target = max(0.0, float(config.get("target_margin_dollars", 0)))
    if margin_used <= target:
        return {"status": "no_margin", "margin_used": margin_used, "target": target}

    open_orders = client.open_orders()
    canceled_buy_order_ids = []
    for order in open_orders:
        if order.get("side") == "buy":
            client.cancel_order(order["id"], symbol=order.get("symbol")) if hasattr(
                client, "symbol_transaction"
            ) else client.cancel_order(order["id"])
            canceled_buy_order_ids.append(order["id"])

    result = {
        "margin_used": margin_used,
        "target": target,
        "canceled_buy_order_ids": canceled_buy_order_ids,
    }
    if not clock.get("is_open"):
        return {**result, "status": "market_closed_margin_reduction_deferred"}

    today = datetime.datetime.now().astimezone().date().isoformat()
    state_path = project_root / MARGIN_REDUCTION_STATE
    state = load_json(state_path, {})
    if state.get("date") != today:
        state = {"date": today, "submitted_notional": 0.0}

    daily_cap = max(0.0, float(config.get("max_daily_liquidation_dollars", 5000)))
    submitted = float(state.get("submitted_notional", 0) or 0)
    remaining_daily = max(0.0, daily_cap - submitted)
    if remaining_daily <= 0:
        return {**result, "status": "daily_margin_reduction_cap_reached", "daily_cap": daily_cap}

    if any(
        order.get("side") == "sell"
        and str(order.get("client_order_id", "")).startswith("margin-reducer-")
        for order in open_orders
    ):
        return {**result, "status": "margin_reduction_order_pending"}

    excluded = set(config.get("excluded_symbols", []))
    positions = [
        position for position in client.positions()
        if position.get("symbol") not in excluded and float(position.get("qty", 0) or 0) > 0
    ]
    if not positions:
        return {**result, "status": "no_position_available_for_margin_reduction"}
    position = min(positions, key=position_margin_rank)
    symbol = position["symbol"]
    price = float(position.get("current_price", 0) or 0)
    held_qty = int(float(position.get("qty", 0) or 0))
    max_order = max(0.0, float(config.get("max_order_dollars", 1000)))
    order_budget = min(margin_used - target, remaining_daily, max_order)
    qty = min(held_qty, int(order_budget // price)) if price > 0 else 0
    if qty <= 0:
        return {**result, "status": "margin_reduction_budget_below_share_price", "symbol": symbol}

    transaction = (
        client.symbol_transaction(symbol)
        if hasattr(client, "symbol_transaction")
        else nullcontext()
    )
    with transaction:
        canceled_sell_order_ids = []
        for order in open_orders:
            if order.get("symbol") == symbol and order.get("side") == "sell":
                client.cancel_order(order["id"], symbol=symbol) if hasattr(
                    client, "symbol_transaction"
                ) else client.cancel_order(order["id"])
                canceled_sell_order_ids.append(order["id"])

        estimated_notional = round(qty * price, 2)
        order = client.submit_order(
            {
                "symbol": symbol,
                "qty": str(qty),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": (
                    f"margin-reducer-{today.replace('-', '')}-{symbol.lower()}-{int(submitted)}"
                ),
            }
        )
        watcher = next(
            (
                item
                for item in supervisor_config.get(
                    "managed_watchers", supervisor_config.get("watchers", [])
                )
                if item.get("symbol") == symbol and item.get("state")
            ),
            None,
        )
        if watcher:
            watcher_state_path = resolve_path(project_root, watcher["state"])
            watcher_state = load_json(watcher_state_path, {})
            record_exit_intent(watcher_state, order, "portfolio_margin_reduction")
            save_json(watcher_state_path, watcher_state)
    state["submitted_notional"] = round(submitted + estimated_notional, 2)
    state["last_order_id"] = order.get("id")
    state["last_symbol"] = symbol
    save_json(state_path, state)
    return {
        **result,
        "status": "margin_reduction_order_submitted",
        "symbol": symbol,
        "qty": qty,
        "estimated_notional": estimated_notional,
        "total_gain_loss_percent": float(position.get("unrealized_plpc", 0) or 0) * 100,
        "order_id": order.get("id"),
        "canceled_sell_order_ids": canceled_sell_order_ids,
        "daily_submitted_notional": state["submitted_notional"],
        "daily_cap": daily_cap,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_WATCHERS_PATH)
    parser.add_argument("--once", action="store_true", help="Run one pass and exit.")
    parser.add_argument("--list", action="store_true", help="List configured watchers.")
    args = parser.parse_args()

    watchers_config_path = Path(args.config).resolve()
    watcher_reloader = WatcherListReloader(watchers_config_path)
    project_root, supervisor_config, watchers = load_supervisor_config(args.config)
    os.chdir(project_root)

    if args.list:
        list_watchers(watchers)
        return 0

    load_env()
    client = ExecutionGateway(AlpacaClient())
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    max_concurrency = max(1, int(supervisor_config.get("max_concurrency", 3)))
    scheduler_sleep = max(1, int(supervisor_config.get("scheduler_sleep_seconds", 2)))
    margin_reduction_interval = max(
        30,
        int(supervisor_config.get("auto_margin_reduction", {}).get("poll_seconds", 300)),
    )
    next_margin_reduction_at = 0.0
    tastytrade_settings = supervisor_config.get("tastytrade_comparison") or {}
    tastytrade_interval = max(60, int(tastytrade_settings.get("poll_seconds", 300)))
    next_tastytrade_collection_at = 0.0
    tastytrade_future = None

    for index, watcher in enumerate(watchers):
        watcher["next_run_at"] = 0.0 if args.once else time.monotonic() + index

    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as collector_executor,
    ):
        while not stop_requested:
            if tastytrade_future is not None and tastytrade_future.done():
                record_tastytrade_collection(project_root, tastytrade_future)
                tastytrade_future = None
            if not args.once:
                supervisor_config, watchers = watcher_reloader.reload(supervisor_config, watchers)
            due = due_watchers(watchers)
            allocator_enabled = (
                supervisor_config.get("portfolio_allocator") or {}
            ).get("enabled", False)
            if allocator_enabled and any(item.get("group") == "new" for item in due):
                due_symbols = {item["symbol"] for item in due}
                due.extend(
                    item for item in watchers
                    if item.get("group") == "new"
                    and item.get("enabled")
                    and item["symbol"] not in due_symbols
                )
            collector_due = (
                tastytrade_settings.get("enabled", False)
                and tastytrade_future is None
                and time.monotonic() >= next_tastytrade_collection_at
            )
            if not due and not collector_due:
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

            if collector_due:
                require_open = tastytrade_settings.get("require_market_open", True)
                if require_open and not clock.get("is_open"):
                    result = {
                        "status": "tastytrade_collection_skipped",
                        "reason": "market_closed",
                    }
                    append_jsonl(
                        project_root / TASTYTRADE_COLLECTION_LOG,
                        {"timestamp": iso_now(), "result": result},
                    )
                else:
                    collection_symbols = [
                        watcher["symbol"] for watcher in watchers if watcher.get("enabled")
                    ]
                    observed_at = datetime.datetime.fromisoformat(
                        str(clock.get("timestamp") or iso_now()).replace("Z", "+00:00")
                    )
                    tastytrade_future = collector_executor.submit(
                        collect_tastytrade_comparison,
                        tastytrade_settings,
                        collection_symbols,
                        project_root,
                        observed_at,
                    )
                next_tastytrade_collection_at = time.monotonic() + tastytrade_interval

            if not due:
                if args.once:
                    if tastytrade_future is not None:
                        record_tastytrade_collection(project_root, tastytrade_future)
                        tastytrade_future = None
                    break
                time.sleep(scheduler_sleep)
                continue

            if time.monotonic() >= next_margin_reduction_at:
                try:
                    margin_result = run_margin_reducer(
                        client, supervisor_config, project_root, clock
                    )
                except Exception as exc:
                    margin_result = {
                        "status": "margin_reduction_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                append_jsonl(
                    project_root / MARGIN_REDUCTION_LOG,
                    {"timestamp": iso_now(), "result": margin_result},
                )
                print(json.dumps({"timestamp": iso_now(), **margin_result}, sort_keys=True), flush=True)
                next_margin_reduction_at = time.monotonic() + margin_reduction_interval

            futures = {
                executor.submit(run_watcher, client, watcher, supervisor_config, clock): watcher
                for watcher in due
            }
            batch_records = []
            for future in concurrent.futures.as_completed(futures):
                watcher = futures[future]
                record = future.result()
                batch_records.append(record)
                result = record["result"]
                record_position_health_alert(project_root, watcher, result)
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

            try:
                allocation, execution_records = run_portfolio_allocator(
                    client,
                    batch_records,
                    supervisor_config,
                    watchers,
                    project_root,
                    clock,
                )
                if allocation is not None:
                    print(
                        json.dumps(
                            {
                                "timestamp": iso_now(),
                                "status": "portfolio_allocation_completed",
                                "candidates": len(allocation["ranked"]),
                                "selected": [
                                    item["symbol"] for item in allocation["selected"]
                                ],
                                "shadow_mode": allocation["shadow_mode"],
                                "executions": allocation["executions"],
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    for record in execution_records:
                        result = record["result"]
                        watcher = next(
                            item for item in watchers
                            if item["symbol"] == record["symbol"]
                        )
                        promoted = promote_new_watcher_if_bought(
                            project_root,
                            watchers_config_path,
                            supervisor_config,
                            watcher,
                            record["strategy_config"],
                            result,
                        )
                        if promoted:
                            print(json.dumps({
                                "timestamp": iso_now(),
                                "symbol": record["symbol"],
                                "promoted_to_managed": True,
                            }, sort_keys=True), flush=True)
            except Exception as exc:
                append_jsonl(
                    project_root / PORTFOLIO_ALLOCATION_LOG,
                    {
                        "timestamp": iso_now(),
                        "status": "portfolio_allocation_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )

            if args.once:
                if tastytrade_future is not None:
                    record_tastytrade_collection(project_root, tastytrade_future)
                    tastytrade_future = None
                break

        if tastytrade_future is not None:
            record_tastytrade_collection(project_root, tastytrade_future)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
