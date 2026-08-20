import argparse
import datetime
import json
from collections import defaultdict
from pathlib import Path

from traderbot.core_strategy_engine.engine import (
    AlpacaClient,
    evaluate_flat_entry_eligibility,
    load_env,
    load_json,
    refresh_position_health,
    save_json,
)
from traderbot.monitoring.supervisor import load_supervisor_config


DEFAULT_WATCHERS_PATH = "config/watchers.json"
DEFAULT_REPORT_DIR = "runtime/reports/daily"
LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo
BUY_STATUSES = {
    "dynamic_reentry_order_submitted",
    "reentry_order_submitted",
}
BLOCKED_STATUSES = {
    "dynamic_entry_cash_reserve_blocked",
    "dynamic_entry_order_canceled_cash_reserve",
    "reentry_cash_reserve_blocked",
}


def parse_time(value):
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_utc(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def last_closed_trading_day(now=None):
    current = (now or datetime.datetime.now(LOCAL_TZ)).date()
    if current.weekday() >= 5:
        current -= datetime.timedelta(days=current.weekday() - 4)
    elif datetime.datetime.now(LOCAL_TZ).time() < datetime.time(16, 0):
        current -= datetime.timedelta(days=1)
    while current.weekday() >= 5:
        current -= datetime.timedelta(days=1)
    return current


def is_market_day(client, report_date):
    if client is None:
        return report_date.weekday() < 5
    calendar = client.calendar(report_date.isoformat(), report_date.isoformat())
    return bool(calendar)


def day_window(report_date):
    start = datetime.datetime.combine(report_date, datetime.time(0, 0), tzinfo=LOCAL_TZ)
    end = start + datetime.timedelta(days=1)
    return start, end


def iter_watcher_log_records(log_path, start_at, end_at):
    if not log_path.exists():
        return
    with log_path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = parse_time(record.get("timestamp"))
            if not timestamp:
                continue
            timestamp = timestamp.astimezone(LOCAL_TZ)
            if start_at <= timestamp < end_at:
                yield record


def configured_watchers(project_root, watchers_path):
    config = load_json(watchers_path, {})
    watchers = []
    watchers.extend(config.get("managed_watchers", config.get("watchers", [])))
    watchers.extend(config.get("new_watchers", []))
    for watcher in watchers:
        log_path = Path(watcher.get("log", ""))
        if not log_path.is_absolute():
            log_path = project_root / log_path
        state_path = Path(watcher.get("state", ""))
        if not state_path.is_absolute():
            state_path = project_root / state_path
        yield {**watcher, "log_path": log_path, "state_path": state_path}


def signal_strength(plan):
    if not plan:
        return {"signal_strength": "Unavailable", "signal_score": None, "signal_as_of": None}
    blockers = set(plan.get("blockers") or [])
    score = round(max(0, 13 - len(blockers)) / 13 * 100)
    if score >= 85:
        label = "Strong"
    elif score >= 65:
        label = "Moderate"
    elif score >= 40:
        label = "Weak"
    else:
        label = "Very Weak"
    return {
        "signal_strength": label,
        "signal_score": score,
        "signal_as_of": plan.get("last_bar_time"),
        "signal_status": plan.get("status"),
        "signal_blockers": sorted(blockers),
    }


def watcher_signal_strengths(project_root, watchers_path):
    strengths = {}
    for watcher in configured_watchers(project_root, watchers_path):
        state = load_json(watcher["state_path"], {})
        strengths[watcher.get("symbol")] = signal_strength(state.get("dynamic_entry_plan"))
    return strengths


def flat_managed_stock_evaluations(project_root, watchers_path, positions):
    """Describe the persisted entry decision for managed symbols with no position."""
    config = load_json(watchers_path, {})
    held_symbols = {
        position.get("symbol")
        for position in (positions or [])
        if as_float(position.get("qty")) != 0
    }
    evaluations = []
    for watcher in config.get("managed_watchers", config.get("watchers", [])):
        symbol = watcher.get("symbol")
        if not symbol or symbol in held_symbols:
            continue
        state_path = Path(watcher.get("state", ""))
        if not state_path.is_absolute():
            state_path = project_root / state_path
        state = load_json(state_path, {})
        plan = state.get("dynamic_entry_plan") or {}
        config_path = Path(watcher.get("config", ""))
        if not config_path.is_absolute():
            config_path = project_root / config_path
        strategy_config = load_json(config_path, {}) if watcher.get("config") else {}
        strategy_config["reentry_observe_only"] = (
            config.get("managed_reentry") or {}
        ).get("observe_only", strategy_config.get("reentry_observe_only", False))
        eligibility = evaluate_flat_entry_eligibility(
            strategy_config,
            state,
            plan if plan else None,
            mode="reentry",
        )
        qualification_config = {**strategy_config, "reentry_observe_only": False}
        reentry_qualification = evaluate_flat_entry_eligibility(
            qualification_config,
            state,
            plan if plan else None,
            mode="reentry",
        )
        evaluations.append(
            {
                "symbol": symbol,
                **signal_strength(plan),
                "entry_eligibility": eligibility,
                "reentry_qualified": reentry_qualification.get("eligible", False),
                "evaluation_status": plan.get("status") or "unavailable",
                "entry_mode": plan.get("mode"),
                "last_price": plan.get("last_price"),
                "next_signal_trigger": plan.get("next_signal_trigger"),
                "limit_price": plan.get("limit_price"),
                "price_action": plan.get("price_action"),
                "volume_ratio": plan.get("volume_ratio"),
            }
        )
    return sorted(evaluations, key=lambda item: item["symbol"])


def unavailable_position_health(reason="health_assessment_missing"):
    return {
        "position_health_state": "Unavailable",
        "position_health_score": None,
        "position_health_action": "freeze",
        "position_health_as_of": None,
        "position_health_reasons": [reason],
        "position_health_data_complete": False,
        "position_health_data_fresh": False,
    }


def report_position_health(assessment):
    if not assessment:
        return unavailable_position_health()
    return {
        "position_health_state": assessment.get("state") or "Unavailable",
        "position_health_score": assessment.get("score"),
        "position_health_action": assessment.get("recommended_action") or "freeze",
        "position_health_as_of": assessment.get("as_of"),
        "position_health_reasons": assessment.get("reasons") or [],
        "position_health_downside_score": assessment.get("downside_score"),
        "position_health_trend_score": assessment.get("trend_score"),
        "position_health_reward_risk_score": assessment.get("reward_risk_score"),
        "position_health_remaining_r": assessment.get("remaining_r"),
        "position_health_entry_return_percent": assessment.get("entry_return_percent"),
        "position_health_stop_price": assessment.get("stop_price"),
        "position_health_stop_qty": assessment.get("stop_qty"),
        "position_health_data_complete": assessment.get("data_complete", False),
        "position_health_data_fresh": assessment.get("data_fresh", False),
        "position_health_model_version": assessment.get("model_version"),
    }


def watcher_position_healths(project_root, watchers_path, client=None, positions=None):
    health = {}
    broker_positions = {
        position.get("symbol"): position for position in (positions or [])
    }
    supervisor_config = {}
    prepared_watchers = None
    if client is not None:
        _, supervisor_config, prepared_watchers = load_supervisor_config(watchers_path)
    watchers = prepared_watchers or configured_watchers(project_root, watchers_path)
    for watcher in watchers:
        state = load_json(watcher["state_path"], {})
        assessment = state.get("position_health")
        position = broker_positions.get(watcher.get("symbol"))
        if client is not None and position is not None:
            strategy_config = dict(watcher.get("config_defaults", {}))
            if watcher.get("config_path"):
                strategy_config.update(load_json(watcher["config_path"], {}))
            strategy_config["position_health"] = {
                **(supervisor_config.get("position_health") or {}),
                **(strategy_config.get("position_health") or {}),
            }
            strategy_config.setdefault("symbol", watcher.get("symbol"))
            assessment = refresh_position_health(
                client,
                strategy_config,
                state,
                position,
                force=True,
            )
        health[watcher.get("symbol")] = report_position_health(assessment)
    return health


def cash_blocked_detail(record, result):
    sizing = result.get("sizing") or {}
    plan = result.get("dynamic_plan") or {}
    return {
        "timestamp": record.get("timestamp"),
        "symbol": record.get("symbol") or result.get("symbol") or plan.get("symbol"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "mode": result.get("mode") or plan.get("mode"),
        "limit_price": result.get("limit_price") or plan.get("limit_price"),
        "target_notional": result.get("target_notional"),
        "requested_qty": sizing.get("requested_qty"),
        "available_notional": sizing.get("available_notional"),
        "cash": sizing.get("cash"),
        "min_cash_balance": sizing.get("min_cash_balance"),
    }


def watcher_activity(project_root, watchers_path, report_date):
    start_at, end_at = day_window(report_date)
    buys = []
    cash_blocked = []
    latest_status = {}

    for watcher in configured_watchers(project_root, watchers_path):
        for record in iter_watcher_log_records(watcher["log_path"], start_at, end_at):
            result = record.get("result") or {}
            symbol = record.get("symbol") or result.get("symbol") or watcher.get("symbol")
            status = result.get("status")
            latest_status[symbol] = {
                "timestamp": record.get("timestamp"),
                "status": status,
                "position_qty": result.get("position_qty"),
                "current_price": result.get("current_price"),
            }
            if status in BUY_STATUSES:
                buys.append(
                    {
                        "timestamp": record.get("timestamp"),
                        "symbol": symbol,
                        "status": status,
                        "reason": result.get("reason"),
                        "qty": result.get("qty"),
                        "limit_price": result.get("limit_price"),
                        "order_id": result.get("reentry_order_id"),
                    }
                )
            if status in BLOCKED_STATUSES:
                cash_blocked.append(cash_blocked_detail(record, result))
            for skipped in result.get("skipped_ladder_orders", []):
                if skipped.get("status") == "cash_reserve_blocked":
                    detail = cash_blocked_detail(record, {"status": "ladder_cash_reserve_blocked", "sizing": skipped.get("sizing", {})})
                    detail["symbol"] = symbol
                    detail["drop_step_percent"] = skipped.get("drop_step_percent")
                    cash_blocked.append(detail)

    return {
        "bot_buy_orders_submitted": buys,
        "cash_blocked_buy_signals": cash_blocked,
        "latest_watcher_status": latest_status,
    }


def enrich_bot_orders_with_broker_status(client, orders):
    enriched = []
    for item in orders or []:
        order_id = item.get("order_id")
        if not order_id:
            enriched.append({**item, "broker_status": "unavailable"})
            continue
        try:
            broker_order = client.order(order_id)
            terminal_at = next(
                (
                    broker_order.get(field)
                    for field in ("filled_at", "canceled_at", "expired_at", "failed_at")
                    if broker_order.get(field)
                ),
                None,
            )
            submitted_at = broker_order.get("submitted_at") or item.get("timestamp")
            submitted_time = parse_time(submitted_at)
            terminal_time = parse_time(terminal_at)
            active_seconds = (
                max(0, (terminal_time - submitted_time).total_seconds())
                if submitted_time and terminal_time
                else None
            )
            enriched.append(
                {
                    **item,
                    "broker_status": broker_order.get("status") or "unavailable",
                    "time_in_force": broker_order.get("time_in_force"),
                    "extended_hours": broker_order.get("extended_hours"),
                    "broker_submitted_at": submitted_at,
                    "broker_terminal_at": terminal_at,
                    "active_seconds": active_seconds,
                }
            )
        except Exception as exc:
            enriched.append(
                {
                    **item,
                    "broker_status": "unavailable",
                    "broker_order_error": f"{type(exc).__name__}: {exc}",
                }
            )
    return enriched


def fill_side(fill):
    side = (fill.get("side") or fill.get("order_side") or "").lower()
    if side:
        return side
    qty = float(fill.get("qty") or fill.get("net_qty") or 0)
    return "buy" if qty > 0 else "sell" if qty < 0 else ""


def fill_timestamp(fill):
    return fill.get("transaction_time") or fill.get("timestamp") or fill.get("date")


def summarize_fills(fills):
    bought = []
    sold = []
    for fill in fills:
        item = {
            "timestamp": fill_timestamp(fill),
            "symbol": fill.get("symbol"),
            "qty": fill.get("qty"),
            "price": fill.get("price"),
            "order_id": fill.get("order_id"),
        }
        side = fill_side(fill)
        if side == "buy":
            bought.append(item)
        elif side == "sell":
            sold.append(item)
    return bought, sold


def aggregate_order_fills(fills):
    orders = {}
    order_sequence = []
    for fill in fills or []:
        symbol = fill.get("symbol")
        side = fill_side(fill)
        order_id = fill.get("order_id")
        timestamp = fill_timestamp(fill)
        if not symbol or side not in ("buy", "sell") or not timestamp:
            continue

        key = (order_id, symbol, side)
        if key not in orders:
            orders[key] = {
                "timestamp": timestamp,
                "symbol": symbol,
                "side": side,
                "qty": 0.0,
                "notional": 0.0,
                "order_id": order_id,
            }
            order_sequence.append(key)

        qty = abs(as_float(fill.get("qty") or fill.get("net_qty")))
        price = as_float(fill.get("price"))
        orders[key]["qty"] += qty
        orders[key]["notional"] += qty * price
        if parse_time(timestamp) > parse_time(orders[key]["timestamp"]):
            orders[key]["timestamp"] = timestamp

    aggregated = []
    for key in order_sequence:
        order = orders[key]
        if not order["qty"]:
            continue
        aggregated.append({**order, "price": order["notional"] / order["qty"]})
    return sorted(aggregated, key=lambda item: parse_time(item["timestamp"]))


def realized_pl_by_sell_order(fills, start_at, end_at):
    lots = defaultdict(list)
    realized = {}
    for fill in aggregate_order_fills(fills):
        timestamp = parse_time(fill["timestamp"]).astimezone(LOCAL_TZ)
        symbol = fill["symbol"]
        qty = as_float(fill["qty"])
        price = as_float(fill["price"])
        if fill["side"] == "buy":
            lots[symbol].append({"qty": qty, "price": price})
            continue

        remaining = qty
        cost_basis = 0.0
        matched_qty = 0.0
        while remaining > 0 and lots[symbol]:
            lot = lots[symbol][0]
            take = min(remaining, lot["qty"])
            cost_basis += take * lot["price"]
            matched_qty += take
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 0:
                lots[symbol].pop(0)

        if not matched_qty or not (start_at <= timestamp < end_at):
            continue

        key = (symbol, fill.get("order_id"))
        item = realized.setdefault(key, {"matched_qty": 0.0, "cost_basis": 0.0, "realized_pl": 0.0})
        item["matched_qty"] += matched_qty
        item["cost_basis"] += cost_basis
        item["realized_pl"] += fill["notional"] * (matched_qty / qty) - cost_basis

    for item in realized.values():
        item["avg_entry_price"] = item["cost_basis"] / item["matched_qty"] if item["matched_qty"] else None
    return realized


def enrich_sold_fills_with_pl(sold, fills, start_at, end_at):
    realized = realized_pl_by_sell_order(fills, start_at, end_at)
    enriched = []
    for item in sold:
        pl = realized.get((item.get("symbol"), item.get("order_id")))
        if not pl or not pl.get("matched_qty"):
            enriched.append(item)
            continue
        ratio = as_float(item.get("qty")) / pl["matched_qty"]
        enriched.append(
            {
                **item,
                "cost_basis": pl["cost_basis"] * ratio,
                "realized_pl": pl["realized_pl"] * ratio,
                "avg_entry_price": pl["avg_entry_price"],
            }
        )
    return enriched


def watcher_entry_history(project_root, watchers_path, end_at):
    """Return entry-price snapshots retained by watcher state and logs."""
    history = defaultdict(list)
    state_fallbacks = {}
    for watcher in configured_watchers(project_root, watchers_path):
        symbol = watcher.get("symbol")
        if not symbol:
            continue
        state = load_json(watcher["state_path"], {})
        exit_price = state.get("last_exit_entry_price")
        exit_at = parse_time(state.get("last_exit_at"))
        if exit_price not in (None, "") and exit_at:
            state_fallbacks[(symbol, state.get("last_exit_order_id"))] = float(exit_price)

        log_path = watcher["log_path"]
        if not log_path.exists():
            continue
        with log_path.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = parse_time(record.get("timestamp"))
                if not timestamp or timestamp.astimezone(LOCAL_TZ) >= end_at:
                    continue
                result = record.get("result") or {}
                entry_price = result.get("entry_fill_price")
                if entry_price in (None, ""):
                    continue
                history[symbol].append((timestamp.astimezone(LOCAL_TZ), float(entry_price)))
    return history, state_fallbacks


def enrich_sold_fills_from_watchers(sold, project_root, watchers_path, end_at):
    """Fill unmatched sold cost basis from the latest local watcher snapshot."""
    history, state_fallbacks = watcher_entry_history(project_root, watchers_path, end_at)
    enriched = []
    for item in sold:
        if item.get("avg_entry_price") is not None:
            enriched.append(item)
            continue
        symbol = item.get("symbol")
        sold_at = parse_time(item.get("timestamp"))
        entry_price = state_fallbacks.get((symbol, item.get("order_id")))
        if entry_price is None and sold_at:
            sold_at = sold_at.astimezone(LOCAL_TZ)
            candidates = [price for timestamp, price in history.get(symbol, []) if timestamp <= sold_at]
            if candidates:
                entry_price = candidates[-1]
        if entry_price is None:
            enriched.append(item)
            continue
        qty = abs(as_float(item.get("qty")))
        sell_price = as_float(item.get("price"))
        enriched.append(
            {
                **item,
                "avg_entry_price": entry_price,
                "cost_basis": qty * entry_price,
                "realized_pl": qty * (sell_price - entry_price),
                "pl_source": "watcher",
            }
        )
    return enriched


def merge_fills(*fill_groups):
    merged = {}
    for fills in fill_groups:
        for fill in fills or []:
            key = (
                fill.get("id"),
                fill.get("order_id"),
                fill.get("symbol"),
                fill_timestamp(fill),
                fill.get("qty"),
                fill.get("price"),
            )
            merged[key] = fill
    return list(merged.values())


def summarize_positions(positions):
    summaries = []
    for position in positions or []:
        qty = as_float(position.get("qty"))
        current_price = as_float(position.get("current_price"))
        avg_entry_price = as_float(position.get("avg_entry_price"))
        market_value = as_float(position.get("market_value"))
        total_pl = as_float(position.get("unrealized_pl"))
        total_plpc = as_float(position.get("unrealized_plpc"))
        day_pl = as_float(position.get("unrealized_intraday_pl"))
        day_plpc = as_float(position.get("unrealized_intraday_plpc"))
        summaries.append(
            {
                "symbol": position.get("symbol"),
                "qty": qty,
                "avg_entry_price": avg_entry_price,
                "current_price": current_price,
                "market_value": market_value,
                "total_gain_loss": total_pl,
                "total_gain_loss_percent": total_plpc * 100,
                "daily_gain_loss": day_pl,
                "daily_gain_loss_percent": day_plpc * 100,
            }
        )
    return sorted(
        summaries,
        key=lambda item: (-(item.get("total_gain_loss") or 0), item.get("symbol") or ""),
    )


def pct_change(start_value, end_value):
    if start_value in (None, 0) or end_value is None:
        return None
    return round((float(end_value) / float(start_value) - 1) * 100, 4)


def portfolio_summary(client, account, reporting_config=None):
    reporting_config = reporting_config or {}
    history = client.portfolio_history(period="1A", timeframe="1D")
    values = [float(value) for value in history.get("equity", []) if value is not None]
    day_gain_percent = None
    total_gain_percent = None
    total_gain_baseline = reporting_config.get("total_gain_baseline")
    portfolio_value = float(account.get("portfolio_value", values[-1] if values else 0))
    if len(values) >= 2:
        day_gain_percent = pct_change(values[-2], values[-1])
    if values:
        total_gain_percent = pct_change(values[0], portfolio_value)
    if total_gain_percent is None and total_gain_baseline not in (None, "", 0):
        total_gain_percent = pct_change(float(total_gain_baseline), portfolio_value)
    cash_balance = as_float(account.get("cash"))
    cash_available = max(0.0, cash_balance)
    margin_used = max(0.0, -cash_balance)
    non_margin_buying_power = account.get("non_marginable_buying_power")
    if non_margin_buying_power in (None, ""):
        non_margin_buying_power = cash_available
    return {
        "cash": cash_available,
        "margin_used": margin_used,
        "buying_power": non_margin_buying_power,
        "cash_balance": account.get("cash"),
        "margin_buying_power": account.get("buying_power"),
        "equity": account.get("equity"),
        "portfolio_value": account.get("portfolio_value"),
        "day_gain_percent": day_gain_percent,
        "total_gain_percent": total_gain_percent,
        "total_gain_baseline": total_gain_baseline,
    }


def build_report(project_root, watchers_path, report_date, client=None):
    start_at, end_at = day_window(report_date)
    supervisor_config = load_json(watchers_path, {})
    reporting_config = supervisor_config.get("reporting", {})
    account_summary = {}
    bought = []
    sold = []
    positions = []
    flat_managed_stocks = []
    account_error = None

    if client:
        try:
            account = client.account()
            account_summary = portfolio_summary(client, account, reporting_config)
            realized_pl_lookback_days = int(reporting_config.get("realized_pl_lookback_days", 365))
            fill_start_at = start_at - datetime.timedelta(days=realized_pl_lookback_days)
            day_fills = client.fills(iso_utc(start_at), iso_utc(end_at))
            fills = merge_fills(client.fills(iso_utc(fill_start_at), iso_utc(end_at)), day_fills)
            bought, sold = summarize_fills(day_fills)
            sold = enrich_sold_fills_with_pl(sold, fills, start_at, end_at)
            sold = enrich_sold_fills_from_watchers(sold, project_root, watchers_path, end_at)
            positions = summarize_positions(client.positions())
            flat_managed_stocks = flat_managed_stock_evaluations(
                project_root, watchers_path, positions
            )
            health = watcher_position_healths(
                project_root,
                watchers_path,
                client=client,
                positions=positions,
            )
            positions = [
                {
                    **position,
                    **health.get(
                        position.get("symbol"),
                        unavailable_position_health("position_watcher_missing"),
                    ),
                }
                for position in positions
            ]
        except Exception as exc:
            account_error = f"{type(exc).__name__}: {exc}"

    activity = watcher_activity(project_root, watchers_path, report_date)
    if client:
        activity["bot_buy_orders_submitted"] = enrich_bot_orders_with_broker_status(
            client,
            activity["bot_buy_orders_submitted"],
        )
    return {
        "report_date": report_date.isoformat(),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": account_summary,
        "account_error": account_error,
        "current_positions": positions,
        "managed_stocks_not_held": flat_managed_stocks,
        "stocks_bought": bought,
        "stocks_sold": sold,
        **activity,
    }


def money(value):
    if value in (None, ""):
        return "n/a"
    return f"${float(value):,.2f}"


def percent(value):
    if value is None:
        return "n/a"
    return f"{float(value):.2f}%"


def number(value):
    if value in (None, ""):
        return "n/a"
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:,.4f}".rstrip("0").rstrip(".")


def short_time(value):
    timestamp = parse_time(value)
    if not timestamp:
        return "n/a"
    return timestamp.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def full_timestamp(value):
    timestamp = parse_time(value)
    if not timestamp:
        return "n/a"
    local = timestamp.astimezone(LOCAL_TZ)
    zone_name = local.tzname() or "local time"
    offset = local.strftime("%z")
    formatted_offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    return (
        f"{local.strftime('%B')} {local.day}, {local.year} at "
        f"{local.strftime('%I:%M:%S %p').lstrip('0')} "
        f"{zone_name} (UTC{formatted_offset})"
    )


def duration(value):
    if value is None:
        return "n/a"
    seconds = max(0, round(float(value)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def aggregate_fills(fills):
    grouped = {}
    for fill in fills or []:
        key = (fill.get("symbol"), fill.get("order_id"))
        item = grouped.setdefault(
            key,
            {
                "symbol": fill.get("symbol"),
                "order_id": fill.get("order_id"),
                "qty": 0.0,
                "notional": 0.0,
                "cost_basis": 0.0,
                "realized_pl": 0.0,
                "first_time": fill.get("timestamp"),
                "last_time": fill.get("timestamp"),
                "fills": 0,
            },
        )
        qty = as_float(fill.get("qty"))
        price = as_float(fill.get("price"))
        item["qty"] += qty
        item["notional"] += qty * price
        item["cost_basis"] += as_float(fill.get("cost_basis"))
        item["realized_pl"] += as_float(fill.get("realized_pl"))
        item["fills"] += 1
        item["last_time"] = fill.get("timestamp") or item["last_time"]
    summaries = []
    for item in grouped.values():
        avg_price = item["notional"] / item["qty"] if item["qty"] else None
        avg_entry_price = item["cost_basis"] / item["qty"] if item["qty"] and item["cost_basis"] else None
        summaries.append({**item, "avg_price": avg_price, "avg_entry_price": avg_entry_price})
    return sorted(summaries, key=lambda item: (item.get("symbol") or "", item.get("last_time") or ""))


def aggregate_cash_blocks(blocks):
    grouped = defaultdict(
        lambda: {
            "symbol": None,
            "count": 0,
            "first_time": None,
            "last_time": None,
            "last_cash": None,
            "last_min_cash_balance": None,
            "last_requested_notional": None,
            "max_requested_notional": 0.0,
            "max_requested_qty": 0.0,
            "best_available_notional": 0.0,
            "modes": set(),
        }
    )
    for block in blocks or []:
        symbol = block.get("symbol") or "UNKNOWN"
        item = grouped[symbol]
        item["symbol"] = symbol
        item["count"] += 1
        item["first_time"] = item["first_time"] or block.get("timestamp")
        if block.get("timestamp"):
            item["last_time"] = block.get("timestamp")
            item["last_cash"] = as_float(block.get("cash"))
            item["last_min_cash_balance"] = as_float(block.get("min_cash_balance"))
            item["last_requested_notional"] = as_float(block.get("target_notional"))
        item["max_requested_notional"] = max(
            item["max_requested_notional"],
            as_float(block.get("target_notional")),
        )
        item["max_requested_qty"] = max(
            item["max_requested_qty"],
            as_float(block.get("requested_qty")),
        )
        item["best_available_notional"] = max(
            item["best_available_notional"],
            as_float(block.get("available_notional")),
        )
        if block.get("mode"):
            item["modes"].add(block["mode"])

    summaries = []
    for item in grouped.values():
        summaries.append(
            {
                **item,
                "last_cash_shortfall": max(
                    0.0,
                    as_float(item["last_requested_notional"])
                    - max(0.0, as_float(item["last_cash"]) - as_float(item["last_min_cash_balance"])),
                ),
                "modes": ", ".join(sorted(item["modes"])) or "n/a",
            }
        )
    return sorted(summaries, key=lambda item: (-item["count"], item["symbol"]))


def markdown_table(headers, rows, alignments=None, pad_columns=False, minimum_width=0):
    def separator(alignment):
        if alignment == "right":
            return "---:"
        if alignment == "center":
            return ":---:"
        return "---"

    alignments = alignments or []
    string_rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    if pad_columns:
        for row in string_rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(cell))
        widths = [max(width, minimum_width) for width in widths]

    def format_cell(value, index):
        if not pad_columns:
            return value
        alignment = alignments[index] if index < len(alignments) else None
        return value.rjust(widths[index]) if alignment == "right" else value.ljust(widths[index])

    lines = [
        "| " + " | ".join(format_cell(header, index) for index, header in enumerate(headers)) + " |",
        "| "
        + " | ".join(
            format_cell(
                separator(alignments[index] if index < len(alignments) else None),
                index,
            )
            for index, _ in enumerate(headers)
        )
        + " |",
    ]
    lines.extend(
        "| "
        + " | ".join(format_cell(cell, index) for index, cell in enumerate(row))
        + " |"
        for row in string_rows
    )
    return lines


def render_fill_table(items, empty_text, include_realized_pl=False):
    summaries = aggregate_fills(items)
    if not summaries:
        return [empty_text]
    rows = []
    for item in summaries:
        row = [
            item.get("symbol") or "n/a",
            number(item.get("qty")),
            money(item.get("avg_price")),
            money(item.get("notional")),
        ]
        if include_realized_pl:
            row.extend([money(item.get("avg_entry_price")), signed_money(item.get("realized_pl"))])
        row.extend([item.get("fills"), short_time(item.get("last_time"))])
        rows.append(row)
    headers = ["Symbol", "Qty", "Avg Price", "Notional"]
    if include_realized_pl:
        headers.extend(["Avg Entry", "Realized P/L"])
    headers.extend(["Fills", "Last Fill"])
    alignments = ["left", "right", "right", "right"]
    if include_realized_pl:
        alignments.extend(["right", "right"])
    alignments.extend(["right", "right"])
    return markdown_table(
        headers,
        rows,
        alignments,
        pad_columns=True,
        minimum_width=6,
    )


def render_bot_order_table(items):
    if not items:
        return ["No bot buy submissions found in watcher logs."]
    rows = []
    for item in items:
        rows.append(
            [
                item.get("symbol") or "n/a",
                number(item.get("qty")),
                money(item.get("limit_price")),
                item.get("reason") or "n/a",
                (item.get("time_in_force") or "n/a").upper(),
                (item.get("broker_status") or "unavailable").replace("_", " ").title(),
                short_time(item.get("broker_submitted_at") or item.get("timestamp")),
                short_time(item.get("broker_terminal_at")),
                duration(item.get("active_seconds")),
            ]
        )
    return markdown_table(
        ["Symbol", "Qty", "Limit", "Reason", "TIF", "Final Status", "Submitted", "Ended", "Active Time"],
        rows,
        ["left", "right", "right", "left", "left", "left", "right", "right", "right"],
        pad_columns=True,
        minimum_width=6,
    )


def render_cash_block_table(items):
    summaries = aggregate_cash_blocks(items)
    if not summaries:
        return ["No cash-blocked buy signals found."]
    rows = []
    for item in summaries:
        rows.append(
            [
                item["symbol"],
                item["count"],
                money(item["max_requested_notional"]),
                number(item["max_requested_qty"]),
                money(item["best_available_notional"]),
                money(item["last_cash"]),
                money(item["last_min_cash_balance"]),
                money(item["last_cash_shortfall"]),
                short_time(item["first_time"]),
                short_time(item["last_time"]),
            ]
        )
    return markdown_table(
        [
            "Symbol",
            "Signals",
            "Largest Target",
            "Max Qty",
            "Best Cash Above Reserve",
            "Cash At Last Block",
            "Reserve At Last Block",
            "Last Shortfall",
            "First",
            "Last",
        ],
        rows,
        [
            "left",
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
        ],
        pad_columns=True,
        minimum_width=6,
    )


def signed_money(value):
    if value in (None, ""):
        return "n/a"
    numeric = float(value)
    if numeric < 0:
        return f"-${abs(numeric):,.2f}"
    sign = "+" if numeric > 0 else ""
    return f"{sign}${numeric:,.2f}"


def signed_percent(value):
    if value is None:
        return "n/a"
    numeric = float(value)
    sign = "+" if numeric > 0 else ""
    return f"{sign}{numeric:.2f}%"


def render_positions_table(positions):
    if not positions:
        return ["No open Alpaca positions found."]
    rows = []
    def total_percent_sort_key(item):
        try:
            total_percent = float(item.get("total_gain_loss_percent"))
        except (TypeError, ValueError):
            return (1, 0, item.get("symbol") or "")
        return (0, -total_percent, item.get("symbol") or "")

    for item in sorted(positions, key=total_percent_sort_key):
        rows.append(
            [
                item.get("symbol") or "n/a",
                number(item.get("qty")),
                money(item.get("market_value")),
                money(item.get("avg_entry_price")),
                money(item.get("current_price")),
                signed_money(item.get("total_gain_loss")),
                signed_percent(item.get("total_gain_loss_percent")),
                (
                    f'{item.get("position_health_state")} '
                    f'({item.get("position_health_score")}%)'
                    if item.get("position_health_score") is not None
                    else item.get("position_health_state", "Unavailable")
                ),
                item.get("position_health_action") or "freeze",
                (
                    f'{float(item.get("position_health_remaining_r")):.2f}'
                    if item.get("position_health_remaining_r") is not None
                    else "n/a"
                ),
                signed_money(item.get("daily_gain_loss")),
                signed_percent(item.get("daily_gain_loss_percent")),
            ]
        )
    return markdown_table(
        [
            "Symbol",
            "Qty",
            "Market Value",
            "Avg Entry",
            "Current",
            "Total P/L",
            "Total %",
            "Position Health",
            "Action",
            "Remaining R",
            "Day P/L",
            "Day %",
        ],
        rows,
        [
            "left", "right", "right", "right", "right", "right", "right",
            "left", "left", "right", "right", "right",
        ],
        pad_columns=True,
        minimum_width=6,
    )


def render_position_health_table(positions):
    if not positions:
        return ["No open positions to assess."]

    rows = []
    for item in sorted(positions, key=lambda position: position.get("symbol") or ""):
        score = item.get("position_health_score")
        state = item.get("position_health_state") or "Unavailable"
        health = f"{state} ({score}%)" if score is not None else state
        stop_price = item.get("position_health_stop_price")
        stop_qty = item.get("position_health_stop_qty")
        protection = (
            f"{money(stop_price)} x {number(stop_qty)}"
            if stop_price is not None and stop_qty is not None
            else "n/a"
        )
        if item.get("position_health_data_fresh") and item.get("position_health_data_complete"):
            data_status = "Fresh / Complete"
        elif item.get("position_health_data_fresh"):
            data_status = "Fresh / Incomplete"
        elif item.get("position_health_data_complete"):
            data_status = "Stale / Complete"
        else:
            data_status = "Stale / Incomplete"
        reasons = item.get("position_health_reasons") or []
        rows.append(
            [
                item.get("symbol") or "n/a",
                health,
                item.get("position_health_action") or "freeze",
                percent(item.get("position_health_entry_return_percent")),
                number(item.get("position_health_downside_score")),
                number(item.get("position_health_trend_score")),
                number(item.get("position_health_reward_risk_score")),
                number(item.get("position_health_remaining_r")),
                protection,
                data_status,
                short_time(item.get("position_health_as_of")),
                ", ".join(reasons) if reasons else "none",
            ]
        )
    return markdown_table(
        [
            "Symbol", "Health", "Action", "Entry Return", "Downside", "Trend",
            "Reward/Risk", "Remaining R", "Stop Coverage", "Data", "As Of", "Reasons",
        ],
        rows,
        ["left", "left", "left", "right", "right", "right", "right", "right", "right", "left", "right", "left"],
        pad_columns=True,
        minimum_width=6,
    )


def render_flat_managed_stocks_table(items):
    if not items:
        return ["No managed stocks are currently flat."]
    rows = []
    for item in items:
        eligibility = item.get("entry_eligibility") or {}
        eligibility_reasons = eligibility.get("reasons") or []
        if eligibility.get("eligible"):
            decision = "Eligible For Re-entry"
        elif eligibility_reasons:
            decision = eligibility_reasons[0].replace("_", " ").title()
        else:
            decision = (item.get("evaluation_status") or "unavailable").replace("_", " ").title()
        score = item.get("signal_score")
        signal = (
            f'{item.get("signal_strength", "Unavailable")} ({score}%)'
            if score is not None
            else item.get("signal_strength", "Unavailable")
        )
        if "stale_entry_plan" in eligibility_reasons and score is not None:
            signal = f"Historical {signal}"
        blockers = item.get("signal_blockers") or []
        rows.append(
            [
                item.get("symbol") or "n/a",
                "YES" if item.get("reentry_qualified") else "NO",
                decision,
                signal,
                (item.get("entry_mode") or "waiting").replace("_", " "),
                money(item.get("last_price")),
                money(item.get("next_signal_trigger")),
                money(item.get("limit_price")),
                item.get("price_action") or "n/a",
                number(item.get("volume_ratio")),
                short_time(item.get("signal_as_of")),
                duration(eligibility.get("plan_age_seconds")),
                ", ".join(eligibility_reasons) if eligibility_reasons else "none",
                ", ".join(blockers) if blockers else "none",
            ]
        )
    return markdown_table(
        [
            "Symbol", "Re-entry Qualified", "Decision", "Entry Setup", "Mode", "Price", "Next Trigger",
            "Planned Limit", "Price Action", "Volume Ratio", "As Of", "Plan Age",
            "Eligibility Reasons", "Signal Blockers",
        ],
        rows,
        [
            "left", "left", "left", "left", "left", "right", "right", "right", "left",
            "right", "right", "right", "left", "left",
        ],
        pad_columns=True,
        minimum_width=6,
    )


def render_markdown(report):
    account = report.get("account") or {}
    positions = report.get("current_positions") or []
    bought = report.get("stocks_bought") or []
    sold = report.get("stocks_sold") or []
    bot_buys = report.get("bot_buy_orders_submitted") or []
    cash_blocked = report.get("cash_blocked_buy_signals") or []
    flat_managed_stocks = report.get("managed_stocks_not_held") or []
    lines = [
        f"# TraderBot Daily Report - {report['report_date']}",
        "",
        f"Generated: {full_timestamp(report.get('generated_at'))}",
        "",
        "## Snapshot",
    ]
    lines.extend(
        markdown_table(
            [
                "Portfolio",
                "Day Gain",
                "Total Gain",
                "Cash Available",
                "Margin Used",
                "Buying Power (No Margin)",
            ],
            [
                [
                    money(account.get("portfolio_value")),
                    percent(account.get("day_gain_percent")),
                    percent(account.get("total_gain_percent")),
                    money(account.get("cash")),
                    money(account.get("margin_used")),
                    money(account.get("buying_power")),
                ]
            ],
            ["right", "right", "right", "right", "right", "right"],
            pad_columns=True,
            minimum_width=6,
        )
    )
    if report.get("account_error"):
        lines.extend(["", f"Account data error: {report['account_error']}"])

    lines.extend(
        [
            "",
            "## Activity Summary",
            f"- Current open positions: {len(positions)}.",
            f"- Alpaca buy fills: {len(bought)} fill(s) across {len(aggregate_fills(bought))} order(s).",
            f"- Alpaca sell fills: {len(sold)} fill(s) across {len(aggregate_fills(sold))} order(s).",
            f"- Bot buy submissions: {len(bot_buys)} order(s).",
            f"- Cash-blocked buy signals: {len(cash_blocked)} signal(s) across {len(aggregate_cash_blocks(cash_blocked))} symbol(s).",
            "",
            "## Current Positions",
        ]
    )
    lines.extend(render_positions_table(positions))
    lines.extend(
        [
            "",
            "## Position Health",
        ]
    )
    lines.extend(render_position_health_table(positions))
    lines.extend(
        [
            "",
            "## Managed Stocks Not Currently Held",
            "These are configured managed symbols with zero shares at Alpaca. Re-entry Qualified shows whether a symbol passes the execution checks with only observer mode ignored; YES does not authorize an order while observer mode is enabled. Decision and eligibility reasons show actual execution eligibility. Historical plans remain visible for audit but cannot authorize an order.",
            "",
        ]
    )
    lines.extend(render_flat_managed_stocks_table(flat_managed_stocks))
    lines.extend(
        [
            "",
            "### Entry / Re-entry Method",
            "Bots evaluate completed five-minute bars for market regime, EMA trend and slope, VWAP/pullback or breakout confirmation, volume, chase protection, and exit-ledger price constraints. All required checks must pass before an order is eligible; portfolio risk, cash reserve, cooldown, and daily limits can still block submission afterward.",
        ]
    )
    lines.extend(
        [
            "",
            "### Position Health Method",
            "Position Health evaluates an open holding using entry-relative downside (45%), fresh holding-period trend (35%), and remaining reward/risk to the active broker stop (20%). Entry Setup scores are reserved for flat candidates and are not used as holding labels.",
            "",
            "Hard safety rules override the weighted score: incomplete stop coverage is Unprotected, a 6% entry-relative loss requires reduction, and the broker-held 8% catastrophic stop exits the remainder.",
            "",
            "Ratings: **Healthy 80-100**, **Stable 65-79**, **Watch 45-64**, **At Risk 25-44**, and **Critical below 25**. **Unavailable** freezes discretionary actions until fresh, complete health data is available. The score is a risk-management assessment, not a return prediction.",
        ]
    )
    lines.extend(
        [
            "",
            "## Bought",
        ]
    )
    lines.extend(render_fill_table(bought, "No Alpaca buy fills found."))
    lines.extend(["", "## Sold"])
    lines.extend(render_fill_table(sold, "No Alpaca sell fills found.", include_realized_pl=True))
    lines.extend(["", "## Bot Buy Orders Submitted"])
    lines.extend(render_bot_order_table(bot_buys))
    lines.extend(["", "## Buy Signals Blocked By Cash"])
    lines.extend(render_cash_block_table(cash_blocked))
    lines.extend(
        [
            "",
            "Detailed event records are preserved in the matching JSON report.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_items(items, empty_text):
    if not items:
        return [f"- {empty_text}"]
    lines = []
    for item in items:
        clean = {key: value for key, value in item.items() if value not in (None, "", [])}
        details = ", ".join(f"{key}={value}" for key, value in clean.items())
        lines.append(f"- {details}")
    return lines


def write_report(report, report_dir):
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"daily_report_{report['report_date']}"
    json_path = report_dir / f"{stem}.json"
    markdown_path = report_dir / f"{stem}.md"
    save_json(json_path, report)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_WATCHERS_PATH)
    parser.add_argument("--date", help="Report date in YYYY-MM-DD. Defaults to last closed trading day.")
    parser.add_argument("--output-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--offline", action="store_true", help="Skip Alpaca account and fill lookups.")
    parser.add_argument(
        "--skip-non-trading-day",
        action="store_true",
        help="Exit without writing a report when the report date is not an Alpaca market day.",
    )
    args = parser.parse_args()

    project_root = Path(args.config).resolve().parent.parent
    report_date = (
        datetime.date.fromisoformat(args.date)
        if args.date
        else last_closed_trading_day()
    )
    client = None
    if not args.offline:
        load_env(project_root / ".env")
        client = AlpacaClient()

    if args.skip_non_trading_day and not is_market_day(client, report_date):
        print(f"Skipping daily report: {report_date.isoformat()} is not a market day.")
        return 0

    report = build_report(
        project_root,
        Path(args.config).resolve(),
        report_date,
        client=client,
    )
    _, markdown_path = write_report(report, project_root / args.output_dir)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
