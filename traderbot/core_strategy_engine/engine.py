import argparse
import datetime
import hashlib
import json
import math
import os
import socket
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from zoneinfo import ZoneInfo

from traderbot.core_strategy_engine.entry_models.features import (
    allowed_ledger_price,
    build_entry_features,
    market_ok_at,
)
from traderbot.core_strategy_engine.entry_models.breakout import evaluate_breakout
from traderbot.core_strategy_engine.entry_models.arbiter import select_entry_candidate
from traderbot.core_strategy_engine.entry_models.candidate import (
    reward_risk_ok,
    structural_stop_price,
)
from traderbot.core_strategy_engine.entry_models.pullback import evaluate_pullback
from traderbot.core_strategy_engine.entry_models.signal_families import (
    evaluate_signal_families,
)
from traderbot.core_strategy_engine.lifecycle.coordinator import select_action_intent
from traderbot.core_strategy_engine.position_health import (
    evaluate_add_eligibility,
    evaluate_position_health,
)


DEFAULT_CONFIG_PATH = "traderbot/core_strategy_engine/strategies/configs/strategy_config.json"
DEFAULT_STATE_PATH = "runtime/state/strategy_state.json"
MARKET_SYMBOL = "QQQ"
DEFAULT_MIN_CASH_BALANCE_PERCENT = 20
DEFAULT_CATASTROPHIC_STOP_LOSS_PERCENT = 8
DEFAULT_ADVERSE_REDUCTION_TRIGGER_PERCENT = 6
DEFAULT_ADVERSE_REDUCTION_FRACTION = 0.5
MAX_ENTRY_CLOCK_AGE_SECONDS = 30
EASTERN = ZoneInfo("America/New_York")
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


def fresh_open_market_clock(clock, now=None, max_age_seconds=MAX_ENTRY_CLOCK_AGE_SECONDS):
    if not clock or not clock.get("is_open"):
        return False, "market is closed"

    timestamp = clock.get("timestamp")
    if not timestamp:
        return False, "market clock timestamp is missing"
    try:
        clock_time = datetime.datetime.fromisoformat(
            str(timestamp).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False, f"market clock timestamp is invalid: {timestamp}"
    if clock_time.tzinfo is None:
        return False, "market clock timestamp has no timezone"

    checked_at = now or datetime.datetime.now(datetime.timezone.utc)
    age_seconds = (checked_at - clock_time.astimezone(datetime.timezone.utc)).total_seconds()
    if age_seconds < -5:
        return False, f"market clock timestamp is {abs(age_seconds):.1f}s in the future"
    if age_seconds > max_age_seconds:
        return False, f"market clock is stale by {age_seconds:.1f}s"
    return True, None


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

    def order_by_client_order_id(self, client_order_id):
        query = urllib.parse.urlencode({"client_order_id": client_order_id})
        try:
            return self.trading("GET", f"/orders:by_client_order_id?{query}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

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

    def latest_quote(self, symbol):
        query = urllib.parse.urlencode({"feed": "iex"})
        response = self.data("GET", f"/stocks/{symbol}/quotes/latest?{query}") or {}
        quote = response.get("quote") or {}
        return {
            "bid_price": quote.get("bp"),
            "ask_price": quote.get("ap"),
            "timestamp": quote.get("t"),
        }

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

    def open_buy_orders(self, symbol):
        return [
            order
            for order in self.open_orders()
            if order.get("symbol") == symbol
            and order.get("side") == "buy"
        ]

    def cancel_order(self, order_id):
        try:
            self.trading("DELETE", f"/orders/{order_id}")
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 422):
                raise

    def submit_order(self, payload):
        if payload.get("side") == "buy":
            clock = self.clock()
            allowed, reason = fresh_open_market_clock(clock)
            if not allowed:
                raise RuntimeError(f"buy order blocked: {reason}")
            open_orders = self.open_buy_orders(payload.get("symbol"))
            if open_orders:
                order_ids = ", ".join(
                    str(order.get("id")) for order in open_orders
                )
                raise RuntimeError(
                    "buy order blocked: existing open buy order(s) for "
                    f"{payload.get('symbol')}: {order_ids}"
                )
        return self.trading("POST", "/orders", payload)

    def replace_order(self, order_id, payload):
        return self.trading("PATCH", f"/orders/{order_id}", payload)


def dollars(value):
    return f"{value:.2f}"


def action_client_order_id(prefix, symbol, episode_id, sequence=1):
    digest = hashlib.sha256(str(episode_id).encode("utf-8")).hexdigest()[:12]
    return f"tb-{prefix}-{str(symbol).lower()}-{digest}-{int(sequence)}"[:48]


def cancel_symbol_order(client, order_id, symbol):
    if hasattr(client, "symbol_transaction"):
        return client.cancel_order(order_id, symbol=symbol)
    return client.cancel_order(order_id)


def dynamic_entry_notional(config, plan):
    if plan.get("market_filter_ignored"):
        return float(config.get("dynamic_market_filter_ignored_notional", 2500))
    return float(config.get("dynamic_entry_notional", 5000))


def dynamic_entry_quantity(config, plan, equity=None):
    limit_price = float(plan["limit_price"])
    stop_price = float(plan.get("stop_price") or 0)
    risk_per_share = limit_price - stop_price
    has_structural_stop = 0 < stop_price < limit_price
    if equity not in (None, "", 0) and has_structural_stop:
        risk_percent = float(
            plan.get("risk_budget_percent")
            or config.get("risk_per_trade_percent", 0.5)
        )
        risk_budget = float(equity) * risk_percent / 100
        return max(0, math.floor(risk_budget / risk_per_share))
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


def completed_market_bars(raw_bars, timeframe, now=None):
    """Exclude bars whose configured interval has not finished yet."""
    checked_at = now or datetime.datetime.now(datetime.timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=datetime.timezone.utc)
    checked_at = checked_at.astimezone(datetime.timezone.utc)
    value = str(timeframe or "5Min").strip().lower()
    if value.endswith("min"):
        duration = datetime.timedelta(minutes=max(1, int(value[:-3])))
    elif value.endswith("hour"):
        duration = datetime.timedelta(hours=max(1, int(value[:-4])))
    else:
        return list(raw_bars)
    return [
        bar
        for bar in raw_bars
        if parse_alpaca_time(bar.get("t"))
        and parse_alpaca_time(bar["t"]) + duration <= checked_at
    ]


def risk_context(client, config):
    now = datetime.datetime.now(datetime.timezone.utc)
    lookback_days = config.get("dynamic_lookback_days", 7)
    start = iso_utc(now - datetime.timedelta(days=lookback_days))
    end = iso_utc(now)
    timeframe = config.get("dynamic_timeframe", "5Min")
    bars = calculate_indicators(
        completed_market_bars(
            client.stock_bars(config["symbol"], start, end, timeframe), timeframe, now
        )
    )
    market_bars = calculate_indicators(
        completed_market_bars(
            client.stock_bars(MARKET_SYMBOL, start, end, timeframe), timeframe, now
        )
    )
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
    dollar_volume_by_slot = {}
    previous_session_close = None
    session_gap_percent = None

    for raw in raw_bars:
        timestamp = parse_alpaca_time(raw["t"])
        high = float(raw["h"])
        low = float(raw["l"])
        close = float(raw["c"])
        volume = float(raw["v"])
        typical = (high + low + close) / 3

        bar_date = timestamp.astimezone(EASTERN).date()
        if session_date != bar_date:
            if session_date is not None:
                previous_session_close = prev_close
            session_date = bar_date
            session_pv = 0.0
            session_volume = 0.0
            session_gap_percent = (
                (float(raw["o"]) / previous_session_close - 1) * 100
                if previous_session_close
                else None
            )

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

        eastern = timestamp.astimezone(EASTERN)
        slot = (eastern.hour, eastern.minute)
        dollar_volume = typical * volume
        slot_history = dollar_volume_by_slot.setdefault(slot, [])
        comparison = slot_history[-20:]
        avg_dollar_volume = (
            sum(comparison) / len(comparison) if comparison else dollar_volume
        )
        volume_ratio = dollar_volume / avg_dollar_volume if avg_dollar_volume else 1
        rvol_sample_size = len(comparison)
        slot_history.append(dollar_volume)

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
                "relative_dollar_volume": volume_ratio,
                "rvol_sample_size": rvol_sample_size,
                "dollar_volume": dollar_volume,
                "matched_average_dollar_volume": avg_dollar_volume,
                "dollar_volume_sample_size": rvol_sample_size,
                "session_gap_percent": session_gap_percent,
                "rvol_method": "matched_eastern_time_20_session_dollar_volume",
            }
        )
        prev_close = close

    return bars


