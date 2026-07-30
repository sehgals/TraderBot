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
DEFAULT_MIN_CASH_BALANCE_PERCENT = 20
RISK_PROFILE_DEFAULTS = {
    "index_etf": {
        "initial_stop_mode": "atr_or_percent",
        "initial_stop_atr_multiple": 2.5,
        "initial_stop_min_percent": 4,
        "initial_stop_max_percent": 20,
        "ladder_mode": "percent",
        "ladder_drop_steps_percent": [4],
        "max_ladder_count": 1,
        "ladder_requires_market_ok": True,
        "trail_tiers": [
            {"gain_percent": 5, "trail_stop_below_current_percent": 2.5},
            {"gain_percent": 10, "trail_stop_below_current_percent": 2.0},
            {"gain_percent": 15, "trail_stop_below_current_percent": 1.5},
        ],
    },
    "large_cap": {
        "initial_stop_mode": "percent",
        "max_ladder_count": 1,
    },
    "high_vol_growth": {
        "adaptive_ladder_enabled": True,
        "initial_stop_mode": "percent",
        "max_ladder_count": 2,
        "ladder_size_fraction": 0.5,
        "min_ladder_quantity": 1,
        "volatility_ladder_scaling": True,
        "market_ladder_scaling": True,
        "max_ladder_notional_percent": 5,
        "max_ladder_position_multiple": 1.5,
    },
    "speculative": {
        "initial_stop_mode": "percent",
        "ladder_drop_steps_percent": [],
        "max_ladder_count": 0,
    },
}


def risk_setting(config, key, default=None):
    profile = RISK_PROFILE_DEFAULTS.get(config.get("risk_profile"), {})
    return config.get(key, profile.get(key, default))


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

    def calendar(self, start, end):
        query = urllib.parse.urlencode({"start": start, "end": end})
        return self.trading("GET", f"/calendar?{query}") or []

    def account(self):
        return self.trading("GET", "/account")

    def portfolio_history(self, period="1A", timeframe="1D"):
        query = urllib.parse.urlencode({"period": period, "timeframe": timeframe})
        return self.trading("GET", f"/account/portfolio/history?{query}") or {}

    def fills(self, after, until=None):
        fills = []
        page_token = None
        seen_page_tokens = set()
        while True:
            params = {
                "activity_types": "FILL",
                "after": after,
                "direction": "asc",
                "page_size": "100",
            }
            if until:
                params["until"] = until
            if page_token:
                params["page_token"] = page_token
            query = urllib.parse.urlencode(params)
            payload = self.trading("GET", f"/account/activities?{query}") or []
            if isinstance(payload, dict):
                page = payload.get("activities", [])
                fills.extend(page)
                page_token = payload.get("next_page_token")
            else:
                page = payload
                fills.extend(page)
                # Alpaca's activities endpoint normally returns a bare list.  In
                # that response shape the id of the last activity is the token
                # for the next page; no next_page_token field is supplied.
                page_token = page[-1].get("id") if len(page) == 100 else None
            if not page_token:
                return fills
            if page_token in seen_page_tokens:
                return fills
            seen_page_tokens.add(page_token)

    def positions(self):
        return self.trading("GET", "/positions") or []

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

    def open_orders(self):
        query = urllib.parse.urlencode({"status": "open", "nested": "false"})
        return self.trading("GET", f"/orders?{query}") or []

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


def dynamic_entry_notional(config, plan):
    if plan.get("market_filter_ignored"):
        return float(config.get("dynamic_market_filter_ignored_notional", 2500))
    return float(config.get("dynamic_entry_notional", 5000))


def dynamic_entry_quantity(config, plan):
    limit_price = float(plan["limit_price"])
    notional = dynamic_entry_notional(config, plan)
    return max(1, math.floor(notional / limit_price))


def cash_reserve_percent(config):
    return float(
        config.get("min_cash_balance_percent", DEFAULT_MIN_CASH_BALANCE_PERCENT)
    )


