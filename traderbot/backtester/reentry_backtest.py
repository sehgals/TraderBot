import argparse
import datetime
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from traderbot.core_strategy_engine.engine import load_env, load_json


DEFAULT_START = "2026-06-01T00:00:00Z"
DEFAULT_TIMEFRAME = "5Min"
MARKET_SYMBOL = "QQQ"


def parse_time(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_utc(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def dollars(value):
    return f"{value:.2f}"


class AlpacaData:
    def __init__(self):
        self.trade_base_url = os.environ["ALPACA_BASE_URL"].rstrip("/")
        self.data_base_url = os.environ.get(
            "ALPACA_DATA_URL", "https://data.alpaca.markets/v2"
        ).rstrip("/")
        self.headers = {
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        }

    def request(self, base_url, path):
        request = urllib.request.Request(
            f"{base_url}{path}", headers=self.headers, method="GET"
        )
        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body) if body else None
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        raise last_error

    def trading(self, path):
        return self.request(self.trade_base_url, path)

    def data(self, path):
        return self.request(self.data_base_url, path)

    def fills(self, start):
        fills = []
        page_token = None
        seen_page_tokens = set()
        while True:
            params = {
                "activity_types": "FILL",
                "after": start,
                "direction": "asc",
                "page_size": "100",
            }
            if page_token:
                params["page_token"] = page_token
            payload = self.trading(
                f"/account/activities?{urllib.parse.urlencode(params)}"
            ) or []
            if isinstance(payload, dict):
                page = payload.get("activities", [])
                page_token = payload.get("next_page_token")
            else:
                page = payload
                page_token = page[-1].get("id") if len(page) == 100 else None
            fills.extend(page)
            if not page_token or page_token in seen_page_tokens:
                return fills
            seen_page_tokens.add(page_token)

    def bars(self, symbol, start, end, timeframe):
        bars = []
        page_token = None
        while True:
            params = {
                "symbols": symbol,
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "adjustment": "raw",
                "feed": "iex",
                "limit": "10000",
            }
            if page_token:
                params["page_token"] = page_token
            query = urllib.parse.urlencode(params)
            payload = self.data(f"/stocks/bars?{query}") or {}
            bars.extend(payload.get("bars", {}).get(symbol, []))
            page_token = payload.get("next_page_token")
            if not page_token:
                return bars


def load_strategy_configs(watchers_path):
    watchers_file = Path(watchers_path).resolve()
    project_root = (
        watchers_file.parent.parent
        if watchers_file.parent.name == "config"
        else watchers_file.parent
    )
    supervisor_config = load_json(watchers_path, {})
    global_position_health = supervisor_config.get("position_health") or {}
    global_entry_filters = supervisor_config.get("entry_filters") or {}
    global_portfolio_allocator = supervisor_config.get("portfolio_allocator") or {}
    global_winner_management = supervisor_config.get("winner_management") or {}
    managed_defaults = supervisor_config.get("managed_watcher_defaults", {})
    configs = {}
    for watcher in supervisor_config.get("managed_watchers", supervisor_config.get("watchers", [])):
        config_path = project_root / watcher["config"]
        config = {**managed_defaults, **load_json(config_path, {})}
        config["position_health"] = {
            **global_position_health,
            **(config.get("position_health") or {}),
        }
        config["entry_filters"] = {
            **global_entry_filters,
            **(config.get("entry_filters") or {}),
        }
        config["portfolio_allocator"] = {
            **global_portfolio_allocator,
            **(config.get("portfolio_allocator") or {}),
        }
        config["winner_management"] = {
            **global_winner_management,
            **(config.get("winner_management") or {}),
        }
        configs[config["symbol"]] = config
    new_defaults = supervisor_config.get("new_watcher_defaults", {})
    for watcher in supervisor_config.get("new_watchers", []):
        config = {**new_defaults, **watcher.get("config_defaults", {})}
        config.setdefault("symbol", watcher["symbol"])
        config["position_health"] = {
            **global_position_health,
            **(config.get("position_health") or {}),
        }
        config["entry_filters"] = {
            **global_entry_filters,
            **(config.get("entry_filters") or {}),
        }
        config["portfolio_allocator"] = {
            **global_portfolio_allocator,
            **(config.get("portfolio_allocator") or {}),
        }
        config["winner_management"] = {
            **global_winner_management,
            **(config.get("winner_management") or {}),
        }
        configs[config["symbol"]] = config
    return configs


def isolate_entry_model_configs(configs, model_name):
    isolated = {symbol: dict(config) for symbol, config in configs.items()}
    known = {
        "pullback", "breakout", "relative_strength", "low_volatility_trend",
        "post_earnings_drift", "trend_mean_reversion", "defensive_etf",
    }
    if model_name not in known:
        raise ValueError(f"unknown entry model: {model_name}")
    for symbol, config in isolated.items():
        models = {
            name: dict(settings or {})
            for name, settings in (config.get("entry_models") or {}).items()
        }
        for name in known:
            models.setdefault(name, {})["enabled"] = name == model_name
        config["entry_models"] = models
    return isolated


def default_strategy_config(symbol):
    return {
        "symbol": symbol,
        "entry_quantity": 100,
        "initial_stop_loss_percent": 20,
        "trail_trigger_step_percent": 5,
        "trail_stop_below_current_percent": 2.5,
        "ladder_buy_quantity": 100,
        "ladder_drop_steps_percent": [5, 10],
    }


def calculate_indicators(raw_bars):
    bars = []
    ema9 = ema21 = ema50 = None
    atr = None
    prev_close = None
    session_date = None
    session_pv = 0.0
    session_volume = 0.0
    true_ranges = []
    volumes = []

    for raw in raw_bars:
        timestamp = parse_time(raw["t"])
        high = float(raw["h"])
        low = float(raw["l"])
        close = float(raw["c"])
        volume = float(raw["v"])
        typical = (high + low + close) / 3

        bar_date = timestamp.astimezone(datetime.timezone.utc).date()
        if session_date != bar_date:
            session_date = bar_date
            session_pv = 0.0
            session_volume = 0.0

        session_pv += typical * volume
        session_volume += volume
        vwap = session_pv / session_volume if session_volume else close

        if ema9 is None:
            ema9 = ema21 = ema50 = close
        else:
            ema9 = close * (2 / 10) + ema9 * (1 - 2 / 10)
            ema21 = close * (2 / 22) + ema21 * (1 - 2 / 22)
            ema50 = close * (2 / 51) + ema50 * (1 - 2 / 51)

        if prev_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(true_range)
        if len(true_ranges) > 14:
            true_ranges.pop(0)
        atr = sum(true_ranges) / len(true_ranges)

        volumes.append(volume)
        if len(volumes) > 20:
            volumes.pop(0)
        avg_volume = sum(volumes) / len(volumes) if volumes else 0
        volume_ratio = volume / avg_volume if avg_volume else 1

        bars.append(
            {
                "t": timestamp,
                "o": float(raw["o"]),
                "h": high,
                "l": low,
                "c": close,
                "v": volume,
                "ema9": ema9,
                "ema21": ema21,
                "ema50": ema50,
                "atr14": atr,
                "vwap": vwap,
                "volume_ratio": volume_ratio,
            }
        )
        prev_close = close

    return bars


def pair_completed_trades(fills, symbols):
    fills = aggregate_order_fills(fills, symbols)
    lots = defaultdict(list)
    episode_ids = {}
    exits = []
    for fill in fills:
        symbol = fill.get("symbol")
        if symbol not in symbols:
            continue

        side = fill["side"]
        qty = float(fill["qty"])
        price = float(fill["price"])
        timestamp = parse_time(fill["transaction_time"])
        if side == "buy":
            if not lots[symbol]:
                episode_ids[symbol] = (
                    fill.get("episode_id")
                    or f"{symbol}:{fill.get('order_id') or fill['transaction_time']}"
                )
            lots[symbol].append({"qty": qty, "price": price, "time": timestamp})
            continue

        remaining = qty
        cost = 0.0
        matched_qty = 0.0
        entry_time = None
        while remaining > 0 and lots[symbol]:
            lot = lots[symbol][0]
            take = min(remaining, lot["qty"])
            cost += take * lot["price"]
            matched_qty += take
            entry_time = entry_time or lot["time"]
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 0:
                lots[symbol].pop(0)

        if matched_qty:
            avg_entry = cost / matched_qty
            realized_pl = matched_qty * (price - avg_entry)
            exits.append(
                {
                    "symbol": symbol,
                    "entry_time": entry_time,
                    "exit_time": timestamp,
                    "qty": matched_qty,
                    "avg_entry": avg_entry,
                    "exit_price": price,
                    "realized_pl": realized_pl,
                    "episode_id": fill.get("episode_id") or episode_ids.get(symbol),
                    "exit_order_id": fill.get("order_id"),
                    "exit_reason": fill.get("exit_reason") or "unknown",
                }
            )

        if not lots[symbol]:
            episode_ids.pop(symbol, None)

    return exits


def aggregate_order_fills(fills, symbols):
    orders = {}
    order_sequence = []
    for fill in fills:
        symbol = fill.get("symbol")
        if symbol not in symbols:
            continue

        order_id = fill.get("order_id")
        key = (order_id, symbol, fill.get("side"))
        if key not in orders:
            orders[key] = {
                "transaction_time": fill["transaction_time"],
                "symbol": symbol,
                "side": fill["side"],
                "qty": 0.0,
                "notional": 0.0,
                "order_id": order_id,
                "episode_id": fill.get("episode_id"),
                "exit_reason": fill.get("exit_reason"),
            }
            order_sequence.append(key)

        qty = float(fill["qty"])
        price = float(fill["price"])
        orders[key]["qty"] += qty
        orders[key]["notional"] += qty * price
        if parse_time(fill["transaction_time"]) > parse_time(orders[key]["transaction_time"]):
            orders[key]["transaction_time"] = fill["transaction_time"]

    aggregated = []
    for key in order_sequence:
        order = orders[key]
        if not order["qty"]:
            continue
        aggregated.append(
            {
                "transaction_time": order["transaction_time"],
                "symbol": order["symbol"],
                "side": order["side"],
                "qty": str(order["qty"]),
                "price": str(order["notional"] / order["qty"]),
                "order_id": order["order_id"],
                "episode_id": order.get("episode_id"),
                "exit_reason": order.get("exit_reason"),
                "type": "aggregated_fill",
            }
        )
    aggregated.sort(key=lambda item: parse_time(item["transaction_time"]))
    return aggregated


def market_ok_at(market_bars, timestamp):
    prior = [bar for bar in market_bars if bar["t"] <= timestamp]
    if len(prior) < 6:
        return True
    bar = prior[-1]
    previous = prior[-6]
    ema21_slope = (bar["ema21"] - previous["ema21"]) / previous["ema21"]
    return (
        bar["c"] > bar["vwap"]
        and bar["c"] > bar["ema21"]
        and ema21_slope >= 0
    )


def prior_window(bars, index, size):
    start = max(0, index - size)
    return bars[start:index]


def allowed_ledger_price(exit_price, realized_pl, strong=False):
    if realized_pl >= 0:
        premium = 1.025 if strong else 1.01
    else:
        premium = 0.985
    return exit_price * premium


def reward_risk_ok(entry_price, target_price, atr, minimum=1.5):
    stop_price = max(entry_price * 0.98, entry_price - atr)
    risk = entry_price - stop_price
    reward = target_price - entry_price
    if risk <= 0:
        return False
    return reward / risk >= minimum


def dynamic_signal(bars, index, exit_trade, market_bars, ignore_ledger=False):
    if index < 50:
        return None

    bar = bars[index]
    previous = bars[index - 1]
    window = prior_window(bars, index, 20)
    if not window:
        return None
    market_ok = market_ok_at(market_bars, bar["t"])

    atr = max(bar["atr14"], 0.01)
    ema21_slope = (bar["ema21"] - bars[index - 5]["ema21"]) / bars[index - 5]["ema21"]
    recent_high = max(item["h"] for item in window)
    recent_low = min(item["l"] for item in window)
    pullback_zone = max(
        bar["ema21"],
        bar["vwap"],
        recent_low + 0.382 * (recent_high - recent_low),
    )

    strong_volume = bar["volume_ratio"] >= 1.5
    ledger_cap = (
        math.inf
        if ignore_ledger
        else allowed_ledger_price(
            exit_trade["exit_price"], exit_trade["realized_pl"], strong=strong_volume
        )
    )
    no_chase = bar["c"] <= bar["ema21"] + 0.75 * atr
    if not ignore_ledger:
        same_day_exit = bar["t"].date() == exit_trade["exit_time"].date()
        if exit_trade["realized_pl"] < 0 and same_day_exit:
            return None
        if exit_trade["realized_pl"] >= 0 and bar["c"] < exit_trade["exit_price"]:
            return None

    touched_pullback = previous["l"] <= pullback_zone * 1.005
    reclaim_trend_ok = (
        bar["c"] > bar["vwap"]
        and bar["c"] > bar["ema21"]
        and bar["ema21"] >= bar["ema50"]
        and ema21_slope >= 0.002
        and no_chase
    )
    reclaimed = bar["c"] > max(bar["ema9"], bar["vwap"]) and bar["c"] > previous["c"]
    vwap_stability = sum(1 for item in bars[index - 2 : index + 1] if item["c"] > item["vwap"]) >= 2
    if touched_pullback and reclaimed and reclaim_trend_ok and vwap_stability and bar["volume_ratio"] >= 1.15:
        limit_price = min(bar["c"], pullback_zone + 0.10 * atr)
        if limit_price <= ledger_cap and reward_risk_ok(limit_price, recent_high, atr, minimum=1.5):
            return {
                "mode": "dynamic_pullback_reclaim",
                "trigger_time": bar["t"],
                "limit_price": limit_price,
                "ledger_cap": ledger_cap,
                "market_ok": market_ok,
                "market_filter_ignored": not market_ok,
            }

    breakout = bar["c"] > recent_high and bar["volume_ratio"] >= 1.3
    trend_ok = (
        bar["c"] > bar["vwap"]
        and bar["c"] > bar["ema9"] > bar["ema21"]
        and bar["ema21"] >= bar["ema50"]
        and ema21_slope >= 0.002
    )
    if breakout and trend_ok and bar["volume_ratio"] >= 1.5:
        limit_price = min(bar["c"], recent_high + 0.10 * atr)
        breakout_target = limit_price + 2 * atr
        if limit_price <= ledger_cap and reward_risk_ok(limit_price, breakout_target, atr, minimum=1.5):
            return {
                "mode": "dynamic_breakout_continuation",
                "trigger_time": bar["t"],
                "limit_price": limit_price,
                "ledger_cap": ledger_cap,
                "market_ok": market_ok,
                "market_filter_ignored": not market_ok,
            }

    return None


def current_dynamic_plan(symbol, bars, market_bars, exit_trade=None, ignore_ledger=False):
    if len(bars) < 51:
        return {"symbol": symbol, "status": "not_enough_bars"}

    index = len(bars) - 1
    bar = bars[index]
    previous = bars[index - 1]
    window = prior_window(bars, index, 20)
    atr = max(bar["atr14"], 0.01)
    ema21_slope = (bar["ema21"] - bars[index - 5]["ema21"]) / bars[index - 5]["ema21"]
    recent_high = max(item["h"] for item in window)
    recent_low = min(item["l"] for item in window)
    pullback_zone = max(
        bar["ema21"],
        bar["vwap"],
        recent_low + 0.382 * (recent_high - recent_low),
    )
    strong_volume = bar["volume_ratio"] >= 1.5
    ledger_cap = (
        math.inf
        if ignore_ledger or not exit_trade
        else allowed_ledger_price(
            exit_trade["exit_price"], exit_trade["realized_pl"], strong=strong_volume
        )
    )
    pullback_limit = min(pullback_zone + 0.10 * atr, ledger_cap)
    breakout_limit = min(recent_high + 0.10 * atr, ledger_cap)
    pullback_touch_trigger = pullback_zone * 1.005
    pullback_reclaim_trigger = max(bar["ema9"], bar["vwap"], previous["c"])
    signal_trigger_candidates = [
        price
        for price in (pullback_reclaim_trigger, recent_high)
        if price >= bar["c"]
    ]
    next_signal_trigger = min(signal_trigger_candidates) if signal_trigger_candidates else None
    market_ok = market_ok_at(market_bars, bar["t"])
    signal = dynamic_signal(
        bars,
        index,
        exit_trade or {"exit_price": bar["c"], "realized_pl": 0, "exit_time": bar["t"]},
        market_bars,
        ignore_ledger=ignore_ledger or not exit_trade,
    )
    market_filter_ignored = bool(signal and signal.get("market_filter_ignored"))

    checks = {
        "market_ok": market_ok or market_filter_ignored,
        "above_exit": True
        if ignore_ledger or not exit_trade
        else exit_trade["realized_pl"] < 0 or bar["c"] >= exit_trade["exit_price"],
        "above_ema9": bar["c"] > bar["ema9"],
        "ema9_above_ema21": bar["ema9"] > bar["ema21"],
        "ema21_slope_ok": ema21_slope >= 0.002,
        "no_chase": bar["c"] <= bar["ema21"] + 0.75 * atr,
        "pullback_volume_ok": bar["volume_ratio"] >= 1.15,
        "breakout_volume_ok": bar["volume_ratio"] >= 1.5,
        "trend_ok": bar["c"] > bar["vwap"] and bar["c"] > bar["ema21"] and bar["ema21"] >= bar["ema50"],
        "touched_pullback": previous["l"] <= pullback_zone * 1.005,
        "breakout_now": bar["c"] > recent_high,
    }
    blockers = [name for name, ok in checks.items() if not ok]

    return {
        "symbol": symbol,
        "status": "active_signal" if signal else "watch",
        "mode": signal.get("mode") if signal else None,
        "ledger_ignored": ignore_ledger or not exit_trade,
        "market_ok": market_ok,
        "market_filter_ignored": market_filter_ignored,
        "last_bar_time": bar["t"],
        "last_price": bar["c"],
        "last_exit_time": exit_trade["exit_time"] if exit_trade else None,
        "last_exit_price": exit_trade["exit_price"] if exit_trade else None,
        "last_trade_pl": exit_trade["realized_pl"] if exit_trade else None,
        "ledger_cap": ledger_cap,
        "pullback_zone": pullback_zone,
        "pullback_touch_trigger": pullback_touch_trigger,
        "pullback_reclaim_trigger": pullback_reclaim_trigger,
        "pullback_limit": pullback_limit,
        "breakout_trigger": recent_high,
        "breakout_limit": breakout_limit,
        "next_signal_trigger": next_signal_trigger,
        "atr14": atr,
        "ema9": bar["ema9"],
        "ema21": bar["ema21"],
        "ema50": bar["ema50"],
        "vwap": bar["vwap"],
        "volume_ratio": bar["volume_ratio"],
        "ema21_slope": ema21_slope,
        "price_action": price_action_label(bar, ema21_slope),
        "blockers": blockers,
    }


def price_action_label(bar, ema21_slope):
    if bar["c"] > bar["ema9"] > bar["ema21"] and ema21_slope >= 0.002:
        return "bullish_9_21"
    if bar["c"] > bar["ema21"] and bar["ema9"] > bar["ema21"]:
        return "constructive"
    if bar["c"] > bar["ema9"] and bar["ema9"] <= bar["ema21"]:
        return "early_reclaim"
    if bar["c"] < bar["ema21"]:
        return "below_21"
    return "neutral"


def static_signal(config, bars, index):
    if not config.get("reentry_enabled"):
        return None
    bar = bars[index]
    breakout_trigger = config.get("reentry_breakout_trigger_price")
    if breakout_trigger and bar["h"] >= breakout_trigger:
        return {
            "mode": "static_breakout",
            "trigger_time": bar["t"],
            "limit_price": float(config.get("reentry_breakout_limit_price", breakout_trigger)),
        }
    pullback_trigger = config.get("reentry_pullback_trigger_price")
    pullback_reclaim = config.get("reentry_pullback_reclaim_price")
    if pullback_trigger and pullback_reclaim:
        seen = any(item["l"] <= pullback_trigger for item in prior_window(bars, index + 1, 40))
        if seen and bar["h"] >= pullback_reclaim:
            return {
                "mode": "static_pullback_reclaim",
                "trigger_time": bar["t"],
                "limit_price": float(config.get("reentry_pullback_limit_price", pullback_reclaim)),
            }
    return None


def fill_after_signal(bars, signal, start_index):
    for bar in bars[start_index + 1 :]:
        if bar["l"] <= signal["limit_price"] <= bar["h"]:
            return {
                **signal,
                "fill_time": bar["t"],
                "fill_price": signal["limit_price"],
            }
    return {**signal, "fill_time": None, "fill_price": None}


def simulate_exit_after_entry(config, bars, entry):
    if not entry.get("fill_time"):
        return None
    start_index = next(
        (index for index, bar in enumerate(bars) if bar["t"] >= entry["fill_time"]),
        None,
    )
    if start_index is None:
        return None

    entry_price = entry["fill_price"]
    floor = entry_price * (1 - config.get("initial_stop_loss_percent", 20) / 100)
    highest_rung = 0
    trail_step = config.get("trail_trigger_step_percent", 5) / 100
    trail_below = config.get("trail_stop_below_current_percent", 2.5) / 100

    for bar in bars[start_index + 1 :]:
        current_rung = max(0, int((bar["h"] / entry_price - 1) / trail_step))
        highest_rung = max(highest_rung, current_rung)
        if highest_rung > 0:
            floor = max(floor, bar["h"] * (1 - trail_below))
        if bar["l"] <= floor:
            pnl_per_share = floor - entry_price
            return {
                "exit_time": bar["t"],
                "exit_price": floor,
                "pnl_per_share": pnl_per_share,
            }

    last = bars[-1]
    return {
        "exit_time": last["t"],
        "exit_price": last["c"],
        "pnl_per_share": last["c"] - entry_price,
        "open_at_end": True,
    }


def backtest_symbol(symbol, config, bars, market_bars, exits, cooldown_minutes):
    symbol_exits = [item for item in exits if item["symbol"] == symbol]
    results = []
    for exit_trade in symbol_exits:
        earliest = exit_trade["exit_time"] + datetime.timedelta(minutes=cooldown_minutes)
        start_index = next((i for i, bar in enumerate(bars) if bar["t"] >= earliest), None)
        if start_index is None:
            continue

        static = None
        dynamic = None
        for index in range(start_index, len(bars)):
            if static is None:
                static_candidate = static_signal(config, bars, index)
                if static_candidate:
                    static = fill_after_signal(bars, static_candidate, index)
            if dynamic is None:
                dynamic_candidate = dynamic_signal(bars, index, exit_trade, market_bars)
                if dynamic_candidate:
                    dynamic = fill_after_signal(bars, dynamic_candidate, index)
            if static and dynamic:
                break

        static_exit = simulate_exit_after_entry(config, bars, static) if static else None
        dynamic_exit = simulate_exit_after_entry(config, bars, dynamic) if dynamic else None
        qty = config.get("reentry_quantity", config.get("entry_quantity", 1))

        results.append(
            {
                "exit": exit_trade,
                "static": static,
                "static_exit": static_exit,
                "static_pnl": static_exit["pnl_per_share"] * qty
                if static_exit
                else None,
                "dynamic": dynamic,
                "dynamic_exit": dynamic_exit,
                "dynamic_pnl": dynamic_exit["pnl_per_share"] * qty
                if dynamic_exit
                else None,
            }
        )
    return results


def summarize(symbol, results):
    static_trades = [r for r in results if r.get("static") and r["static"].get("fill_time")]
    dynamic_trades = [r for r in results if r.get("dynamic") and r["dynamic"].get("fill_time")]
    static_pnl = sum(r["static_pnl"] or 0 for r in results)
    dynamic_pnl = sum(r["dynamic_pnl"] or 0 for r in results)
    return {
        "symbol": symbol,
        "exits_tested": len(results),
        "static_fills": len(static_trades),
        "dynamic_fills": len(dynamic_trades),
        "static_pnl": static_pnl,
        "dynamic_pnl": dynamic_pnl,
    }


def print_report(summaries, detailed):
    print("REENTRY BACKTEST")
    print("")
    print(
        f"{'Symbol':<6} {'Exits':>5} {'Static fills':>12} {'Dynamic fills':>14} "
        f"{'Static P/L':>12} {'Dynamic P/L':>12}"
    )
    print("-" * 70)
    for row in summaries:
        print(
            f"{row['symbol']:<6} {row['exits_tested']:>5} {row['static_fills']:>12} "
            f"{row['dynamic_fills']:>14} {dollars(row['static_pnl']):>12} "
            f"{dollars(row['dynamic_pnl']):>12}"
        )

    print("")
    for symbol, results in detailed.items():
        if not results:
            continue
        print(symbol)
        for result in results:
            exit_trade = result["exit"]
            print(
                "  exit "
                f"{exit_trade['exit_time'].isoformat()} @ {dollars(exit_trade['exit_price'])} "
                f"ledger P/L {dollars(exit_trade['realized_pl'])}"
            )
            for name in ("static", "dynamic"):
                entry = result.get(name)
                simulated_exit = result.get(f"{name}_exit")
                pnl = result.get(f"{name}_pnl")
                if not entry:
                    print(f"    {name}: no signal")
                elif not entry.get("fill_time"):
                    print(
                        f"    {name}: {entry['mode']} triggered "
                        f"{entry['trigger_time'].isoformat()} limit {dollars(entry['limit_price'])}, no fill"
                    )
                else:
                    suffix = " open at end" if simulated_exit and simulated_exit.get("open_at_end") else ""
                    print(
                        f"    {name}: {entry['mode']} fill "
                        f"{entry['fill_time'].isoformat()} @ {dollars(entry['fill_price'])}; "
                        f"exit {simulated_exit['exit_time'].isoformat()} @ "
                        f"{dollars(simulated_exit['exit_price'])}; P/L {dollars(pnl)}{suffix}"
                    )


def print_current_scan(plans):
    print("CURRENT DYNAMIC REENTRY SCAN")
    print("")
    print(
        f"{'Symbol':<6} {'Last':>8} {'Exit':>8} {'Ledger cap':>10} "
        f"{'Trigger':>10} {'Pullback':>10} {'Breakout':>10} {'9/21':>14} {'Status':>14}"
    )
    print("-" * 103)
    for plan in plans:
        if plan["status"] == "not_enough_bars":
            print(f"{plan['symbol']:<6} not enough bars")
            continue
        ledger_cap = "n/a" if math.isinf(plan["ledger_cap"]) else dollars(plan["ledger_cap"])
        print(
            f"{plan['symbol']:<6} {dollars(plan['last_price']):>8} "
            f"{dollars(plan['last_exit_price']) if plan['last_exit_price'] is not None else 'n/a':>8} {ledger_cap:>10} "
            f"{dollars(plan['next_signal_trigger']) if plan['next_signal_trigger'] else 'none':>10} "
            f"{dollars(plan['pullback_limit']):>10} {dollars(plan['breakout_limit']):>10} "
            f"{plan['price_action']:>14} "
            f"{plan['status']:>14}"
        )

    print("")
    for plan in plans:
        if plan["status"] == "not_enough_bars":
            continue
        exit_text = (
            "ledger ignored"
            if plan["ledger_ignored"]
            else f"last exit {plan['last_exit_time'].isoformat()} @ {dollars(plan['last_exit_price'])}; "
            f"ledger P/L {dollars(plan['last_trade_pl'])}"
        )
        print(plan["symbol"])
        print(
            f"  latest bar {plan['last_bar_time'].isoformat()} close {dollars(plan['last_price'])}; "
            f"{exit_text}"
        )
        if plan["status"] == "active_signal":
            limit = plan["pullback_limit"] if "pullback" in plan["mode"] else plan["breakout_limit"]
            print(f"  active {plan['mode']} reentry limit: {dollars(limit)}")
            if plan.get("market_filter_ignored"):
                print("  market filter ignored because all stock-specific signal checks passed")
        else:
            print(
                f"  no active strict signal; next trigger "
                f"{dollars(plan['next_signal_trigger']) if plan['next_signal_trigger'] else 'none'}"
            )
            print(
                f"  watch pullback reclaim above {dollars(plan['pullback_reclaim_trigger'])} "
                f"after touch near {dollars(plan['pullback_touch_trigger'])}; "
                f"limit up to {dollars(plan['pullback_limit'])}"
            )
            print(
                f"  watch breakout above {dollars(plan['breakout_trigger'])} "
                f"with limit up to {dollars(plan['breakout_limit'])}"
            )
            print(f"  blockers: {', '.join(plan['blockers']) if plan['blockers'] else 'none'}")
        print(
            f"  9/21 EMA: close {dollars(plan['last_price'])}, "
            f"ema9 {dollars(plan['ema9'])}, ema21 {dollars(plan['ema21'])}, "
            f"ema50 {dollars(plan['ema50'])}, ema21 slope {plan['ema21_slope'] * 100:.2f}%"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchers", default="config/watchers.json")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=iso_utc(datetime.datetime.now(datetime.timezone.utc)))
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--cooldown-minutes", type=int, default=30)
    parser.add_argument("--symbols", nargs="*", help="Limit to specific symbols.")
    parser.add_argument("--scan-current", action="store_true", help="Show current dynamic reentry levels.")
    parser.add_argument(
        "--ignore-ledger",
        action="store_true",
        help="For current scans, use market action only for every symbol.",
    )
    parser.add_argument(
        "--ignore-ledger-for-new",
        action="store_true",
        help="For current scans, use ledger-aware rules when history exists and market-action-only rules for new symbols.",
    )
    args = parser.parse_args()

    load_env()
    configs = load_strategy_configs(args.watchers)
    symbols = set(args.symbols or configs.keys())
    data = AlpacaData()

    fills = data.fills(args.start)
    exits = pair_completed_trades(fills, symbols)
    if not exits:
        raise SystemExit("No completed ledger exits found for the selected symbols.")

    bar_symbols = symbols if args.scan_current else {item["symbol"] for item in exits}
    bar_cache = {}
    for symbol in sorted(bar_symbols | {MARKET_SYMBOL}):
        try:
            bar_cache[symbol] = calculate_indicators(
                data.bars(symbol, args.start, args.end, args.timeframe)
            )
        except urllib.error.HTTPError as exc:
            print(f"Skipping {symbol}: HTTP {exc.code} {exc.reason}")
            bar_cache[symbol] = []
        except urllib.error.URLError as exc:
            print(f"Skipping {symbol}: {exc.reason}")
            bar_cache[symbol] = []

    market_bars = bar_cache.get(MARKET_SYMBOL, [])
    if args.scan_current:
        plans = []
        for symbol in sorted(symbols):
            bars = bar_cache.get(symbol, [])
            symbol_exits = [item for item in exits if item["symbol"] == symbol]
            use_market_only = args.ignore_ledger or (
                args.ignore_ledger_for_new and not symbol_exits
            )
            if not symbol_exits and not use_market_only:
                continue
            plans.append(
                current_dynamic_plan(
                    symbol,
                    bars,
                    market_bars,
                    symbol_exits[-1] if symbol_exits else None,
                    ignore_ledger=use_market_only,
                )
            )
        print_current_scan(plans)
        return

    summaries = []
    detailed = {}
    for symbol in sorted(symbols):
        bars = bar_cache.get(symbol, [])
        if not bars:
            continue
        results = backtest_symbol(
            symbol,
            configs.get(symbol, default_strategy_config(symbol)),
            bars,
            market_bars,
            exits,
            args.cooldown_minutes,
        )
        detailed[symbol] = results
        summaries.append(summarize(symbol, results))

    print_report(summaries, detailed)


if __name__ == "__main__":
    main()