def prior_window(bars, index, size):
    return bars[max(0, index - size) : index]


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
    lookback_days = max(
        int(config.get("dynamic_lookback_days", 7)),
        int(config.get("rvol_lookback_days", 35)),
    )
    start = iso_utc(now - datetime.timedelta(days=lookback_days))
    end = iso_utc(now)
    timeframe = config.get("dynamic_timeframe", "5Min")
    bars = calculate_indicators(
        completed_market_bars(
            client.stock_bars(symbol, start, end, timeframe), timeframe, now
        )
    )
    market_bars = calculate_indicators(
        completed_market_bars(
            client.stock_bars(MARKET_SYMBOL, start, end, timeframe), timeframe, now
        )
    )
    health_settings = config.get("position_health") or {}
    sector_symbol = str(
        (health_settings.get("benchmark_by_symbol") or {}).get(symbol)
        or health_settings.get("benchmark_symbol")
        or MARKET_SYMBOL
    ).upper()
    if sector_symbol == MARKET_SYMBOL:
        sector_bars = market_bars
    else:
        sector_bars = calculate_indicators(
            completed_market_bars(
                client.stock_bars(sector_symbol, start, end, timeframe), timeframe, now
            )
        )
    filter_context = {}
    filter_settings = config.get("entry_filters") or {}
    spread_settings = filter_settings.get("spread") or {}
    if spread_settings is True or (
        isinstance(spread_settings, dict) and spread_settings.get("enabled", False)
    ):
        filter_context["quote"] = client.latest_quote(symbol)
    filter_context["evaluated_at"] = datetime.datetime.now(datetime.timezone.utc)
    event_calendar_path = filter_settings.get("event_calendar_path")
    if event_calendar_path:
        filter_context["event_calendar"] = load_json(event_calendar_path, {})
    return bars, market_bars, sector_bars, filter_context


def dynamic_entry_plan(
    symbol,
    bars,
    market_bars,
    exit_trade=None,
    ignore_ledger=False,
    sector_bars=None,
    config=None,
    filter_context=None,
):
    config = config or {}
    features = build_entry_features(
        symbol,
        bars,
        market_bars,
        exit_trade=exit_trade,
        ignore_ledger=ignore_ledger,
        sector_bars=sector_bars,
        config=config,
        filter_context=filter_context,
    )
    if features is None:
        return {"symbol": symbol, "status": "not_enough_bars"}

    bar = features["bar"]
    atr = features["atr14"]
    ema21_slope = features["ema21_slope_5bars"]
    recent_high = features["recent_high_20"]
    ledger_cap = features["ledger_cap"]
    pullback_candidate = evaluate_pullback(features, config)
    breakout_candidate = evaluate_breakout(features, config)
    family_candidates = evaluate_signal_families(features, config)
    entry_candidates = [pullback_candidate, breakout_candidate, *family_candidates]
    pullback_zone = pullback_candidate["pullback_zone"]
    pullback_touch_trigger = pullback_candidate["touch_trigger"]
    pullback_reclaim_trigger = pullback_candidate["reclaim_trigger"]
    pullback_limit = pullback_candidate["limit_price"]
    breakout_limit = breakout_candidate["limit_price"]
    breakout_target = breakout_candidate["target_price"]
    signal_trigger_candidates = [
        price
        for price in (pullback_reclaim_trigger, recent_high)
        if price >= bar["c"]
    ]
    next_signal_trigger = min(signal_trigger_candidates) if signal_trigger_candidates else None

    market_ok = features["market_ok"]
    sector_ok = features["sector_ok"]
    market_filter_ignored = False

    arbitration = select_entry_candidate(entry_candidates)
    selected_candidate = arbitration["selected"]
    territorial_candidate = arbitration["classified"] or pullback_candidate
    classified_candidate = selected_candidate or territorial_candidate

    mode = selected_candidate["legacy_mode"] if selected_candidate else None
    limit_price = selected_candidate["limit_price"] if selected_candidate else None
    selected_stop = selected_candidate["stop_price"] if selected_candidate else None
    selected_target = selected_candidate["target_price"] if selected_candidate else None
    selected_risk = selected_candidate["risk_per_share"] if selected_candidate else None
    selected_reward_risk = (
        selected_candidate["expected_reward_risk"] if selected_candidate else None
    )
    minimum_rr = (
        selected_candidate["minimum_reward_risk"]
        if selected_candidate
        else float(config.get("minimum_entry_reward_risk", 1.5))
    )

    return {
        "symbol": symbol,
        "status": "active_signal" if mode else "watch",
        "mode": mode,
        "model_id": selected_candidate.get("model_id") if selected_candidate else None,
        "model_version": (
            selected_candidate.get("model_version") if selected_candidate else None
        ),
        "classified_model_id": territorial_candidate.get("model_id"),
        "entry_arbitration_reason": arbitration["reason"],
        "setup_score": classified_candidate.get("setup_score"),
        "soft_check_score": classified_candidate.get("soft_check_score"),
        "factor_scores": classified_candidate.get("factor_scores"),
        "factor_weights": classified_candidate.get("factor_weights"),
        "factor_contributions": classified_candidate.get("factor_contributions"),
        "trend_assessment": classified_candidate.get("trend_assessment"),
        "breakout_assessment": classified_candidate.get("breakout_assessment"),
        "overall_check_score": classified_candidate.get("overall_check_score"),
        "minimum_setup_score": classified_candidate.get("minimum_setup_score"),
        "setup_score_meets_threshold": classified_candidate.get(
            "setup_score_meets_threshold"
        ),
        "model_checks": classified_candidate.get("checks"),
        "hard_checks": classified_candidate.get("hard_checks"),
        "soft_checks": classified_candidate.get("soft_checks"),
        "limit_price": limit_price,
        "last_bar_time": bar["t"].isoformat(),
        "last_price": bar["c"],
        "ledger_ignored": features["ledger_ignored"],
        "market_ok": market_ok,
        "sector_ok": sector_ok,
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
        "stop_price": selected_stop,
        "target_price": selected_target,
        "risk_per_share": selected_risk,
        "expected_reward_risk": selected_reward_risk,
        "minimum_entry_reward_risk": minimum_rr,
        "risk_budget_percent": classified_candidate.get("risk_budget_percent"),
        "signal_family": classified_candidate.get("signal_family"),
        "fallback_only": classified_candidate.get("fallback_only", False),
        "ema9": bar["ema9"],
        "ema21": bar["ema21"],
        "ema50": bar["ema50"],
        "vwap": bar["vwap"],
        "volume_ratio": bar["volume_ratio"],
        "ema21_slope": ema21_slope,
        "price_action": price_action_label(bar, ema21_slope),
        "blockers": classified_candidate.get("blockers", []),
        "hard_blockers": classified_candidate.get("hard_blockers", []),
        "soft_blockers": classified_candidate.get("soft_blockers", []),
        "decision_reasons": classified_candidate.get("decision_reasons", []),
        "qualification_policy": classified_candidate.get("qualification_policy"),
        "entry_candidates": entry_candidates,
    }


def serializable_plan(plan):
    cleaned = dict(plan)
    ledger_cap = cleaned.get("ledger_cap", math.inf)
    if ledger_cap is None or math.isinf(ledger_cap):
        cleaned["ledger_cap"] = None
    return cleaned


def reset_managed_position_state(state):
    for key in (
        "active_stop_order_id",
        "active_stop_price",
        "active_stop_qty",
        "recovery_stop_active",
        "recovery_stop_price",
        "entry_fill_price",
        "filled_ladder_steps",
        "floor_price",
        "highest_trail_rung",
        "base_position_qty",
        "ladder_filled_qty",
        "ladder_filled_notional",
        "ladder_order_ids",
        "adverse_reduction_order_id",
        "adverse_reduction_completed",
        "adverse_reduction_qty",
        "adverse_reduction_trigger_price",
        "position_episode",
        "position_health",
        "position_health_last_evaluated_at",
        "position_health_confirmation",
    ):
        state.pop(key, None)


def effective_managed_stop_price(config, state, current_price, planned_floor):
    if planned_floor < current_price:
        state.pop("recovery_stop_active", None)
        state.pop("recovery_stop_price", None)
        return planned_floor, {
            "recovery_stop_active": False,
            "planned_floor_price": planned_floor,
        }

    if not risk_setting(config, "recovery_stop_enabled", True):
        return planned_floor, {
            "recovery_stop_active": False,
            "planned_floor_price": planned_floor,
            "recovery_stop_disabled": True,
        }

    distance_percent = float(
        risk_setting(
            config,
            "recovery_stop_below_current_percent",
            risk_setting(config, "trail_stop_below_current_percent", 2.5),
        )
    )
    if not 0 < distance_percent < 100:
        raise ValueError("recovery_stop_below_current_percent must be between 0 and 100")

    candidate = current_price * (1 - distance_percent / 100)
    previous_recovery = float(state.get("recovery_stop_price", 0) or 0)
    recovery_price = max(previous_recovery, candidate)
    state["recovery_stop_active"] = True
    state["recovery_stop_price"] = recovery_price
    return recovery_price, {
        "recovery_stop_active": True,
        "recovery_stop_price": recovery_price,
        "recovery_stop_below_current_percent": distance_percent,
        "planned_floor_price": planned_floor,
    }