def cash_protected_quantity(client, config, desired_qty, estimated_price):
    reserve_percent = max(0.0, cash_reserve_percent(config))
    account = client.account()
    cash = float(account.get("cash", 0))
    buying_power = float(account.get("buying_power", cash))
    equity = float(account.get("equity", 0))
    min_cash_balance = equity * reserve_percent / 100
    available_notional = max(0, cash - min_cash_balance)
    max_qty = math.floor(available_notional / estimated_price)
    qty = max(0, min(desired_qty, max_qty))
    estimated_notional = qty * estimated_price
    return qty, {
        "cash_reserve_enforced": True,
        "margin_disabled": True,
        "min_cash_balance_percent": reserve_percent,
        "cash": cash,
        "buying_power": buying_power,
        "equity": equity,
        "min_cash_balance": min_cash_balance,
        "available_notional": available_notional,
        "requested_qty": desired_qty,
        "requested_notional": desired_qty * estimated_price,
        "adjusted_qty": qty,
        "estimated_order_notional": estimated_notional,
        "estimated_cash_after_order": cash - estimated_notional,
    }


def cash_available_share_count(client, config, estimated_price):
    account = client.account()
    cash = float(account.get("cash", 0))
    buying_power = float(account.get("buying_power", cash))
    equity = float(account.get("equity", 0))
    reserve_percent = max(0.0, cash_reserve_percent(config))
    min_cash_balance = equity * reserve_percent / 100
    available_notional = max(0, cash - min_cash_balance)
    return math.floor(available_notional / estimated_price), {
        "cash": cash,
        "buying_power": buying_power,
        "margin_disabled": True,
        "equity": equity,
        "min_cash_balance": min_cash_balance,
        "available_notional": available_notional,
        "estimated_price": estimated_price,
    }


def max_symbol_quantity_from_caps(client, config, current_qty, estimated_price):
    max_qty = None
    max_total_qty = risk_setting(config, "max_total_position_qty")
    if max_total_qty not in (None, ""):
        max_qty = int(float(max_total_qty))

    max_notional_percent = risk_setting(config, "max_symbol_notional_percent")
    if max_notional_percent not in (None, "", 0):
        account = client.account()
        equity = float(account.get("equity", 0))
        notional_cap = equity * float(max_notional_percent) / 100
        cap_qty = math.floor(notional_cap / estimated_price)
        max_qty = cap_qty if max_qty is None else min(max_qty, cap_qty)

    if max_qty is None:
        return None
    return max(0, max_qty - max(0, int(float(current_qty or 0))))


def apply_order_risk_caps(client, config, desired_qty, estimated_price, current_qty=0):
    capped_qty = int(desired_qty)
    cap_remaining_qty = max_symbol_quantity_from_caps(
        client, config, current_qty, estimated_price
    )
    if cap_remaining_qty is not None:
        capped_qty = min(capped_qty, cap_remaining_qty)
    return max(0, capped_qty), {
        "risk_caps_enforced": cap_remaining_qty is not None,
        "cap_remaining_qty": cap_remaining_qty,
        "requested_qty_before_caps": desired_qty,
        "adjusted_qty_after_caps": max(0, capped_qty),
        "current_position_qty": current_qty,
    }


def apply_ladder_risk_caps(client, config, state, desired_qty, estimated_price, current_qty=0):
    capped_qty = int(desired_qty)
    base_qty = int(float(state.get("base_position_qty") or config.get("entry_quantity", 0) or 0))
    ladder_qty_so_far = int(float(state.get("ladder_filled_qty", 0) or 0))
    ladder_notional_so_far = float(state.get("ladder_filled_notional", 0) or 0)

    max_ladder_notional = None
    max_ladder_notional_percent = risk_setting(config, "max_ladder_notional_percent")
    if max_ladder_notional_percent not in (None, "", 0):
        account = client.account()
        equity = float(account.get("equity", 0))
        max_ladder_notional = equity * float(max_ladder_notional_percent) / 100
        remaining_notional = max(0, max_ladder_notional - ladder_notional_so_far)
        capped_qty = min(capped_qty, math.floor(remaining_notional / estimated_price))

    max_position_qty = None
    max_ladder_position_multiple = risk_setting(config, "max_ladder_position_multiple")
    if max_ladder_position_multiple not in (None, "", 0) and base_qty > 0:
        max_position_qty = math.floor(base_qty * float(max_ladder_position_multiple))
        capped_qty = min(capped_qty, max(0, max_position_qty - int(float(current_qty or 0))))

    return max(0, capped_qty), {
        "ladder_caps_enforced": max_ladder_notional is not None or max_position_qty is not None,
        "base_position_qty": base_qty,
        "current_position_qty": current_qty,
        "ladder_qty_so_far": ladder_qty_so_far,
        "ladder_notional_so_far": ladder_notional_so_far,
        "max_ladder_notional": max_ladder_notional,
        "max_ladder_position_qty": max_position_qty,
        "requested_qty_before_ladder_caps": desired_qty,
        "adjusted_qty_after_ladder_caps": max(0, capped_qty),
    }


