import argparse
import datetime
import json
import math
import os
import socket
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_CONFIG_PATH = "traderbot/core_strategy_engine/strategies/configs/strategy_config.json"
DEFAULT_STATE_PATH = "runtime/state/strategy_state.json"
MARKET_SYMBOL = "QQQ"


def load_env(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig") as file:
        return json.load(file)


def save_json(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)
        file.write("\n")


class AlpacaClient:
    def __init__(self):
        self.trade_base_url = os.environ["ALPACA_BASE_URL"].rstrip("/")
        self.data_base_url = os.environ.get(
            "ALPACA_DATA_URL", "https://data.alpaca.markets/v2"
        ).rstrip("/")
        self.headers = {
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        }

    def request(self, method, url, payload=None, retries=3):
        data = None
        headers = dict(self.headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error = None
        for attempt in range(1, retries + 1):
            request = urllib.request.Request(
                url, data=data, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                    raise
                last_error = exc
            except (
                ConnectionResetError,
                TimeoutError,
                socket.timeout,
                urllib.error.URLError,
            ) as exc:
                if attempt == retries:
                    raise
                last_error = exc

            time.sleep(min(2 * attempt, 5))

        raise last_error

    def trading(self, method, path, payload=None):
        return self.request(method, f"{self.trade_base_url}{path}", payload)

    def data(self, method, path, payload=None):
        return self.request(method, f"{self.data_base_url}{path}", payload)

    def order(self, order_id):
        return self.trading("GET", f"/orders/{order_id}")

    def clock(self):
        return self.trading("GET", "/clock")

    def position(self, symbol):
        try:
            return self.trading("GET", f"/positions/{symbol}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def latest_trade_price(self, symbol):
        query = urllib.parse.urlencode({"feed": "iex"})
        response = self.data("GET", f"/stocks/{symbol}/trades/latest?{query}")
        return float(response["trade"]["p"])

    def stock_bars(self, symbol, start, end, timeframe):
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
            payload = self.data("GET", f"/stocks/bars?{query}") or {}
            bars.extend(payload.get("bars", {}).get(symbol, []))
            page_token = payload.get("next_page_token")
            if not page_token:
                return bars

    def open_stop_orders(self, symbol):
        query = urllib.parse.urlencode(
            {"status": "open", "symbols": symbol, "nested": "false"}
        )
        orders = self.trading("GET", f"/orders?{query}") or []
        return [
            order
            for order in orders
            if order.get("symbol") == symbol
            and order.get("side") == "sell"
            and order.get("type") == "stop"
        ]

    def cancel_order(self, order_id):
        try:
            self.trading("DELETE", f"/orders/{order_id}")
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 422):
                raise

    def submit_order(self, payload):
        return self.trading("POST", "/orders", payload)

    def replace_order(self, order_id, payload):
        return self.trading("PATCH", f"/orders/{order_id}", payload)


def dollars(value):
    return f"{value:.2f}"


def parse_alpaca_time(value):
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_utc(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def calculate_indicators(raw_bars):
    bars = []
    ema9 = ema21 = ema50 = None
    prev_close = None
    session_date = None
    session_pv = 0.0
    session_volume = 0.0
    true_ranges = []
    volumes = []

    for raw in raw_bars:
        timestamp = parse_alpaca_time(raw["t"])
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


def prior_window(bars, index, size):
    return bars[max(0, index - size) : index]


def market_ok_at(market_bars, timestamp):
    prior = [bar for bar in market_bars if bar["t"] <= timestamp]
    if len(prior) < 6:
        return True
    bar = prior[-1]
    previous = prior[-6]
    ema21_slope = (bar["ema21"] - previous["ema21"]) / previous["ema21"]
    return bar["c"] > bar["vwap"] and bar["c"] > bar["ema21"] and ema21_slope >= 0


def allowed_ledger_price(exit_price, realized_pl=0, strong=False):
    if realized_pl >= 0:
        premium = 1.025 if strong else 1.01
    else:
        premium = 0.985
    return exit_price * premium


def reward_risk_ok(entry_price, target_price, atr, minimum=1.5):
    stop_price = max(entry_price * 0.98, entry_price - atr)
    risk = entry_price - stop_price
    reward = target_price - entry_price
    return risk > 0 and reward / risk >= minimum


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


def live_bar_context(client, symbol, config):
    now = datetime.datetime.now(datetime.timezone.utc)
    lookback_days = config.get("dynamic_lookback_days", 7)
    start = iso_utc(now - datetime.timedelta(days=lookback_days))
    end = iso_utc(now)
    timeframe = config.get("dynamic_timeframe", "5Min")
    bars = calculate_indicators(client.stock_bars(symbol, start, end, timeframe))
    market_bars = calculate_indicators(client.stock_bars(MARKET_SYMBOL, start, end, timeframe))
    return bars, market_bars


def dynamic_entry_plan(symbol, bars, market_bars, exit_trade=None, ignore_ledger=False):
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
            exit_trade["exit_price"], exit_trade.get("realized_pl", 0), strong=strong_volume
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
    same_day_exit = (
        exit_trade
        and exit_trade.get("exit_time")
        and bar["t"].date() == exit_trade["exit_time"].date()
    )
    above_exit = (
        True
        if ignore_ledger or not exit_trade
        else exit_trade.get("realized_pl", 0) < 0 or bar["c"] >= exit_trade["exit_price"]
    )
    no_same_day_loss_reentry = not (
        exit_trade and exit_trade.get("realized_pl", 0) < 0 and same_day_exit
    )
    no_chase = bar["c"] <= bar["ema21"] + 0.75 * atr
    trend_base = bar["c"] > bar["vwap"] and bar["c"] > bar["ema21"] and bar["ema21"] >= bar["ema50"]

    touched_pullback = previous["l"] <= pullback_touch_trigger
    reclaimed = bar["c"] > pullback_reclaim_trigger
    reclaim_trend_ok = trend_base and ema21_slope >= 0.002 and no_chase
    vwap_stability = sum(1 for item in bars[index - 2 : index + 1] if item["c"] > item["vwap"]) >= 2
    pullback_signal = (
        market_ok
        and above_exit
        and no_same_day_loss_reentry
        and touched_pullback
        and reclaimed
        and reclaim_trend_ok
        and vwap_stability
        and bar["volume_ratio"] >= 1.15
        and pullback_limit <= ledger_cap
        and reward_risk_ok(pullback_limit, recent_high, atr, minimum=1.5)
    )

    breakout = bar["c"] > recent_high and bar["volume_ratio"] >= 1.3
    breakout_trend_ok = (
        bar["c"] > bar["vwap"]
        and bar["c"] > bar["ema9"] > bar["ema21"]
        and bar["ema21"] >= bar["ema50"]
        and ema21_slope >= 0.002
    )
    breakout_signal = (
        market_ok
        and above_exit
        and no_same_day_loss_reentry
        and breakout
        and breakout_trend_ok
        and bar["volume_ratio"] >= 1.5
        and breakout_limit <= ledger_cap
        and reward_risk_ok(breakout_limit, breakout_limit + 2 * atr, atr, minimum=1.5)
    )

    mode = None
    limit_price = None
    if pullback_signal:
        mode = "dynamic_pullback_reclaim"
        limit_price = pullback_limit
    elif breakout_signal:
        mode = "dynamic_breakout_continuation"
        limit_price = breakout_limit

    checks = {
        "market_ok": market_ok,
        "above_exit": above_exit,
        "no_same_day_loss_reentry": no_same_day_loss_reentry,
        "above_ema9": bar["c"] > bar["ema9"],
        "ema9_above_ema21": bar["ema9"] > bar["ema21"],
        "ema21_slope_ok": ema21_slope >= 0.002,
        "no_chase": no_chase,
        "pullback_volume_ok": bar["volume_ratio"] >= 1.15,
        "breakout_volume_ok": bar["volume_ratio"] >= 1.5,
        "trend_ok": trend_base,
        "touched_pullback": touched_pullback,
        "breakout_now": bar["c"] > recent_high,
        "reward_risk_ok": reward_risk_ok(
            pullback_limit if mode != "dynamic_breakout_continuation" else breakout_limit,
            recent_high if mode != "dynamic_breakout_continuation" else breakout_limit + 2 * atr,
            atr,
            minimum=1.5,
        ),
    }

    return {
        "symbol": symbol,
        "status": "active_signal" if mode else "watch",
        "mode": mode,
        "limit_price": limit_price,
        "last_bar_time": bar["t"].isoformat(),
        "last_price": bar["c"],
        "ledger_ignored": ignore_ledger or not exit_trade,
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
        "blockers": [name for name, ok in checks.items() if not ok],
    }


def serializable_plan(plan):
    cleaned = dict(plan)
    if math.isinf(cleaned.get("ledger_cap", math.inf)):
        cleaned["ledger_cap"] = None
    return cleaned


def reset_managed_position_state(state):
    for key in (
        "active_stop_order_id",
        "active_stop_price",
        "active_stop_qty",
        "entry_fill_price",
        "filled_ladder_steps",
        "floor_price",
        "highest_trail_rung",
        "ladder_order_ids",
    ):
        state.pop(key, None)


def reset_entry_tracking_state(state):
    for key in (
        "current_entry_order_id",
        "managed_entry_order_id",
        "reentry_order_id",
        "reentry_reason",
    ):
        state.pop(key, None)


def position_quantity(position):
    if not position:
        return 0
    return int(float(position["qty"]))


def order_is_open(order):
    return order and order.get("status") in (
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
    )


def record_exit_from_order(state, order):
    if not order or order.get("status") != "filled":
        return None

    state["last_exit_order_id"] = order["id"]
    if order.get("filled_avg_price"):
        state["last_exit_price"] = float(order["filled_avg_price"])
    if order.get("filled_at"):
        state["last_exit_at"] = order.get("filled_at")
    return {
        "last_exit_order_id": state.get("last_exit_order_id"),
        "last_exit_price": state.get("last_exit_price"),
        "last_exit_at": state.get("last_exit_at"),
    }


def reconcile_flat_position_state(client, symbol, state):
    position = client.position(symbol)
    qty = position_quantity(position)
    if qty > 0:
        return {"position_qty": qty, "cleared_stale_state": False}

    active_stop_id = state.get("active_stop_order_id")
    active_stop_order = None
    exit_details = None
    if active_stop_id:
        try:
            active_stop_order = client.order(active_stop_id)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise

    if order_is_open(active_stop_order):
        return {
            "position_qty": 0,
            "cleared_stale_state": False,
            "waiting_for_stop_order_id": active_stop_id,
            "stop_order_status": active_stop_order.get("status"),
        }

    exit_details = record_exit_from_order(state, active_stop_order)

    canceled_stop_order_ids = []
    for order in client.open_stop_orders(symbol):
        client.cancel_order(order["id"])
        canceled_stop_order_ids.append(order["id"])

    had_managed_state = any(
        key in state
        for key in (
            "active_stop_order_id",
            "active_stop_price",
            "active_stop_qty",
            "entry_fill_price",
            "floor_price",
        )
    )
    reset_managed_position_state(state)
    reset_entry_tracking_state(state)
    return {
        "position_qty": 0,
        "cleared_stale_state": had_managed_state,
        "canceled_stop_order_ids": canceled_stop_order_ids,
        **(exit_details or {}),
    }


def update_stop_order(client, symbol, qty, stop_price, state):
    rounded_stop = dollars(stop_price)
    if state.get("active_stop_price") == rounded_stop and state.get("active_stop_qty") == qty:
        return None

    active_stop_id = state.get("active_stop_order_id")
    if active_stop_id:
        try:
            order = client.replace_order(
                active_stop_id,
                {
                    "qty": str(qty),
                    "stop_price": rounded_stop,
                    "time_in_force": "gtc",
                },
            )
            state["active_stop_order_id"] = order["id"]
            state["active_stop_price"] = rounded_stop
            state["active_stop_qty"] = qty
            return order
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 422):
                raise

    for order in client.open_stop_orders(symbol):
        client.cancel_order(order["id"])

    order = client.submit_order(
        {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "stop_price": rounded_stop,
            "time_in_force": "gtc",
        }
    )
    state["active_stop_order_id"] = order["id"]
    state["active_stop_price"] = rounded_stop
    state["active_stop_qty"] = qty
    return order


def update_dynamic_pending_order(client, config, state, order_id, plan):
    order = client.order(order_id)
    if order_is_open(order):
        desired_limit = plan.get("limit_price")
        if (
            config.get("dynamic_replace_open_orders", True)
            and plan.get("status") == "active_signal"
            and desired_limit
        ):
            current_limit = float(order.get("limit_price") or 0)
            min_change = config.get("dynamic_replace_min_change_percent", 0.25) / 100
            if current_limit <= 0 or abs(desired_limit / current_limit - 1) >= min_change:
                replaced = client.replace_order(
                    order_id,
                    {
                        "qty": order["qty"],
                        "limit_price": dollars(desired_limit),
                        "time_in_force": order.get("time_in_force", "day"),
                    },
                )
                state["current_entry_order_id"] = replaced["id"]
                state["reentry_order_id"] = replaced["id"]
                return {
                    "status": "dynamic_entry_order_replaced",
                    "old_order_id": order_id,
                    "new_order_id": replaced["id"],
                    "limit_price": replaced.get("limit_price"),
                    "mode": plan.get("mode"),
                }
        return {
            "status": "waiting_for_reentry_fill",
            "reentry_order_id": order_id,
            "reentry_order_status": order.get("status"),
            "dynamic_status": plan.get("status"),
        }

    if order.get("status") == "filled":
        return {
            "status": "reentry_filled_waiting_for_management",
            "reentry_order_id": order_id,
        }

    state.pop("reentry_order_id", None)
    state.pop("current_entry_order_id", None)
    return None


def build_exit_trade_from_state(state):
    if "last_exit_price" not in state:
        return None
    return {
        "exit_price": float(state["last_exit_price"]),
        "exit_time": parse_alpaca_time(state.get("last_exit_at")),
        "realized_pl": float(state.get("last_trade_pl", 0)),
    }


def submit_dynamic_entry(client, config, state, plan, reason_prefix):
    symbol = config["symbol"]
    qty = config.get("reentry_quantity", config["entry_quantity"])
    order = client.submit_order(
        {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "limit_price": dollars(plan["limit_price"]),
            "time_in_force": config.get("dynamic_entry_time_in_force", "day"),
        }
    )
    state["current_entry_order_id"] = order["id"]
    state["reentry_order_id"] = order["id"]
    state["reentry_reason"] = f"{reason_prefix}_{plan['mode']}"
    state["reentries_today"] = state.get("reentries_today", 0) + 1
    reset_managed_position_state(state)
    return {
        "status": "dynamic_reentry_order_submitted",
        "reason": state["reentry_reason"],
        "limit_price": order.get("limit_price"),
        "reentry_order_id": order["id"],
        "dynamic_plan": serializable_plan(plan),
    }


def reset_entry_day_if_needed(state):
    today_key = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if state.get("reentry_day") != today_key:
        state["reentry_day"] = today_key
        state["reentries_today"] = 0
        state["reentry_pullback_seen"] = False


def dynamic_entry_limit_reached(config, state):
    max_per_day = config.get("reentry_max_per_day", 1)
    return state.get("reentries_today", 0) >= max_per_day


def handle_dynamic_flat_entry(client, config, state, exit_trade=None):
    symbol = config["symbol"]
    reset_entry_day_if_needed(state)
    bars, market_bars = live_bar_context(client, symbol, config)
    ignore_ledger = not exit_trade
    plan = dynamic_entry_plan(symbol, bars, market_bars, exit_trade, ignore_ledger=ignore_ledger)
    state["dynamic_entry_plan"] = serializable_plan(plan)

    reentry_order_id = state.get("reentry_order_id")
    if reentry_order_id:
        pending = update_dynamic_pending_order(client, config, state, reentry_order_id, plan)
        if pending:
            return {**pending, "dynamic_plan": serializable_plan(plan)}

    if plan.get("status") != "active_signal":
        return {
            "status": "dynamic_entry_waiting_for_signal",
            "dynamic_plan": serializable_plan(plan),
        }

    if dynamic_entry_limit_reached(config, state):
        return {
            "status": "dynamic_entry_limit_reached",
            "reentries_today": state.get("reentries_today", 0),
            "dynamic_plan": serializable_plan(plan),
        }

    return submit_dynamic_entry(
        client,
        config,
        state,
        plan,
        "new_entry" if ignore_ledger else "reentry",
    )


def refresh_dynamic_plan_only(client, config, state):
    if not (config.get("dynamic_reentry_enabled") or config.get("dynamic_entry_enabled")):
        return None

    exit_trade = build_exit_trade_from_state(state)
    if not exit_trade and not config.get("dynamic_entry_enabled"):
        return None

    bars, market_bars = live_bar_context(client, config["symbol"], config)
    plan = dynamic_entry_plan(
        config["symbol"],
        bars,
        market_bars,
        exit_trade,
        ignore_ledger=not exit_trade,
    )
    state["dynamic_entry_plan"] = serializable_plan(plan)
    return serializable_plan(plan)


def handle_reentry(client, config, state):
    symbol = config["symbol"]
    if not config.get("reentry_enabled"):
        return {"status": "no_long_position", "qty": 0}

    stop_order_id = state.get("active_stop_order_id")
    if not stop_order_id and not state.get("last_exit_at"):
        return {"status": "no_long_position_no_reentry_stop", "qty": 0}

    if stop_order_id:
        stop_order = client.order(stop_order_id)
        if stop_order.get("status") != "filled":
            return {
                "status": "no_long_position_waiting_for_stop_resolution",
                "stop_order_status": stop_order.get("status"),
            }
        record_exit_from_order(state, stop_order)
        reset_managed_position_state(state)
        reset_entry_tracking_state(state)

    exit_price = float(state["last_exit_price"])
    exit_at = parse_alpaca_time(state.get("last_exit_at"))
    now_at = datetime.datetime.now(datetime.timezone.utc)
    reset_entry_day_if_needed(state)

    cooldown_seconds = config.get("reentry_cooldown_seconds", 600)
    if exit_at and (now_at - exit_at).total_seconds() < cooldown_seconds:
        return {
            "status": "reentry_cooldown",
            "exit_price": exit_price,
            "exit_at": state.get("last_exit_at"),
            "cooldown_seconds": cooldown_seconds,
        }

    if dynamic_entry_limit_reached(config, state):
        return {
            "status": "reentry_limit_reached",
            "reentries_today": state.get("reentries_today", 0),
        }

    if config.get("dynamic_reentry_enabled"):
        return handle_dynamic_flat_entry(
            client,
            config,
            state,
            exit_trade=build_exit_trade_from_state(state),
        )

    reentry_order_id = state.get("reentry_order_id")
    if reentry_order_id:
        reentry_order = client.order(reentry_order_id)
        if reentry_order.get("status") in ("new", "accepted", "pending_new", "partially_filled"):
            return {
                "status": "waiting_for_reentry_fill",
                "reentry_order_id": reentry_order_id,
                "reentry_order_status": reentry_order.get("status"),
            }
        if reentry_order.get("status") == "filled":
            return {
                "status": "reentry_filled_waiting_for_management",
                "reentry_order_id": reentry_order_id,
            }
        state.pop("reentry_order_id", None)

    current_price = client.latest_trade_price(symbol)
    reason = None
    limit_price = None

    pullback_trigger = config.get("reentry_pullback_trigger_price")
    if pullback_trigger and current_price <= pullback_trigger:
        state["reentry_pullback_seen"] = True

    breakout_trigger = config.get("reentry_breakout_trigger_price")
    if breakout_trigger and current_price >= breakout_trigger:
        reason = "breakout"
        limit_price = config.get("reentry_breakout_limit_price", current_price)

    pullback_reclaim = config.get("reentry_pullback_reclaim_price")
    if (
        not reason
        and state.get("reentry_pullback_seen")
        and pullback_reclaim
        and current_price >= pullback_reclaim
    ):
        reason = "pullback_reclaim"
        limit_price = config.get("reentry_pullback_limit_price", current_price)

    if not reason:
        return {
            "status": "reentry_waiting_for_trigger",
            "current_price": current_price,
            "exit_price": exit_price,
            "pullback_seen": state.get("reentry_pullback_seen", False),
        }

    order = client.submit_order(
        {
            "symbol": symbol,
            "qty": str(config.get("reentry_quantity", config["entry_quantity"])),
            "side": "buy",
            "type": "limit",
            "limit_price": dollars(limit_price),
            "time_in_force": "day",
        }
    )

    state["current_entry_order_id"] = order["id"]
    state["reentry_order_id"] = order["id"]
    state["reentry_reason"] = reason
    state["reentries_today"] = state.get("reentries_today", 0) + 1
    reset_managed_position_state(state)

    return {
        "status": "reentry_order_submitted",
        "reason": reason,
        "current_price": current_price,
        "limit_price": order.get("limit_price"),
        "reentry_order_id": order["id"],
    }


def run_once(client, config, state, clock=None):
    symbol = config["symbol"]
    if clock is None:
        clock = client.clock()
    if not clock.get("is_open"):
        reconciliation = reconcile_flat_position_state(client, symbol, state)
        dynamic_plan = None
        dynamic_plan_error = None
        try:
            dynamic_plan = refresh_dynamic_plan_only(client, config, state)
        except Exception as exc:
            dynamic_plan_error = f"{type(exc).__name__}: {exc}"
        return {
            "status": "market_closed_sleeping",
            "symbol": symbol,
            "timestamp": clock.get("timestamp"),
            "next_open": clock.get("next_open"),
            "reconciliation": reconciliation,
            "dynamic_plan": dynamic_plan,
            "dynamic_plan_error": dynamic_plan_error,
        }

    entry_order_id = state.get("current_entry_order_id") or config.get("entry_order_id")
    entry_order = client.order(entry_order_id) if entry_order_id else None

    if not entry_order_id:
        position = client.position(symbol)
        qty = position_quantity(position)
        if qty <= 0:
            if config.get("dynamic_entry_enabled"):
                return handle_dynamic_flat_entry(client, config, state, exit_trade=None)
            return {"status": "no_entry_order_configured", "symbol": symbol}
        state.setdefault("entry_fill_price", float(position["avg_entry_price"]))
        state.setdefault("highest_trail_rung", 0)
        state.setdefault("filled_ladder_steps", [])
        state.setdefault("ladder_order_ids", {})

    if entry_order and entry_order.get("status") != "filled":
        if config.get("reentry_enabled") and entry_order.get("status") in (
            "canceled",
            "expired",
            "rejected",
        ):
            state.pop("current_entry_order_id", None)
            state.pop("reentry_order_id", None)
            return handle_reentry(client, config, state)
        return {
            "status": "waiting_for_entry_fill",
            "entry_order_status": entry_order.get("status"),
        }

    fill_price = (
        float(entry_order["filled_avg_price"])
        if entry_order
        else float(state["entry_fill_price"])
    )
    if (
        entry_order_id
        and entry_order_id != config.get("entry_order_id")
        and state.get("managed_entry_order_id") != entry_order_id
    ):
        reset_managed_position_state(state)
        state["managed_entry_order_id"] = entry_order_id

    state.setdefault("entry_fill_price", fill_price)
    state.setdefault("highest_trail_rung", 0)
    state.setdefault("filled_ladder_steps", [])
    state.setdefault("ladder_order_ids", {})

    position = client.position(symbol)
    qty = position_quantity(position)
    if qty <= 0:
        reconciliation = reconcile_flat_position_state(client, symbol, state)
        if reconciliation.get("waiting_for_stop_order_id"):
            return {
                "status": "no_long_position_waiting_for_stop_resolution",
                **reconciliation,
            }
        return handle_reentry(client, config, state)

    current_price = client.latest_trade_price(symbol)
    base_floor = fill_price * (1 - config["initial_stop_loss_percent"] / 100)
    trail_step = config["trail_trigger_step_percent"] / 100
    current_rung = int((current_price / fill_price - 1) / trail_step)
    current_rung = max(0, current_rung)

    if current_rung > state["highest_trail_rung"]:
        state["highest_trail_rung"] = current_rung

    if state["highest_trail_rung"] > 0:
        candidate_floor = current_price * (
            1 - config["trail_stop_below_current_percent"] / 100
        )
    else:
        candidate_floor = base_floor

    previous_floor = float(state.get("floor_price", 0))
    floor_price = max(previous_floor, base_floor, candidate_floor)
    state["floor_price"] = floor_price

    stop_order = update_stop_order(client, symbol, qty, floor_price, state)

    ladder_orders = []
    for drop_step in config["ladder_drop_steps_percent"]:
        if drop_step in state["filled_ladder_steps"]:
            continue

        trigger_price = fill_price * (1 - drop_step / 100)
        if current_price <= trigger_price:
            order = client.submit_order(
                {
                    "symbol": symbol,
                    "qty": str(config["ladder_buy_quantity"]),
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "day",
                }
            )
            state["filled_ladder_steps"].append(drop_step)
            state["ladder_order_ids"][str(drop_step)] = order["id"]
            ladder_orders.append(order)

    return {
        "status": "managed",
        "entry_fill_price": fill_price,
        "current_price": current_price,
        "position_qty": qty,
        "floor_price": floor_price,
        "highest_trail_rung": state["highest_trail_rung"],
        "updated_stop_order": stop_order["id"] if stop_order else None,
        "new_ladder_orders": [order["id"] for order in ladder_orders],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Run continuously.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Strategy config path.")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="Strategy state path.")
    args = parser.parse_args()

    load_env()
    config = load_json(args.config, {})
    state = load_json(args.state, {})
    client = AlpacaClient()

    while True:
        try:
            result = run_once(client, config, state)
            save_json(args.state, state)
        except Exception as exc:
            result = {
                "status": "temporary_error",
                "symbol": config.get("symbol"),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "retrying": bool(args.watch),
            }
            print(traceback.format_exc(), flush=True)

        print(json.dumps(result, indent=2, sort_keys=True), flush=True)

        if not args.watch:
            return 1 if result.get("status") == "temporary_error" else 0

        if result.get("status") == "market_closed_sleeping":
            sleep_seconds = config.get("market_closed_poll_seconds", 900)
            next_open = result.get("next_open")
            if next_open:
                next_open_at = datetime.datetime.fromisoformat(next_open)
                now_at = datetime.datetime.now(next_open_at.tzinfo)
                seconds_until_open = (next_open_at - now_at).total_seconds()
                if seconds_until_open > 0:
                    sleep_seconds = min(sleep_seconds, max(5, seconds_until_open + 2))
        else:
            sleep_seconds = (
                config.get("error_poll_seconds", 30)
                if result.get("status") == "temporary_error"
                else config.get("poll_seconds", 30)
            )

        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