def reset_entry_tracking_state(state):
    for key in (
        "current_entry_order_id",
        "managed_entry_order_id",
        "reentry_order_id",
        "reentry_reason",
        "pending_entry_model_id",
        "pending_entry_model_version",
        "pending_entry_candidate_as_of",
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


def symbol_open_buy_orders(client, symbol):
    if hasattr(client, "open_buy_orders"):
        return list(client.open_buy_orders(symbol) or [])
    if hasattr(client, "open_orders"):
        return [
            order
            for order in (client.open_orders() or [])
            if order.get("symbol") == symbol
            and order.get("side") == "buy"
        ]
    return []


def track_existing_open_buy_order(client, symbol, state):
    open_orders = symbol_open_buy_orders(client, symbol)
    if not open_orders:
        return []
    tracked_id = state.get("reentry_order_id") or state.get("current_entry_order_id")
    tracked = next(
        (order for order in open_orders if order.get("id") == tracked_id),
        None,
    )
    selected = tracked or sorted(
        open_orders,
        key=lambda order: (order.get("created_at") or "", order.get("id") or ""),
    )[0]
    state["current_entry_order_id"] = selected["id"]
    state["reentry_order_id"] = selected["id"]
    return open_orders


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
    entry_price = state.get("last_exit_entry_price")
    if (
        entry_price not in (None, "")
        and state.get("last_exit_price") not in (None, "")
        and state.get("last_exit_qty") not in (None, "")
    ):
        state["last_trade_pl"] = (
            float(state["last_exit_price"]) - float(entry_price)
        ) * float(state["last_exit_qty"])
    state["last_exit_source"] = "tracked_order"
    return {
        "last_exit_order_id": state.get("last_exit_order_id"),
        "last_exit_price": state.get("last_exit_price"),
        "last_exit_at": state.get("last_exit_at"),
        "last_exit_qty": state.get("last_exit_qty"),
        "last_trade_pl": state.get("last_trade_pl"),
    }


def record_position_snapshot(state, position, as_of=None):
    """Retain enough broker state to reconcile an exit observed between polls."""
    if position_quantity(position) <= 0:
        return
    state["last_position_seen_at"] = as_of or iso_utc(
        datetime.datetime.now(datetime.timezone.utc)
    )
    state["last_position_qty"] = position_quantity(position)
    if position.get("avg_entry_price") not in (None, ""):
        state["last_position_entry_price"] = float(position["avg_entry_price"])


def fill_timestamp_value(fill):
    return fill.get("transaction_time") or fill.get("timestamp") or fill.get("date")


def fill_is_sell(fill):
    side = str(fill.get("side") or fill.get("order_side") or "").lower()
    if side:
        return side == "sell"
    try:
        return float(fill.get("qty") or fill.get("net_qty") or 0) < 0
    except (TypeError, ValueError):
        return False


def reconstruct_exit_from_fills(client, symbol, state):
    """Recover an untracked/manual exit after a held position is observed flat."""
    if not hasattr(client, "fills"):
        return None
    after = state.get("last_position_seen_at")
    episode = state.get("position_episode") or {}
    after = after or episode.get("opened_at")
    if not after:
        return None
    after_at = parse_alpaca_time(after)
    if after_at:
        after = iso_utc(after_at - datetime.timedelta(minutes=5))
    try:
        fills = client.fills(after)
    except Exception as exc:
        state["exit_reconciliation_error"] = f"{type(exc).__name__}: {exc}"
        return None
    sells = [
        fill for fill in (fills or [])
        if fill.get("symbol") == symbol and fill_is_sell(fill) and fill_timestamp_value(fill)
    ]
    if not sells:
        return None
    ordered_sells = sorted(
        sells,
        key=lambda fill: parse_alpaca_time(fill_timestamp_value(fill)),
        reverse=True,
    )
    expected_qty = float(state.get("last_position_qty") or 0)
    order_fills = []
    selected_qty = 0.0
    for fill in ordered_sells:
        order_fills.append(fill)
        selected_qty += abs(float(fill.get("qty") or fill.get("net_qty") or 0))
        if expected_qty <= 0 or selected_qty >= expected_qty:
            break
    qty = sum(abs(float(fill.get("qty") or fill.get("net_qty") or 0)) for fill in order_fills)
    notional = sum(
        abs(float(fill.get("qty") or fill.get("net_qty") or 0))
        * float(fill.get("price") or 0)
        for fill in order_fills
    )
    if qty <= 0 or notional <= 0:
        return None
    order_ids = list(dict.fromkeys(
        fill.get("order_id") for fill in order_fills if fill.get("order_id")
    ))
    state["last_exit_order_id"] = order_ids[0] if order_ids else None
    state["last_exit_order_ids"] = order_ids
    state["last_exit_price"] = notional / qty
    latest_fill = max(
        order_fills,
        key=lambda fill: parse_alpaca_time(fill_timestamp_value(fill)),
    )
    state["last_exit_at"] = fill_timestamp_value(latest_fill)
    state["last_exit_qty"] = int(qty) if qty.is_integer() else qty
    entry_price = state.get("entry_fill_price") or state.get("last_position_entry_price")
    if entry_price not in (None, ""):
        state["last_exit_entry_price"] = float(entry_price)
        state["last_trade_pl"] = (
            state["last_exit_price"] - state["last_exit_entry_price"]
        ) * state["last_exit_qty"]
    state["last_exit_source"] = "broker_fill_reconstruction"
    state.pop("exit_reconciliation_error", None)
    return {
        "last_exit_order_id": state.get("last_exit_order_id"),
        "last_exit_price": state["last_exit_price"],
        "last_exit_at": state["last_exit_at"],
        "last_exit_qty": state["last_exit_qty"],
        "last_exit_source": state["last_exit_source"],
        "last_trade_pl": state.get("last_trade_pl"),
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
        cancel_symbol_order(client, active_stop_id, symbol)
        canceled_stop_order_ids = [active_stop_id]
    else:
        canceled_stop_order_ids = []
        exit_details = record_exit_from_order(state, active_stop_order)

    for order in client.open_stop_orders(symbol):
        if order["id"] in canceled_stop_order_ids:
            continue
        cancel_symbol_order(client, order["id"], symbol)
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
    if had_managed_state and not exit_details:
        exit_details = reconstruct_exit_from_fills(client, symbol, state)
        if not exit_details:
            state["exit_record_missing"] = True
        else:
            state.pop("exit_record_missing", None)
    episode = state.get("position_episode")
    if episode:
        closed_episode = {
            **episode,
            "state": "CLOSED",
            "closed_at": (exit_details or {}).get("last_exit_at")
            or iso_utc(datetime.datetime.now(datetime.timezone.utc)),
            "exit_order_id": (exit_details or {}).get("last_exit_order_id"),
            "exit_price": (exit_details or {}).get("last_exit_price"),
            "final_health": state.get("position_health"),
        }
        history = list(state.get("closed_position_episodes") or [])
        if not any(item.get("episode_id") == closed_episode.get("episode_id") for item in history):
            history.append(closed_episode)
        state["closed_position_episodes"] = history[-50:]
    reset_managed_position_state(state)
    open_buy_orders = track_existing_open_buy_order(client, symbol, state)
    if not open_buy_orders:
        reset_entry_tracking_state(state)
    return {
        "position_qty": 0,
        "cleared_stale_state": had_managed_state,
        "canceled_stop_order_ids": canceled_stop_order_ids,
        "open_buy_order_ids": [order.get("id") for order in open_buy_orders],
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
                cancel_symbol_order(client, order["id"], symbol)
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
                    cancel_symbol_order(client, open_order["id"], symbol)
            return order
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 422):
                raise

    for order in open_stop_orders:
        cancel_symbol_order(client, order["id"], symbol)

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


def ensure_catastrophic_stop(client, config, position, state):
    """Keep every live long position covered by a broker-held stop order.

    This runs before entry-order status handling so shares from a partial fill are
    protected instead of waiting for the complete entry order to fill.  A stop
    already at or above the catastrophic floor is retained and adopted into
    state, allowing the normal managed-stop logic to tighten it later.
    """
    qty = position_quantity(position)
    if qty <= 0:
        return None

    entry_price = float(position["avg_entry_price"])
    catastrophic_floor = catastrophic_floor_price(config, entry_price)
    current_price = float(position.get("current_price") or entry_price)
    # A sell stop cannot safely be submitted above the current market.  On a
    # restart after a gap, place it just below the observed price immediately.
    valid_floor = min(catastrophic_floor, current_price * 0.975)
    rounded_floor = dollars(valid_floor)
    open_stops = client.open_stop_orders(config["symbol"])
    adequate = [
        order
        for order in open_stops
        if int(float(order.get("qty") or 0)) == qty
        and float(order.get("stop_price") or 0) >= float(rounded_floor)
    ]
    if adequate:
        selected = max(adequate, key=lambda order: float(order.get("stop_price") or 0))
        state["active_stop_order_id"] = selected["id"]
        state["active_stop_price"] = dollars(float(selected["stop_price"]))
        state["active_stop_qty"] = qty
        update_stop_order(
            client,
            config["symbol"],
            qty,
            float(selected["stop_price"]),
            state,
        )
        return {
            "order_id": selected["id"],
            "stop_price": state["active_stop_price"],
            "qty": qty,
            "created": False,
        }

    order = update_stop_order(
        client,
        config["symbol"],
        qty,
        valid_floor,
        state,
    )
    return {
        "order_id": state["active_stop_order_id"],
        "stop_price": state["active_stop_price"],
        "qty": qty,
        "created": order is not None,
    }


def catastrophic_floor_price(config, entry_price):
    loss_percent = float(
        risk_setting(
            config,
            "catastrophic_stop_loss_percent",
            DEFAULT_CATASTROPHIC_STOP_LOSS_PERCENT,
        )
    )
    if not 0 < loss_percent < 100:
        raise ValueError("catastrophic_stop_loss_percent must be between 0 and 100")
    return entry_price * (1 - loss_percent / 100)


def managed_initial_floor_price(config, entry_price, context=None, plan=None):
    """Return the shared live/research floor used before trailing activates."""
    floors = [
        initial_floor_price(config, entry_price, context),
        catastrophic_floor_price(config, entry_price),
    ]
    planned_stop = float((plan or {}).get("stop_price") or 0)
    if 0 < planned_stop < entry_price:
        floors.append(planned_stop)
    return max(floors)


def adverse_reduction_details(config, entry_price, qty):
    trigger_percent = float(
        risk_setting(
            config,
            "adverse_reduction_trigger_percent",
            DEFAULT_ADVERSE_REDUCTION_TRIGGER_PERCENT,
        )
    )
    fraction = float(
        risk_setting(
            config,
            "adverse_reduction_fraction",
            DEFAULT_ADVERSE_REDUCTION_FRACTION,
        )
    )
    if not 0 < trigger_percent < 100:
        raise ValueError("adverse_reduction_trigger_percent must be between 0 and 100")
    if not 0 < fraction <= 1:
        raise ValueError("adverse_reduction_fraction must be greater than 0 and at most 1")
    return {
        "trigger_percent": trigger_percent,
        "trigger_price": entry_price * (1 - trigger_percent / 100),
        "qty": min(qty, max(1, math.ceil(qty * fraction))),
        "fraction": fraction,
    }


def submit_adverse_reduction(client, config, position, current_price, state):
    if state.get("adverse_reduction_completed"):
        return None

    qty = position_quantity(position)
    if qty <= 0:
        return None
    entry_price = float(position["avg_entry_price"])
    details = adverse_reduction_details(config, entry_price, qty)
    if current_price > details["trigger_price"]:
        return None

    transaction = (
        client.symbol_transaction(config["symbol"])
        if hasattr(client, "symbol_transaction")
        else nullcontext()
    )
    with transaction:
        canceled_stop_order_ids = []
        for order in client.open_stop_orders(config["symbol"]):
            cancel_symbol_order(client, order["id"], config["symbol"])
            canceled_stop_order_ids.append(order["id"])
        for key in ("active_stop_order_id", "active_stop_price", "active_stop_qty"):
            state.pop(key, None)

        order = client.submit_order(
            {
                "symbol": config["symbol"],
                "qty": str(details["qty"]),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": action_client_order_id(
                    "hard-reduce",
                    config["symbol"],
                    (state.get("position_episode") or {}).get("episode_id", config["symbol"]),
                ),
            }
        )
    state["adverse_reduction_order_id"] = order["id"]
    state["adverse_reduction_qty"] = details["qty"]
    state["adverse_reduction_trigger_price"] = details["trigger_price"]
    return {
        "status": "adverse_reduction_order_submitted",
        "order_id": order["id"],
        "qty": details["qty"],
        "position_qty": qty,
        "current_price": current_price,
        **details,
        "canceled_stop_order_ids": canceled_stop_order_ids,
    }


def resolve_adverse_reduction_order(client, state):
    order_id = state.get("adverse_reduction_order_id")
    if not order_id:
        return None
    order = client.order(order_id)
    if order_is_open(order):
        return {
            "status": "adverse_reduction_order_pending",
            "order_id": order_id,
            "order_status": order.get("status"),
        }
    state.pop("adverse_reduction_order_id", None)
    if order.get("status") == "filled":
        state["adverse_reduction_completed"] = True
        return {
            "status": "adverse_reduction_filled",
            "order_id": order_id,
            "filled_qty": order.get("filled_qty") or order.get("qty"),
            "filled_avg_price": order.get("filled_avg_price"),
        }
    return {
        "status": "adverse_reduction_not_filled",
        "order_id": order_id,
        "order_status": order.get("status"),
    }


def position_health_config(config):
    nested = dict(config.get("position_health") or {})
    for key in (
        "catastrophic_stop_loss_percent",
        "adverse_reduction_trigger_percent",
    ):
        if key in config and key not in nested:
            nested[key] = config[key]
    if "adverse_reduction_trigger_percent" in nested:
        nested.setdefault(
            "hard_reduction_loss_percent",
            nested["adverse_reduction_trigger_percent"],
        )
    nested.setdefault("enabled", True)
    nested.setdefault("shadow_mode", True)
    nested.setdefault("timeframe", "1Hour")
    # Alpaca limits minute-based timeframe multipliers to 59. Preserve
    # compatibility with older TraderBot configs while sending the supported
    # hourly representation to the market-data API.
    if str(nested["timeframe"]).strip().lower() == "60min":
        nested["timeframe"] = "1Hour"
    nested.setdefault("lookback_days", 45)
    nested.setdefault("refresh_seconds", 3300)
    nested.setdefault("reduction_confirmation_bars", 2)
    nested.setdefault("exit_confirmation_bars", 2)
    nested.setdefault("health_reduction_fraction", 0.5)
    nested.setdefault("minimum_add_health_score", 80)
    nested.setdefault("minimum_add_setup_score", 85)
    nested.setdefault("minimum_add_remaining_r", 1.5)
    nested.setdefault("max_adds_per_episode", 1)
    nested.setdefault("max_add_fraction_of_initial_qty", 0.5)
    return nested


def timeframe_minutes(timeframe):
    value = str(timeframe or "1Hour").strip().lower()
    if value.endswith("min"):
        return max(1, int(value[:-3]))
    if value.endswith("hour"):
        return max(1, int(value[:-4])) * 60
    if value.endswith("day"):
        return max(1, int(value[:-3])) * 1440
    raise ValueError(f"unsupported position health timeframe: {timeframe}")


def entry_setup_score(plan):
    if not plan:
        return None
    if plan.get("setup_score") is not None:
        return int(plan["setup_score"])
    blockers = set(plan.get("blockers") or [])
    return round(max(0, 13 - len(blockers)) / 13 * 100)


ENTRY_MODE_MODEL_IDS = {
    "dynamic_pullback_reclaim": "pullback_reclaim",
    "dynamic_breakout_continuation": "breakout_continuation",
}


def entry_model_identity(state, plan, episode=None):
    episode = episode or {}
    explicit_model_id = (
        episode.get("origin_model_id")
        or state.get("pending_entry_model_id")
        or plan.get("model_id")
    )
    mode = episode.get("entry_setup_mode") or plan.get("mode")
    model_id = explicit_model_id or ENTRY_MODE_MODEL_IDS.get(mode) or "legacy_combined"
    explicit_version = (
        episode.get("origin_model_version")
        or state.get("pending_entry_model_version")
        or plan.get("model_version")
    )
    return model_id, int(explicit_version or 0)


def selected_entry_candidate_snapshot(state, plan, model_id):
    candidate = next(
        (
            item
            for item in (plan.get("entry_candidates") or [])
            if item.get("model_id") == model_id
        ),
        None,
    )
    source = candidate or {
        "model_id": model_id,
        "model_version": plan.get("model_version"),
        "status": plan.get("status"),
        "as_of": plan.get("last_bar_time"),
        "setup_score": plan.get("setup_score"),
        "checks": plan.get("model_checks") or {},
        "blockers": plan.get("blockers") or [],
        "limit_price": plan.get("limit_price"),
        "stop_price": plan.get("stop_price"),
        "target_price": plan.get("target_price"),
        "risk_per_share": plan.get("risk_per_share"),
        "expected_reward_risk": plan.get("expected_reward_risk"),
    }
    return json.loads(json.dumps(source, sort_keys=True, default=str))


def candidate_snapshot_hash(snapshot):
    payload = json.dumps(snapshot or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def migrate_position_episode_identity(state, episode):
    plan = episode.get("entry_plan") or state.get("dynamic_entry_plan") or {}
    model_id, model_version = entry_model_identity(state, plan, episode=episode)
    episode.setdefault("origin_model_id", model_id)
    episode.setdefault("origin_model_version", model_version)
    if "entry_candidate_snapshot" not in episode:
        episode["entry_candidate_snapshot"] = selected_entry_candidate_snapshot(
            state, plan, episode["origin_model_id"]
        )
    episode.setdefault(
        "entry_candidate_hash",
        candidate_snapshot_hash(episode["entry_candidate_snapshot"]),
    )
    episode.setdefault(
        "entry_model_checks",
        dict(episode["entry_candidate_snapshot"].get("checks") or {}),
    )
    episode.setdefault(
        "entry_model_blockers",
        list(episode["entry_candidate_snapshot"].get("blockers") or []),
    )
    return episode


def initial_entry_target(config, state, entry_price):
    plan = state.get("dynamic_entry_plan") or {}
    planned_target = float(plan.get("target_price") or 0)
    if planned_target > entry_price:
        return planned_target, "entry_structural_target"
    atr = float(plan.get("atr14") or 0)
    if plan.get("mode") == "dynamic_breakout_continuation":
        target = float(plan.get("breakout_limit") or entry_price) + 2 * atr
        source = "entry_breakout_plus_2atr"
    else:
        target = float(plan.get("breakout_trigger") or 0)
        source = "entry_recent_high"
    if target <= entry_price:
        risk_percent = float(
            risk_setting(
                config,
                "catastrophic_stop_loss_percent",
                DEFAULT_CATASTROPHIC_STOP_LOSS_PERCENT,
            )
        )
        target = entry_price * (1 + 1.5 * risk_percent / 100)
        source = "reconstructed_1_5r_target"
    return target, source


def ensure_position_episode(config, state, position, entry_order=None):
    qty = position_quantity(position)
    if qty <= 0:
        return None
    entry_price = float(position.get("avg_entry_price") or 0)
    episode = state.get("position_episode")
    if episode and episode.get("symbol") == config["symbol"]:
        migrate_position_episode_identity(state, episode)
        episode["current_qty"] = qty
        episode["average_entry_price"] = entry_price
        return episode

    target_price, target_source = initial_entry_target(config, state, entry_price)
    opened_at = (
        (entry_order or {}).get("filled_at")
        or (entry_order or {}).get("created_at")
        or iso_utc(datetime.datetime.now(datetime.timezone.utc))
    )
    entry_order_id = (entry_order or {}).get("id") or state.get("current_entry_order_id")
    episode_id = f"{config['symbol']}:{entry_order_id or opened_at}"
    plan = state.get("dynamic_entry_plan") or {}
    model_id, model_version = entry_model_identity(state, plan)
    candidate_snapshot = selected_entry_candidate_snapshot(state, plan, model_id)
    episode = {
        "episode_id": episode_id,
        "symbol": config["symbol"],
        "state": "OPEN_PARTIAL"
        if (entry_order or {}).get("status") == "partially_filled"
        else "OPEN",
        "state_version": 1,
        "opened_at": opened_at,
        "entry_order_id": entry_order_id,
        "entry_setup_score": entry_setup_score(plan),
        "entry_setup_status": plan.get("status"),
        "entry_setup_mode": plan.get("mode"),
        "entry_setup_as_of": plan.get("last_bar_time"),
        "entry_plan": serializable_plan(plan) if plan else {},
        "origin_model_id": model_id,
        "origin_model_version": model_version,
        "entry_candidate_snapshot": candidate_snapshot,
        "entry_candidate_hash": candidate_snapshot_hash(candidate_snapshot),
        "entry_model_checks": dict(candidate_snapshot.get("checks") or {}),
        "entry_model_blockers": list(candidate_snapshot.get("blockers") or []),
        "initial_stop_price": plan.get("stop_price"),
        "initial_risk_per_share": plan.get("risk_per_share"),
        "original_target_price": target_price,
        "target_source": target_source,
        "initial_qty": qty,
        "current_qty": qty,
        "average_entry_price": entry_price,
        "add_count": 0,
        "adverse_reduction_completed": bool(state.get("adverse_reduction_completed")),
    }
    state["position_episode"] = episode
    if entry_order_id:
        state["managed_entry_order_id"] = entry_order_id
    return episode


def position_health_session_close(client, now):
    """Return the latest session close only while the regular market is closed."""
    if not callable(getattr(client, "calendar", None)):
        return None
    local_now = now.astimezone(ZoneInfo("America/New_York"))
    sessions = client.calendar(
        (local_now.date() - datetime.timedelta(days=14)).isoformat(),
        local_now.date().isoformat(),
    )
    latest_close = None
    for session in sorted(sessions, key=lambda item: item["date"]):
        opened = datetime.datetime.fromisoformat(
            f'{session["date"]}T{session["open"]}'
        ).replace(tzinfo=local_now.tzinfo)
        closed = datetime.datetime.fromisoformat(
            f'{session["date"]}T{session["close"]}'
        ).replace(tzinfo=local_now.tzinfo)
        if opened <= local_now < closed:
            return None
        if closed <= local_now:
            latest_close = closed
    return latest_close.isoformat() if latest_close else None


def position_health_market_context(client, config):
    settings = position_health_config(config)
    symbol = config["symbol"]
    benchmark_symbol = str(
        (settings.get("benchmark_by_symbol") or {}).get(symbol)
        or settings.get("benchmark_symbol")
        or MARKET_SYMBOL
    ).upper()
    now = datetime.datetime.now(datetime.timezone.utc)
    session_close = position_health_session_close(client, now)
    start = iso_utc(now - datetime.timedelta(days=int(settings["lookback_days"])))
    # The broker's end bound is inclusive; omit the bar starting at the close.
    end = (
        iso_utc(parse_alpaca_time(session_close) - datetime.timedelta(seconds=1))
        if session_close else iso_utc(now)
    )
    timeframe = settings["timeframe"]
    bars = calculate_indicators(
        completed_market_bars(
            client.stock_bars(symbol, start, end, timeframe), timeframe, now
        )
    )
    market_bars = calculate_indicators(
        completed_market_bars(
            client.stock_bars(benchmark_symbol, start, end, timeframe), timeframe, now
        )
    )
    if len(bars) < 51 or len(market_bars) < 51:
        return {
            "as_of": bars[-1]["t"].isoformat() if bars else None,
            "bar_id": None,
            "timeframe_minutes": timeframe_minutes(timeframe),
            "latest_bar": None,
            "relative_strength_5d": None,
            "market_ok": None,
            "benchmark_symbol": benchmark_symbol,
        }

    latest = dict(bars[-1])
    prior_slope = bars[-6]
    latest["ema21_slope"] = (
        (latest["ema21"] - prior_slope["ema21"]) / prior_slope["ema21"]
        if prior_slope["ema21"]
        else 0
    )
    bars_per_day = max(1, round(390 / timeframe_minutes(timeframe)))
    relative_lookback = min(len(bars) - 1, len(market_bars) - 1, bars_per_day * 5)
    stock_return = bars[-1]["c"] / bars[-1 - relative_lookback]["c"] - 1
    market_return = market_bars[-1]["c"] / market_bars[-1 - relative_lookback]["c"] - 1
    as_of = latest["t"].isoformat()
    return {
        "as_of": as_of,
        "session_close": session_close,
        "bar_id": f"{symbol}:{timeframe}:{as_of}",
        "timeframe_minutes": timeframe_minutes(timeframe),
        "latest_bar": latest,
        "relative_strength_5d": stock_return - market_return,
        "market_ok": market_ok_at(market_bars, latest["t"]),
        "benchmark_symbol": benchmark_symbol,
    }


def health_refresh_due(state, settings, now=None):
    previous = parse_alpaca_time(state.get("position_health_last_evaluated_at"))
    if previous is None or not state.get("position_health"):
        return True
    checked_at = now or datetime.datetime.now(datetime.timezone.utc)
    return (checked_at - previous).total_seconds() >= int(settings["refresh_seconds"])


def refresh_position_health(client, config, state, position, force=False):
    if not position or position_quantity(position) <= 0:
        return None
    settings = position_health_config(config)
    if not settings.get("enabled", True):
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    if not force and not health_refresh_due(state, settings, now=now):
        return state.get("position_health")

    episode = ensure_position_episode(config, state, position)
    protection = {
        "stop_price": state.get("active_stop_price"),
        "stop_qty": state.get("active_stop_qty", 0),
    }
    try:
        context = position_health_market_context(client, config)
        if episode.get("target_source") == "reconstructed_1_5r_target":
            latest_bar = context.get("latest_bar") or {}
            atr = float(latest_bar.get("atr14") or 0)
            if atr > 0:
                entry_price = float(episode["average_entry_price"])
                episode["original_target_price"] = entry_price + 2 * atr
                episode["target_source"] = "reconstructed_current_2atr"
        assessment = evaluate_position_health(
            position,
            episode,
            context,
            protection,
            settings,
            now=now,
        )
    except Exception as exc:
        assessment = evaluate_position_health(
            position,
            episode,
            {},
            protection,
            settings,
            now=now,
        )
        assessment["reasons"] = sorted(
            set(assessment.get("reasons", []))
            | {f"health_context_error:{type(exc).__name__}"}
        )

    previous = state.get("position_health") or {}
    confirmation = state.get("position_health_confirmation") or {}
    if assessment.get("bar_id") and assessment.get("bar_id") != previous.get("bar_id"):
        action = assessment.get("recommended_action")
        if action in ("reduce", "exit"):
            count = confirmation.get("count", 0) + 1 if confirmation.get("action") == action else 1
            state["position_health_confirmation"] = {
                "action": action,
                "count": count,
                "last_bar_id": assessment["bar_id"],
            }
        else:
            state["position_health_confirmation"] = {
                "action": action,
                "count": 0,
                "last_bar_id": assessment["bar_id"],
            }
    state["position_health"] = assessment
    state["position_health_last_evaluated_at"] = iso_utc(now)
    episode["current_qty"] = position_quantity(position)
    episode["average_entry_price"] = float(position.get("avg_entry_price") or 0)
    episode["latest_health_state"] = assessment.get("state")
    episode["latest_health_score"] = assessment.get("score")
    return assessment


def submit_confirmed_health_action(client, config, state, position, health):
    settings = position_health_config(config)
    if settings.get("shadow_mode", True) or not health:
        return None
    action = health.get("recommended_action")
    if action not in ("reduce", "exit"):
        return None
    if action == "reduce" and state.get("adverse_reduction_completed"):
        return None

    confirmation = state.get("position_health_confirmation") or {}
    required = int(
        settings[
            "exit_confirmation_bars"
            if action == "exit"
            else "reduction_confirmation_bars"
        ]
    )
    if confirmation.get("action") != action or int(confirmation.get("count", 0)) < required:
        return None

    position_qty = position_quantity(position)
    if position_qty <= 0:
        return None
    if action == "exit":
        action_qty = position_qty
    else:
        fraction = float(settings["health_reduction_fraction"])
        if not 0 < fraction <= 1:
            raise ValueError("health_reduction_fraction must be greater than 0 and at most 1")
        action_qty = min(position_qty, max(1, math.ceil(position_qty * fraction)))

    transaction = (
        client.symbol_transaction(config["symbol"])
        if hasattr(client, "symbol_transaction")
        else nullcontext()
    )
    with transaction:
        canceled_stop_order_ids = []
        for order in client.open_stop_orders(config["symbol"]):
            cancel_symbol_order(client, order["id"], config["symbol"])
            canceled_stop_order_ids.append(order["id"])
        for key in ("active_stop_order_id", "active_stop_price", "active_stop_qty"):
            state.pop(key, None)

        order = client.submit_order(
            {
                "symbol": config["symbol"],
                "qty": str(action_qty),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": action_client_order_id(
                    f"health-{action}",
                    config["symbol"],
                    (state.get("position_episode") or {}).get("episode_id", config["symbol"]),
                ),
            }
        )
    state["adverse_reduction_order_id"] = order["id"]
    state["adverse_reduction_qty"] = action_qty
    state["position_health_action_reason"] = f"health_{action}"
    episode = state.get("position_episode") or {}
    episode["state"] = "EXIT_PENDING" if action == "exit" else "REDUCE_PENDING"
    episode["state_version"] = int(episode.get("state_version", 0)) + 1
    return {
        "status": f"position_health_{action}_order_submitted",
        "order_id": order["id"],
        "qty": action_qty,
        "position_qty": position_qty,
        "health_score": health.get("score"),
        "health_state": health.get("state"),
        "confirmation_bars": confirmation.get("count"),
        "canceled_stop_order_ids": canceled_stop_order_ids,
        "position_health": health,
    }


def submit_position_health_add(client, config, state, position, health, stop_price):
    settings = position_health_config(config)
    if settings.get("shadow_mode", True) or not settings.get("additions_enabled", False):
        return None
    if not health or not health.get("data_fresh"):
        return None

    episode = state.get("position_episode") or {}
    current_qty = position_quantity(position)
    if current_qty <= 0 or client.open_buy_orders(config["symbol"]):
        return None

    entry_assessment = {
        "score": episode.get("entry_setup_score"),
        "status": episode.get("entry_setup_status"),
    }
    account = client.account()
    eligibility_config = {
        **settings,
        "max_symbol_notional_percent": float(
            settings.get("max_symbol_notional_percent", 20)
        ),
    }
    eligibility = evaluate_add_eligibility(
        health,
        entry_assessment,
        episode,
        position,
        {"stop_price": stop_price, "stop_qty": current_qty},
        float(account.get("equity") or 0),
        eligibility_config,
    )
    if not eligibility["eligible"]:
        state["position_health_add_eligibility"] = eligibility
        return None

    current_price = float(position.get("current_price") or 0)
    add_qty, cash_detail = cash_protected_quantity(
        client,
        config,
        int(eligibility["qty"]),
        current_price,
    )
    if add_qty <= 0:
        state["position_health_add_eligibility"] = {
            **eligibility,
            "eligible": False,
            "qty": 0,
            "reasons": sorted(set(eligibility["reasons"] + ["cash_reserve_blocked"])),
            "cash": cash_detail,
        }
        return None

    with (
        client.symbol_transaction(config["symbol"])
        if hasattr(client, "symbol_transaction")
        else nullcontext()
    ):
        order = client.submit_order(
            {
                "symbol": config["symbol"],
                "qty": str(add_qty),
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": action_client_order_id(
                    "health-add",
                    config["symbol"],
                    episode.get("episode_id", config["symbol"]),
                ),
            }
        )

    episode["add_count"] = int(episode.get("add_count", 0)) + 1
    episode["state"] = "OPEN"
    episode["state_version"] = int(episode.get("state_version", 0)) + 1
    state["position_health_add_order_id"] = order["id"]
    state["position_health_action_reason"] = "health_add"
    state["position_health_add_eligibility"] = {
        **eligibility,
        "qty": add_qty,
        "cash": cash_detail,
    }
    return {
        "status": "position_health_add_order_submitted",
        "order_id": order["id"],
        "qty": add_qty,
        "position_qty": current_qty,
        "resulting_position_qty": current_qty + add_qty,
        "health_score": health.get("score"),
        "position_health": health,
        "eligibility": state["position_health_add_eligibility"],
    }