def risk_context_needed(config):
    return (
        risk_setting(config, "initial_stop_mode", "percent") != "percent"
        or risk_setting(config, "ladder_mode", "percent") == "atr"
        or bool(risk_setting(config, "ladder_requires_market_ok", False))
    )


def risk_context(client, config):
    now = datetime.datetime.now(datetime.timezone.utc)
    lookback_days = config.get("dynamic_lookback_days", 7)
    start = iso_utc(now - datetime.timedelta(days=lookback_days))
    end = iso_utc(now)
    timeframe = config.get("dynamic_timeframe", "5Min")
    bars = calculate_indicators(client.stock_bars(config["symbol"], start, end, timeframe))
    market_bars = calculate_indicators(client.stock_bars(MARKET_SYMBOL, start, end, timeframe))
    return {
        "bars": bars,
        "market_bars": market_bars,
        "latest_bar": bars[-1] if bars else None,
        "market_ok": market_ok_at(market_bars, bars[-1]["t"]) if bars else True,
    }


def initial_floor_price(config, fill_price, context=None):
    percent_floor = fill_price * (1 - config["initial_stop_loss_percent"] / 100)
    mode = risk_setting(config, "initial_stop_mode", "percent")
    if mode == "percent":
        return percent_floor

    latest_bar = (context or {}).get("latest_bar")
    atr = float((latest_bar or {}).get("atr14") or 0)
    if atr <= 0:
        return percent_floor

    atr_floor = fill_price - atr * float(risk_setting(config, "initial_stop_atr_multiple", 2.5))
    min_percent = float(risk_setting(config, "initial_stop_min_percent", 0) or 0)
    max_percent = float(risk_setting(config, "initial_stop_max_percent", config["initial_stop_loss_percent"]) or 0)
    floors = [atr_floor]
    if min_percent > 0:
        floors.append(fill_price * (1 - min_percent / 100))
    floor_price = min(floors)
    if max_percent > 0:
        floor_price = max(floor_price, fill_price * (1 - max_percent / 100))
    return floor_price


def trail_below_current_percent(config, highest_trail_rung):
    tiers = risk_setting(config, "trail_tiers", []) or []
    if not tiers:
        return config["trail_stop_below_current_percent"]

    gain_percent = highest_trail_rung * config["trail_trigger_step_percent"]
    selected = None
    for tier in sorted(tiers, key=lambda item: float(item.get("gain_percent", 0))):
        if gain_percent >= float(tier.get("gain_percent", 0)):
            selected = tier
    if not selected:
        return config["trail_stop_below_current_percent"]
    return float(
        selected.get(
            "trail_stop_below_current_percent",
            selected.get("trail_below_current_percent", config["trail_stop_below_current_percent"]),
        )
    )


def ladder_steps(config, fill_price, context=None):
    mode = risk_setting(config, "ladder_mode", "percent")
    if mode != "atr":
        return [
            {
                "key": str(drop_step),
                "drop_step_percent": drop_step,
                "trigger_price": fill_price * (1 - float(drop_step) / 100),
            }
            for drop_step in risk_setting(config, "ladder_drop_steps_percent", []) or []
        ]

    latest_bar = (context or {}).get("latest_bar")
    atr = float((latest_bar or {}).get("atr14") or 0)
    if atr <= 0:
        return []
    return [
        {
            "key": f"atr:{atr_step}",
            "atr_step": atr_step,
            "trigger_price": fill_price - atr * float(atr_step),
        }
        for atr_step in risk_setting(config, "ladder_atr_steps", []) or []
    ]