def position_action_intents(config, state, position, current_price, health):
    """Return mutually competing post-fill intents without changing broker state."""
    intents = []
    episode_id = (state.get("position_episode") or {}).get(
        "episode_id", config["symbol"]
    )
    qty = position_quantity(position)
    if qty > 0 and not state.get("adverse_reduction_completed"):
        details = adverse_reduction_details(
            config, float(position["avg_entry_price"]), qty
        )
        if current_price <= details["trigger_price"]:
            intents.append(
                {
                    "action_id": action_client_order_id(
                        "hard-reduce", config["symbol"], episode_id
                    ),
                    "action": "reduce",
                    "source": "hard_adverse_reduction",
                    "priority": 88,
                    "reason": "hard_reduction_threshold",
                }
            )

    settings = position_health_config(config)
    if not settings.get("shadow_mode", True) and health:
        action = health.get("recommended_action")
        if action in ("reduce", "exit"):
            confirmation = state.get("position_health_confirmation") or {}
            required = int(
                settings[
                    "exit_confirmation_bars"
                    if action == "exit"
                    else "reduction_confirmation_bars"
                ]
            )
            if (
                confirmation.get("action") == action
                and int(confirmation.get("count", 0)) >= required
                and not (action == "reduce" and state.get("adverse_reduction_completed"))
            ):
                intents.append(
                    {
                        "action_id": action_client_order_id(
                            f"health-{action}", config["symbol"], episode_id
                        ),
                        "action": action,
                        "source": "position_health",
                        "reason": f"confirmed_health_{action}",
                    }
                )
        if (
            settings.get("additions_enabled", False)
            and health.get("data_fresh")
            and health.get("recommended_action") == "hold"
        ):
            intents.append(
                {
                    "action_id": action_client_order_id(
                        "health-add", config["symbol"], episode_id
                    ),
                    "action": "add",
                    "source": "position_health",
                    "reason": "health_add_candidate",
                }
            )
    return intents


def submit_coordinated_position_action(
    client,
    config,
    state,
    position,
    health,
    current_price,
    stop_price,
):
    intents = position_action_intents(
        config, state, position, current_price, health
    )
    selected = select_action_intent(intents)
    state["position_action_intents"] = intents
    state["selected_position_action_intent"] = selected
    if not selected:
        return None
    if selected["source"] == "hard_adverse_reduction":
        return submit_adverse_reduction(
            client, config, position, current_price, state
        )
    if selected["action"] in ("reduce", "exit"):
        return submit_confirmed_health_action(
            client, config, state, position, health
        )
    if selected["action"] == "add":
        return submit_position_health_add(
            client, config, state, position, health, stop_price
        )
    return None


def cancel_tracked_legacy_ladder_orders(client, config, state):
    """Cancel only bot-tracked legacy ladder buys, never untracked/manual orders."""
    tracked = dict(state.get("ladder_order_ids") or {})
    canceled = []
    retained = {}
    for step_key, order_id in tracked.items():
        try:
            order = client.order(order_id)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if (
            order_is_open(order)
            and order.get("side") == "buy"
            and order.get("symbol") == config["symbol"]
        ):
            cancel_symbol_order(client, order_id, config["symbol"])
            canceled.append(order_id)
        elif order_is_open(order):
            retained[step_key] = order_id
    state["ladder_order_ids"] = retained
    if canceled:
        state["canceled_legacy_ladder_order_ids"] = sorted(
            set((state.get("canceled_legacy_ladder_order_ids") or []) + canceled)
        )
    return canceled