def atr_percent_from_context(context):
    latest_bar = (context or {}).get("latest_bar") or {}
    close = float(latest_bar.get("c") or 0)
    atr = float(latest_bar.get("atr14") or 0)
    return atr / close if close > 0 and atr > 0 else 0.0


def volatility_ladder_multiplier(atr_percent):
    if atr_percent > 0.06:
        return 0.25
    if atr_percent > 0.04:
        return 0.50
    if atr_percent > 0.025:
        return 0.75
    return 1.0


def adaptive_ladder_limit(config, context=None):
    base_limit = int(float(risk_setting(config, "max_ladder_count", 999999) or 0))
    if not risk_setting(config, "adaptive_ladder_enabled", False):
        return base_limit, {
            "adaptive_ladder_enabled": False,
            "max_ladder_count": base_limit,
        }

    atr_percent = atr_percent_from_context(context)
    adjusted_limit = base_limit
    if risk_setting(config, "volatility_ladder_scaling", False):
        if atr_percent > 0.06:
            adjusted_limit = 0
        elif atr_percent > 0.04:
            adjusted_limit = min(adjusted_limit, 1)

    if risk_setting(config, "market_ladder_scaling", False) and not (context or {}).get("market_ok", True):
        adjusted_limit = 0

    return adjusted_limit, {
        "adaptive_ladder_enabled": True,
        "base_max_ladder_count": base_limit,
        "adjusted_max_ladder_count": adjusted_limit,
        "atr_percent": atr_percent,
        "market_ok": (context or {}).get("market_ok", True),
    }