def observe_ladder_opportunities(config, state, fill_price, current_price, context, health):
    """Retain ladder diagnostics without granting them order authority."""
    observations = []
    max_ladder_count, ladder_limit_detail = adaptive_ladder_limit(config, context)
    if state.get("adverse_reduction_completed"):
        max_ladder_count = 0
        ladder_limit_detail = {
            **ladder_limit_detail,
            "adverse_reduction_ladder_lockout": True,
        }
    for ladder_step in ladder_steps(config, fill_price, context):
        if current_price > ladder_step["trigger_price"]:
            continue
        observations.append(
            {
                **ladder_step,
                "status": "position_health_authority_required",
                "observation_only": True,
                "proposed_qty": int(config.get("ladder_buy_quantity", 0)),
                "position_health_state": (health or {}).get("state"),
                "position_health_score": (health or {}).get("score"),
                "ladder_limit": ladder_limit_detail,
                "max_ladder_count": max_ladder_count,
            }
        )
    return observations, ladder_limit_detail


def update_dynamic_pending_order(client, config, state, order_id, plan):
    order = client.order(order_id)
    if order_is_open(order):
        plan_model_id = plan.get("model_id")
        pending_model_id = state.get("pending_entry_model_id")
        if not pending_model_id and plan.get("status") == "active_signal":
            pending_model_id = plan_model_id
            state["pending_entry_model_id"] = plan_model_id
            state["pending_entry_model_version"] = plan.get("model_version")
            state["pending_entry_candidate_as_of"] = plan.get("last_bar_time")
        if plan.get("status") != "active_signal" or not plan_model_id:
            cancel_symbol_order(client, order_id, config["symbol"])
            reset_entry_tracking_state(state)
            return {
                "status": "dynamic_entry_order_canceled_signal_inactive",
                "old_order_id": order_id,
                "pending_model_id": pending_model_id,
                "classified_model_id": plan.get("classified_model_id"),
                "dynamic_plan": serializable_plan(plan),
            }
        if pending_model_id and pending_model_id != plan_model_id:
            cancel_symbol_order(client, order_id, config["symbol"])
            reset_entry_tracking_state(state)
            return {
                "status": "dynamic_entry_order_canceled_model_switch",
                "old_order_id": order_id,
                "old_model_id": pending_model_id,
                "new_model_id": plan_model_id,
                "dynamic_plan": serializable_plan(plan),
            }
        desired_limit = plan.get("limit_price")
        if (
            config.get("dynamic_replace_open_orders", True)
            and plan.get("status") == "active_signal"
            and desired_limit
        ):
            current_qty = int(float(order.get("qty") or 0))
            desired_qty = dynamic_entry_quantity(
                config, plan, equity=float(client.account().get("equity") or 0)
            )
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
                cancel_symbol_order(client, order_id, config["symbol"])
                reset_entry_tracking_state(state)
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
                cancel_symbol_order(client, order_id, config["symbol"])
                reset_entry_tracking_state(state)
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
                state["pending_entry_model_id"] = plan_model_id
                state["pending_entry_model_version"] = plan.get("model_version")
                state["pending_entry_candidate_as_of"] = plan.get("last_bar_time")
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
    if order.get("status") != "filled":
        for key in (
            "pending_entry_model_id",
            "pending_entry_model_version",
            "pending_entry_candidate_as_of",
        ):
            state.pop(key, None)
    return None


def build_exit_trade_from_state(state):
    if "last_exit_price" not in state:
        return None
    return {
        "exit_price": float(state["last_exit_price"]),
        "exit_time": parse_alpaca_time(state.get("last_exit_at")),
        "realized_pl": float(state.get("last_trade_pl", 0)),
    }


def dynamic_plan_max_age_seconds(config):
    configured = config.get("dynamic_plan_max_age_seconds")
    if configured not in (None, ""):
        return int(configured)
    timeframe = str(config.get("dynamic_timeframe", "5Min")).lower()
    digits = "".join(character for character in timeframe if character.isdigit())
    minutes = int(digits or 5) if "min" in timeframe else 5
    return minutes * 60 * 2 + 60


def dynamic_plan_is_fresh(config, plan_at, now_at):
    """Keep the final regular-session plan valid after the closing bell."""
    age_seconds = max(
        0, (now_at - plan_at.astimezone(datetime.timezone.utc)).total_seconds()
    )
    if age_seconds <= dynamic_plan_max_age_seconds(config):
        return True

    plan_eastern = plan_at.astimezone(EASTERN)
    now_eastern = now_at.astimezone(EASTERN)
    market_close = datetime.time(16, 0)
    closing_window_start = datetime.time(15, 45)
    return (
        now_eastern.date() == plan_eastern.date()
        and now_eastern.time() >= market_close
        and plan_eastern.time() >= closing_window_start
    )