def adaptive_ladder_quantity(config, requested_qty, base_qty, context=None):
    requested_qty = int(requested_qty)
    if not risk_setting(config, "adaptive_ladder_enabled", False):
        return requested_qty, {
            "adaptive_ladder_enabled": False,
            "requested_qty": requested_qty,
            "adjusted_qty": requested_qty,
        }

    fraction = float(risk_setting(config, "ladder_size_fraction", 0.5) or 0)
    adjusted_qty = math.floor(max(0, int(float(base_qty or 0))) * fraction)
    adjusted_qty = min(requested_qty, adjusted_qty)
    atr_percent = atr_percent_from_context(context)
    volatility_multiplier = 1.0
    if risk_setting(config, "volatility_ladder_scaling", False):
        volatility_multiplier = volatility_ladder_multiplier(atr_percent)
        adjusted_qty = math.floor(adjusted_qty * volatility_multiplier)

    market_multiplier = 1.0
    if risk_setting(config, "market_ladder_scaling", False) and not (context or {}).get("market_ok", True):
        market_multiplier = 0.0
        adjusted_qty = 0

    min_qty = int(float(risk_setting(config, "min_ladder_quantity", 1) or 0))
    if 0 < adjusted_qty < min_qty:
        adjusted_qty = 0

    return max(0, adjusted_qty), {
        "adaptive_ladder_enabled": True,
        "requested_qty": requested_qty,
        "base_position_qty": int(float(base_qty or 0)),
        "ladder_size_fraction": fraction,
        "volatility_multiplier": volatility_multiplier,
        "market_multiplier": market_multiplier,
        "atr_percent": atr_percent,
        "market_ok": (context or {}).get("market_ok", True),
        "adjusted_qty": max(0, adjusted_qty),
    }


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
    pullback_stock_signal = (
        above_exit
        and no_same_day_loss_reentry
        and touched_pullback
        and reclaimed
        and reclaim_trend_ok
        and vwap_stability
        and bar["volume_ratio"] >= 1.15
        and pullback_limit <= ledger_cap
        and reward_risk_ok(pullback_limit, recent_high, atr, minimum=1.5)
    )
    pullback_signal = pullback_stock_signal

    breakout = bar["c"] > recent_high and bar["volume_ratio"] >= 1.3
    breakout_trend_ok = (
        bar["c"] > bar["vwap"]
        and bar["c"] > bar["ema9"] > bar["ema21"]
        and bar["ema21"] >= bar["ema50"]
        and ema21_slope >= 0.002
    )
    breakout_stock_signal = (
        above_exit
        and no_same_day_loss_reentry
        and breakout
        and breakout_trend_ok
        and bar["volume_ratio"] >= 1.5
        and breakout_limit <= ledger_cap
        and reward_risk_ok(breakout_limit, breakout_limit + 2 * atr, atr, minimum=1.5)
    )
    breakout_signal = breakout_stock_signal
    market_filter_ignored = not market_ok and (pullback_stock_signal or breakout_stock_signal)

    mode = None
    limit_price = None
    if pullback_signal:
        mode = "dynamic_pullback_reclaim"
        limit_price = pullback_limit
    elif breakout_signal:
        mode = "dynamic_breakout_continuation"
        limit_price = breakout_limit

    checks = {
        "market_ok": market_ok or market_filter_ignored,
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
        "market_ok": market_ok,
        "market_filter_ignored": market_filter_ignored,
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
        "base_position_qty",
        "ladder_filled_qty",
        "ladder_filled_notional",
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
    if state.get("entry_fill_price") is not None:
        state["last_exit_entry_price"] = float(state["entry_fill_price"])
    if order.get("filled_avg_price"):
        state["last_exit_price"] = float(order["filled_avg_price"])
    if order.get("filled_at"):
        state["last_exit_at"] = order.get("filled_at")
    if order.get("filled_qty") or order.get("qty"):
        state["last_exit_qty"] = int(float(order.get("filled_qty") or order.get("qty")))
    return {
        "last_exit_order_id": state.get("last_exit_order_id"),
        "last_exit_price": state.get("last_exit_price"),
        "last_exit_at": state.get("last_exit_at"),
        "last_exit_qty": state.get("last_exit_qty"),
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
        client.cancel_order(active_stop_id)
        canceled_stop_order_ids = [active_stop_id]
    else:
        canceled_stop_order_ids = []
        exit_details = record_exit_from_order(state, active_stop_order)

    for order in client.open_stop_orders(symbol):
        if order["id"] in canceled_stop_order_ids:
            continue
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
    active_stop_id = state.get("active_stop_order_id")
    open_stop_orders = client.open_stop_orders(symbol)
    active_open_stop = next(
        (order for order in open_stop_orders if order.get("id") == active_stop_id),
        None,
    )
    if (
        active_open_stop
        and state.get("active_stop_price") == rounded_stop
        and state.get("active_stop_qty") == qty
        and int(float(active_open_stop.get("qty") or 0)) == qty
        and dollars(float(active_open_stop.get("stop_price") or 0)) == rounded_stop
    ):
        for order in open_stop_orders:
            if order.get("id") != active_stop_id:
                client.cancel_order(order["id"])
        return None

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
            for open_order in open_stop_orders:
                if open_order.get("id") != active_stop_id:
                    client.cancel_order(open_order["id"])
            return order
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 422):
                raise

    for order in open_stop_orders:
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
            current_qty = int(float(order.get("qty") or 0))
            desired_qty = dynamic_entry_quantity(config, plan)
            daily_limit = dynamic_entry_share_limit(
                client,
                config,
                state,
                desired_limit,
                desired_qty,
            )
            desired_qty = min(desired_qty, daily_limit["remaining_qty"] + current_qty)
            desired_qty, risk_caps = apply_order_risk_caps(
                client, config, desired_qty, float(desired_limit), current_qty=0
            )
            if desired_qty <= 0:
                client.cancel_order(order_id)
                state.pop("reentry_order_id", None)
                state.pop("current_entry_order_id", None)
                return {
                    "status": "dynamic_entry_order_canceled_risk_cap",
                    "old_order_id": order_id,
                    "target_notional": dynamic_entry_notional(config, plan),
                    "mode": plan.get("mode"),
                    "risk_caps": risk_caps,
                    "daily_limit": daily_limit,
                    "dynamic_plan": serializable_plan(plan),
                }
            adjusted_qty, sizing = cash_protected_quantity(
                client, config, desired_qty, float(desired_limit)
            )
            if adjusted_qty <= 0:
                client.cancel_order(order_id)
                state.pop("reentry_order_id", None)
                state.pop("current_entry_order_id", None)
                return {
                    "status": "dynamic_entry_order_canceled_cash_reserve",
                    "old_order_id": order_id,
                    "target_notional": dynamic_entry_notional(config, plan),
                    "mode": plan.get("mode"),
                    "sizing": sizing,
                    "risk_caps": risk_caps,
                    "daily_limit": daily_limit,
                    "dynamic_plan": serializable_plan(plan),
                }
            desired_qty = adjusted_qty
            current_limit = float(order.get("limit_price") or 0)
            min_change = config.get("dynamic_replace_min_change_percent", 0.25) / 100
            if (
                current_qty != desired_qty
                or current_limit <= 0
                or abs(desired_limit / current_limit - 1) >= min_change
            ):
                replaced = client.replace_order(
                    order_id,
                    {
                        "qty": str(desired_qty),
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
                    "qty": replaced.get("qty"),
                    "target_notional": dynamic_entry_notional(config, plan),
                    "mode": plan.get("mode"),
                    "sizing": sizing,
                    "risk_caps": risk_caps,
                    "daily_limit": daily_limit,
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


def dynamic_entry_reference_qty(client, config, state, fallback_qty):
    position = client.position(config["symbol"])
    position_qty = position_quantity(position)
    for value in (
        position_qty,
        state.get("active_stop_qty"),
        state.get("last_exit_qty"),
        fallback_qty,
    ):
        qty = int(float(value or 0))
        if qty > 0:
            return qty
    return 0


def dynamic_entry_share_limit(client, config, state, limit_price, fallback_qty):
    affordable_qty, cash_sizing = cash_available_share_count(
        client,
        config,
        float(limit_price),
    )
    reference_qty = dynamic_entry_reference_qty(client, config, state, fallback_qty)
    daily_share_limit = min(reference_qty, affordable_qty)
    shares_today = int(float(state.get("reentry_shares_today", 0) or 0))
    remaining_qty = max(0, daily_share_limit - shares_today)
    return {
        "reference_qty": reference_qty,
        "affordable_qty": affordable_qty,
        "daily_share_limit": daily_share_limit,
        "shares_today": shares_today,
        "remaining_qty": remaining_qty,
        **cash_sizing,
    }


def submit_dynamic_entry(client, config, state, plan, reason_prefix):
    symbol = config["symbol"]
    requested_qty = dynamic_entry_quantity(config, plan)
    daily_limit = dynamic_entry_share_limit(
        client,
        config,
        state,
        plan["limit_price"],
        requested_qty,
    )
    requested_qty = min(requested_qty, daily_limit["remaining_qty"])
    if requested_qty <= 0:
        return {
            "status": "dynamic_entry_limit_reached",
            "reason": f"{reason_prefix}_{plan['mode']}",
            "reentry_shares_today": state.get("reentry_shares_today", 0),
            "daily_limit": daily_limit,
            "dynamic_plan": serializable_plan(plan),
        }

    requested_qty, risk_caps = apply_order_risk_caps(
        client, config, requested_qty, float(plan["limit_price"]), current_qty=0
    )
    if requested_qty <= 0:
        return {
            "status": "dynamic_entry_risk_cap_blocked",
            "reason": f"{reason_prefix}_{plan['mode']}",
            "risk_caps": risk_caps,
            "daily_limit": daily_limit,
            "dynamic_plan": serializable_plan(plan),
        }

    qty, sizing = cash_protected_quantity(
        client, config, requested_qty, float(plan["limit_price"])
    )
    target_notional = dynamic_entry_notional(config, plan)
    if qty <= 0:
        return {
            "status": "dynamic_entry_cash_reserve_blocked",
            "reason": f"{reason_prefix}_{plan['mode']}",
            "target_notional": target_notional,
            "sizing": sizing,
            "risk_caps": risk_caps,
            "dynamic_plan": serializable_plan(plan),
        }

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
    state["reentry_shares_today"] = state.get("reentry_shares_today", 0) + qty
    reset_managed_position_state(state)
    return {
        "status": "dynamic_reentry_order_submitted",
        "reason": state["reentry_reason"],
        "limit_price": order.get("limit_price"),
        "qty": order.get("qty"),
        "target_notional": target_notional,
        "market_filter_ignored": plan.get("market_filter_ignored", False),
        "reentry_order_id": order["id"],
        "sizing": sizing,
        "risk_caps": risk_caps,
        "daily_limit": daily_limit,
        "dynamic_plan": serializable_plan(plan),
    }


def reset_entry_day_if_needed(state):
    today_key = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if state.get("reentry_day") != today_key:
        state["reentry_day"] = today_key
        state["reentries_today"] = 0
        state["reentry_shares_today"] = 0
        state["reentry_pullback_seen"] = False


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

    requested_qty = int(config.get("reentry_quantity", config["entry_quantity"]))
    daily_limit = dynamic_entry_share_limit(
        client,
        config,
        state,
        limit_price,
        requested_qty,
    )
    requested_qty = min(requested_qty, daily_limit["remaining_qty"])
    if requested_qty <= 0:
        return {
            "status": "reentry_limit_reached",
            "reentry_shares_today": state.get("reentry_shares_today", 0),
            "daily_limit": daily_limit,
        }

    requested_qty, risk_caps = apply_order_risk_caps(
        client, config, requested_qty, float(limit_price), current_qty=0
    )
    if requested_qty <= 0:
        return {
            "status": "reentry_risk_cap_blocked",
            "reason": reason,
            "current_price": current_price,
            "limit_price": limit_price,
            "risk_caps": risk_caps,
            "daily_limit": daily_limit,
        }

    qty, sizing = cash_protected_quantity(
        client, config, requested_qty, float(limit_price)
    )
    if qty <= 0:
        return {
            "status": "reentry_cash_reserve_blocked",
            "reason": reason,
            "current_price": current_price,
            "limit_price": limit_price,
            "sizing": sizing,
            "risk_caps": risk_caps,
        }

    order = client.submit_order(
        {
            "symbol": symbol,
            "qty": str(qty),
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
    state["reentry_shares_today"] = state.get("reentry_shares_today", 0) + qty
    reset_managed_position_state(state)

    return {
        "status": "reentry_order_submitted",
        "reason": reason,
        "current_price": current_price,
        "limit_price": order.get("limit_price"),
        "qty": order.get("qty"),
        "reentry_order_id": order["id"],
        "sizing": sizing,
        "risk_caps": risk_caps,
        "daily_limit": daily_limit,
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
    position = client.position(symbol)
    qty = position_quantity(position)

    if not entry_order_id:
        if qty <= 0:
            if config.get("dynamic_entry_enabled"):
                return handle_dynamic_flat_entry(client, config, state, exit_trade=None)
            return {"status": "no_entry_order_configured", "symbol": symbol}
        state.setdefault("entry_fill_price", float(position["avg_entry_price"]))
        state.setdefault("highest_trail_rung", 0)
        state.setdefault("base_position_qty", int(config.get("entry_quantity", qty)))
        state.setdefault("ladder_filled_qty", 0)
        state.setdefault("ladder_filled_notional", 0.0)
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
    state.setdefault("base_position_qty", int(config.get("entry_quantity", qty)))
    state.setdefault("ladder_filled_qty", 0)
    state.setdefault("ladder_filled_notional", 0.0)
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
    context = risk_context(client, config) if risk_context_needed(config) else None
    base_floor = initial_floor_price(config, fill_price, context)
    trail_step = config["trail_trigger_step_percent"] / 100
    current_rung = int((current_price / fill_price - 1) / trail_step)
    current_rung = max(0, current_rung)

    if current_rung > state["highest_trail_rung"]:
        state["highest_trail_rung"] = current_rung

    if state["highest_trail_rung"] > 0:
        trail_below = trail_below_current_percent(config, state["highest_trail_rung"])
        candidate_floor = current_price * (
            1 - trail_below / 100
        )
    else:
        candidate_floor = base_floor

    previous_floor = float(state.get("floor_price", 0))
    floor_price = max(previous_floor, base_floor, candidate_floor)
    state["floor_price"] = floor_price

    stop_order = update_stop_order(client, symbol, qty, floor_price, state)

    ladder_orders = []
    skipped_ladder_orders = []
    max_ladder_count, ladder_limit_detail = adaptive_ladder_limit(config, context)
    for ladder_step in ladder_steps(config, fill_price, context):
        step_key = ladder_step["key"]
        legacy_step = ladder_step.get("drop_step_percent")
        if (
            step_key in state["filled_ladder_steps"]
            or legacy_step in state["filled_ladder_steps"]
            or len(state["filled_ladder_steps"]) >= max_ladder_count
        ):
            if len(state["filled_ladder_steps"]) >= max_ladder_count:
                skipped_ladder_orders.append(
                    {
                        **ladder_step,
                        "status": "max_ladder_count_blocked",
                        "ladder_limit": ladder_limit_detail,
                    }
                )
            continue

        trigger_price = ladder_step["trigger_price"]
        if current_price <= trigger_price:
            if (
                risk_setting(config, "ladder_requires_market_ok", False)
                and context
                and not context.get("market_ok", True)
            ):
                skipped_ladder_orders.append(
                    {
                        **ladder_step,
                        "status": "market_regime_blocked",
                    }
                )
                continue

            requested_qty = int(config["ladder_buy_quantity"])
            requested_qty, adaptive_qty = adaptive_ladder_quantity(
                config,
                requested_qty,
                state.get("base_position_qty", config.get("entry_quantity", qty)),
                context,
            )
            if requested_qty <= 0:
                skipped_ladder_orders.append(
                    {
                        **ladder_step,
                        "status": "adaptive_ladder_size_blocked",
                        "adaptive_ladder": adaptive_qty,
                        "ladder_limit": ladder_limit_detail,
                    }
                )
                continue

            requested_qty, ladder_caps = apply_ladder_risk_caps(
                client, config, state, requested_qty, current_price, current_qty=qty
            )
            if requested_qty <= 0:
                skipped_ladder_orders.append(
                    {
                        **ladder_step,
                        "status": "ladder_risk_cap_blocked",
                        "adaptive_ladder": adaptive_qty,
                        "ladder_limit": ladder_limit_detail,
                        "ladder_caps": ladder_caps,
                    }
                )
                continue

            requested_qty, risk_caps = apply_order_risk_caps(
                client, config, requested_qty, current_price, current_qty=qty
            )
            if requested_qty <= 0:
                skipped_ladder_orders.append(
                    {
                        **ladder_step,
                        "status": "risk_cap_blocked",
                        "adaptive_ladder": adaptive_qty,
                        "ladder_limit": ladder_limit_detail,
                        "ladder_caps": ladder_caps,
                        "risk_caps": risk_caps,
                    }
                )
                continue

            ladder_qty, sizing = cash_protected_quantity(
                client, config, requested_qty, current_price
            )
            if ladder_qty <= 0:
                skipped_ladder_orders.append(
                    {
                        **ladder_step,
                        "status": "cash_reserve_blocked",
                        "sizing": sizing,
                        "adaptive_ladder": adaptive_qty,
                        "ladder_limit": ladder_limit_detail,
                        "ladder_caps": ladder_caps,
                        "risk_caps": risk_caps,
                    }
                )
                continue

            order = client.submit_order(
                {
                    "symbol": symbol,
                    "qty": str(ladder_qty),
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "day",
                }
            )
            state["filled_ladder_steps"].append(step_key)
            state["ladder_order_ids"][step_key] = order["id"]
            state["ladder_filled_qty"] = state.get("ladder_filled_qty", 0) + ladder_qty
            state["ladder_filled_notional"] = state.get("ladder_filled_notional", 0.0) + ladder_qty * current_price
            ladder_orders.append(
                {
                    "order": order,
                    "sizing": sizing,
                    "adaptive_ladder": adaptive_qty,
                    "ladder_limit": ladder_limit_detail,
                    "ladder_caps": ladder_caps,
                    "risk_caps": risk_caps,
                }
            )

    return {
        "status": "managed",
        "entry_fill_price": fill_price,
        "current_price": current_price,
        "position_qty": qty,
        "floor_price": floor_price,
        "highest_trail_rung": state["highest_trail_rung"],
        "updated_stop_order": stop_order["id"] if stop_order else None,
        "new_ladder_orders": [item["order"]["id"] for item in ladder_orders],
        "ladder_sizing": [item["sizing"] for item in ladder_orders],
        "adaptive_ladder": [item["adaptive_ladder"] for item in ladder_orders],
        "ladder_limit": ladder_limit_detail,
        "ladder_caps": [item["ladder_caps"] for item in ladder_orders],
        "ladder_risk_caps": [item["risk_caps"] for item in ladder_orders],
        "skipped_ladder_orders": skipped_ladder_orders,
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