def evaluate_flat_entry_eligibility(config, state, plan=None, now=None, mode=None):
    """Return the shared configuration, lifecycle, cooldown, and plan decision."""
    exit_trade = build_exit_trade_from_state(state)
    mode = mode or (
        "reentry" if exit_trade or state.get("active_stop_order_id") else "new_entry"
    )
    reasons = []
    if mode == "new_entry":
        new_entry_authorized = config.get("dynamic_entry_enabled") or (
            not exit_trade
            and config.get("reentry_enabled")
            and config.get("dynamic_reentry_enabled")
        )
        if not new_entry_authorized:
            reasons.append("new_entry_disabled")
    else:
        if not config.get("reentry_enabled"):
            reasons.append("reentry_disabled")
        if not config.get("dynamic_reentry_enabled"):
            reasons.append("dynamic_reentry_disabled")
        if not exit_trade:
            reasons.append("exit_record_missing")
        if config.get("reentry_observe_only"):
            reasons.append("reentry_observe_only")

    now_at = now or datetime.datetime.now(datetime.timezone.utc)
    cooldown_remaining = 0
    if mode == "reentry" and exit_trade and exit_trade.get("exit_time"):
        cooldown = int(config.get("reentry_cooldown_seconds", 600))
        elapsed = (now_at - exit_trade["exit_time"].astimezone(datetime.timezone.utc)).total_seconds()
        cooldown_remaining = max(0, math.ceil(cooldown - elapsed))
        if cooldown_remaining:
            reasons.append("reentry_cooldown")

    plan_age_seconds = None
    if plan is not None:
        plan_at = parse_alpaca_time(plan.get("last_bar_time"))
        if plan_at:
            plan_age_seconds = max(
                0, (now_at - plan_at.astimezone(datetime.timezone.utc)).total_seconds()
            )
        if not plan_at or not dynamic_plan_is_fresh(config, plan_at, now_at):
            reasons.append("stale_entry_plan")
        if plan.get("status") != "active_signal":
            reasons.append("entry_signal_inactive")
        if config.get("portfolio_allocation_required") and (
            not config.get("portfolio_allocation_authorized")
            or config.get("portfolio_allocation_as_of") != plan.get("last_bar_time")
        ):
            reasons.append("portfolio_allocation_required")

    return {
        "eligible": not reasons,
        "mode": mode,
        "reasons": reasons,
        "cooldown_remaining_seconds": cooldown_remaining,
        "plan_age_seconds": plan_age_seconds,
        "plan_status": plan.get("status") if plan else None,
        "as_of": plan.get("last_bar_time") if plan else None,
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
    requested_qty = dynamic_entry_quantity(
        config, plan, equity=float(client.account().get("equity") or 0)
    )
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
            "client_order_id": action_client_order_id(
                "entry",
                symbol,
                f"{plan.get('model_id')}:{plan.get('last_bar_time')}",
            ),
        }
    )
    state["current_entry_order_id"] = order["id"]
    state["reentry_order_id"] = order["id"]
    state["reentry_reason"] = f"{reason_prefix}_{plan['mode']}"
    state["pending_entry_model_id"] = plan.get("model_id")
    state["pending_entry_model_version"] = plan.get("model_version")
    state["pending_entry_candidate_as_of"] = plan.get("last_bar_time")
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
    bars, market_bars, sector_bars, filter_context = live_bar_context(
        client, symbol, config
    )
    ignore_ledger = not exit_trade
    plan = dynamic_entry_plan(
        symbol,
        bars,
        market_bars,
        exit_trade,
        ignore_ledger=ignore_ledger,
        sector_bars=sector_bars,
        config=config,
        filter_context=filter_context,
    )
    state["dynamic_entry_plan"] = serializable_plan(plan)

    eligibility = evaluate_flat_entry_eligibility(
        config,
        state,
        plan,
        mode="reentry" if exit_trade else "new_entry",
    )
    state["flat_entry_eligibility"] = eligibility

    open_buy_orders = track_existing_open_buy_order(client, symbol, state)
    reentry_order_id = state.get("reentry_order_id")
    if reentry_order_id:
        pending = update_dynamic_pending_order(client, config, state, reentry_order_id, plan)
        if pending:
            return {
                **pending,
                "open_buy_order_ids": [
                    order.get("id") for order in open_buy_orders
                ],
                "dynamic_plan": serializable_plan(plan),
            }

    if not eligibility["eligible"]:
        return {
            "status": "dynamic_entry_waiting_for_signal",
            "eligibility": eligibility,
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
    if not exit_trade and not (
        config.get("dynamic_entry_enabled")
        or (config.get("reentry_enabled") and config.get("dynamic_reentry_enabled"))
    ):
        return None

    bars, market_bars, sector_bars, filter_context = live_bar_context(
        client, config["symbol"], config
    )
    plan = dynamic_entry_plan(
        config["symbol"],
        bars,
        market_bars,
        exit_trade,
        ignore_ledger=not exit_trade,
        sector_bars=sector_bars,
        config=config,
        filter_context=filter_context,
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
    canceled_legacy_ladders = cancel_tracked_legacy_ladder_orders(
        client, config, state
    )
    if canceled_legacy_ladders:
        return {
            "status": "legacy_ladder_orders_canceled",
            "canceled_order_ids": canceled_legacy_ladders,
            "position_qty": qty,
        }
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
    reduction_resolution = resolve_adverse_reduction_order(client, state)
    if (
        reduction_resolution
        and reduction_resolution["status"] == "adverse_reduction_order_pending"
    ):
        return reduction_resolution
    if reduction_resolution and reduction_resolution["status"] == "adverse_reduction_filled":
        episode = state.get("position_episode") or {}
        episode["state"] = (
            "CLOSED"
            if state.get("position_health_action_reason") == "health_exit"
            else "REDUCED"
        )
        episode["state_version"] = int(episode.get("state_version", 0)) + 1

    position = client.position(symbol)
    qty = position_quantity(position)
    catastrophic_stop = None
    if qty > 0:
        catastrophic_stop = ensure_catastrophic_stop(client, config, position, state)
        ensure_position_episode(config, state, position)
        record_position_snapshot(state, position)

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
            "catastrophic_stop": catastrophic_stop,
            "dynamic_plan": dynamic_plan,
            "dynamic_plan_error": dynamic_plan_error,
        }

    entry_order_id = state.get("current_entry_order_id") or config.get("entry_order_id")
    entry_order = client.order(entry_order_id) if entry_order_id else None

    if not entry_order_id:
        if qty <= 0:
            if config.get("dynamic_entry_enabled") or (
                not state.get("last_exit_at")
                and not state.get("active_stop_order_id")
                and config.get("reentry_enabled")
                and config.get("dynamic_reentry_enabled")
            ):
                return handle_dynamic_flat_entry(client, config, state, exit_trade=None)
            if config.get("reentry_enabled"):
                return handle_reentry(client, config, state)
            return {"status": "no_entry_order_configured", "symbol": symbol}
        state.setdefault("entry_fill_price", float(position["avg_entry_price"]))
        state.setdefault("highest_trail_rung", 0)
        state.setdefault("base_position_qty", int(config.get("entry_quantity", qty)))
        state.setdefault("ladder_filled_qty", 0)
        state.setdefault("ladder_filled_notional", 0.0)
        state.setdefault("filled_ladder_steps", [])
        state.setdefault("ladder_order_ids", {})

    if entry_order and entry_order.get("status") != "filled":
        # An unfilled or expired entry has no holding to assess.  Position
        # health remains applicable to partial fills, where qty is positive.
        health = (
            refresh_position_health(client, config, state, position)
            if qty > 0
            else None
        )
        settings = position_health_config(config)
        should_cancel_remainder = (
            qty > 0
            and not settings.get("shadow_mode", True)
            and (
                health is None
                or health.get("score") is None
                or health.get("score") < int(settings.get("stable_score", 65))
            )
        )
        if should_cancel_remainder and order_is_open(entry_order):
            cancel_symbol_order(client, entry_order["id"], symbol)
            return {
                "status": "entry_remainder_canceled_by_position_health",
                "entry_order_id": entry_order["id"],
                "entry_order_status": entry_order.get("status"),
                "catastrophic_stop": catastrophic_stop,
                "position_health": health,
            }
        if order_is_open(entry_order) and (
            config.get("dynamic_entry_enabled")
            or config.get("dynamic_reentry_enabled")
        ):
            plan = refresh_dynamic_plan_only(client, config, state)
            if plan:
                pending = update_dynamic_pending_order(
                    client,
                    config,
                    state,
                    entry_order["id"],
                    plan,
                )
                if pending:
                    return {
                        **pending,
                        "catastrophic_stop": catastrophic_stop,
                        "position_health": health,
                        "dynamic_plan": serializable_plan(plan),
                    }
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
            "catastrophic_stop": catastrophic_stop,
            "position_health": health,
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
    base_floor = managed_initial_floor_price(
        config,
        fill_price,
        context,
        plan=(state.get("position_episode") or {}).get("entry_plan")
        or state.get("dynamic_entry_plan"),
    )
    initial_risk = float(
        (state.get("position_episode") or {}).get("initial_risk_per_share") or 0
    )
    if initial_risk > 0:
        current_rung = int((current_price - fill_price) / initial_risk)
    else:
        trail_step = config["trail_trigger_step_percent"] / 100
        current_rung = int((current_price / fill_price - 1) / trail_step)
    current_rung = max(0, current_rung)

    if current_rung > state["highest_trail_rung"]:
        state["highest_trail_rung"] = current_rung

    if state["highest_trail_rung"] > 0:
        trail_below = trail_below_current_percent(config, state["highest_trail_rung"])
        candidate_floor = max(
            fill_price,
            current_price * (1 - trail_below / 100),
        )
    else:
        candidate_floor = base_floor

    previous_floor = float(state.get("floor_price", 0))
    floor_price = max(previous_floor, base_floor, candidate_floor)
    state["floor_price"] = floor_price

    effective_stop_price, recovery_stop = effective_managed_stop_price(
        config,
        state,
        current_price,
        floor_price,
    )
    stop_order = update_stop_order(
        client,
        symbol,
        qty,
        effective_stop_price,
        state,
    )
    position_for_health = dict(position)
    position_for_health["current_price"] = current_price
    health = refresh_position_health(
        client,
        config,
        state,
        position_for_health,
    )
    position_action = submit_coordinated_position_action(
        client,
        config,
        state,
        position_for_health,
        health,
        current_price,
        effective_stop_price,
    )
    if position_action:
        return position_action

    ladder_opportunities, ladder_limit_detail = observe_ladder_opportunities(
        config, state, fill_price, current_price, context, health
    )

    return {
        "status": "managed",
        "entry_fill_price": fill_price,
        "current_price": current_price,
        "position_qty": qty,
        "floor_price": floor_price,
        "effective_stop_price": effective_stop_price,
        **recovery_stop,
        "highest_trail_rung": state["highest_trail_rung"],
        "updated_stop_order": stop_order["id"] if stop_order else None,
        "position_health": health,
        "new_ladder_orders": [],
        "ladder_sizing": [],
        "adaptive_ladder": [],
        "ladder_limit": ladder_limit_detail,
        "ladder_caps": [],
        "ladder_risk_caps": [],
        "ladder_opportunities": ladder_opportunities,
        "skipped_ladder_orders": ladder_opportunities,
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
    from traderbot.broker.execution_gateway import ExecutionGateway

    client = ExecutionGateway(AlpacaClient())

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
