import argparse
import bisect
import collections
import datetime
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traderbot.backtester.reentry_backtest import load_strategy_configs
from traderbot.core_strategy_engine.engine import (
    adaptive_ladder_limit,
    adaptive_ladder_quantity,
    adverse_reduction_details,
    calculate_indicators,
    dynamic_entry_plan,
    evaluate_add_eligibility,
    evaluate_position_health,
    entry_setup_score,
    initial_entry_target,
    initial_floor_price,
    iso_utc,
    ladder_steps,
    load_env,
    managed_initial_floor_price,
    market_ok_at,
    position_health_config,
    risk_setting,
    trail_below_current_percent,
)


TIMEFRAME = "5Min"
ACCOUNT_EQUITY = 100000.0
START = datetime.datetime(2025, 7, 7, 20, 0, tzinfo=datetime.timezone.utc)
END = datetime.datetime(2026, 7, 7, 20, 0, tzinfo=datetime.timezone.utc)


def fetch_bars(
    symbol,
    data_url,
    headers,
    timeframe=TIMEFRAME,
    start=START,
    end=END,
):
    bars = []
    page_token = None
    while True:
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": iso_utc(start),
            "end": iso_utc(end),
            "adjustment": "raw",
            "feed": "iex",
            "limit": "10000",
        }
        if page_token:
            params["page_token"] = page_token
        request = urllib.request.Request(
            f"{data_url}/stocks/bars?{urllib.parse.urlencode(params)}",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        bars.extend(payload.get("bars", {}).get(symbol, []))
        page_token = payload.get("next_page_token")
        if not page_token:
            return calculate_indicators(bars)
        time.sleep(0.05)


def cap_qty(config, desired_qty, price, current_qty):
    qty = int(desired_qty)
    max_total = risk_setting(config, "max_total_position_qty")
    if max_total not in (None, ""):
        qty = min(qty, max(0, int(float(max_total)) - current_qty))
    max_notional_pct = risk_setting(config, "max_symbol_notional_percent")
    if max_notional_pct not in (None, "", 0):
        cap_notional = ACCOUNT_EQUITY * float(max_notional_pct) / 100
        qty = min(qty, max(0, math.floor(cap_notional / price) - current_qty))
    return max(0, qty)


def ladder_cap_qty(config, open_pos, desired_qty, price):
    qty = int(desired_qty)
    max_ladder_notional_pct = risk_setting(config, "max_ladder_notional_percent")
    if max_ladder_notional_pct not in (None, "", 0):
        max_ladder_notional = ACCOUNT_EQUITY * float(max_ladder_notional_pct) / 100
        remaining_notional = max(0, max_ladder_notional - open_pos.get("ladder_notional", 0.0))
        qty = min(qty, math.floor(remaining_notional / price))

    max_ladder_multiple = risk_setting(config, "max_ladder_position_multiple")
    if max_ladder_multiple not in (None, "", 0):
        max_position_qty = math.floor(open_pos["base_qty"] * float(max_ladder_multiple))
        qty = min(qty, max(0, max_position_qty - open_pos["qty"]))

    return max(0, qty)


def dynamic_qty(config, plan, equity=None):
    limit_price = float(plan["limit_price"])
    stop_price = float(plan.get("stop_price") or 0)
    risk_per_share = limit_price - stop_price
    has_structural_stop = 0 < stop_price < limit_price
    if equity not in (None, "", 0) and has_structural_stop:
        risk_budget = float(equity) * float(config.get("risk_per_trade_percent", 0.5)) / 100
        return max(0, math.floor(risk_budget / risk_per_share))
    notional_key = (
        "dynamic_market_filter_ignored_notional"
        if plan.get("market_filter_ignored")
        else "dynamic_entry_notional"
    )
    if config.get(notional_key):
        return max(1, math.floor(float(config[notional_key]) / limit_price))
    return int(config.get("entry_quantity", 1))


def plan_model_id(plan):
    if plan.get("model_id"):
        return plan["model_id"]
    return {
        "dynamic_pullback_reclaim": "pullback_reclaim",
        "dynamic_breakout_continuation": "breakout_continuation",
    }.get(plan.get("mode"), "legacy_combined")


def model_attribution(trades, candidate_counts=None):
    candidate_counts = candidate_counts or {}
    models = sorted(
        set(candidate_counts)
        | {trade.get("entry_model_id", "legacy_combined") for trade in trades}
    )
    attribution = {}
    for model_id in models:
        model_trades = [
            trade
            for trade in trades
            if trade.get("entry_model_id", "legacy_combined") == model_id
        ]
        profits = [float(trade.get("pnl") or 0) for trade in model_trades]
        gross_profit = sum(value for value in profits if value > 0)
        gross_loss = -sum(value for value in profits if value < 0)
        realized_r = [
            float(trade.get("realized_r"))
            for trade in model_trades
            if trade.get("realized_r") is not None
        ]
        counts = candidate_counts.get(model_id, {})
        attribution[model_id] = {
            "candidate_evaluations": int(counts.get("evaluated", 0)),
            "qualified_signals": int(counts.get("qualified", 0)),
            "selected_signals": int(counts.get("selected", 0)),
            "trades": len(model_trades),
            "wins": sum(1 for value in profits if value > 0),
            "win_rate_percent": round(
                100 * sum(1 for value in profits if value > 0) / len(model_trades), 4
            )
            if model_trades
            else 0,
            "net_pnl": round(sum(profits), 2),
            "average_pnl": round(sum(profits) / len(profits), 2) if profits else 0,
            "average_realized_r": round(sum(realized_r) / len(realized_r), 4)
            if realized_r
            else None,
            "profit_factor": round(gross_profit / gross_loss, 4)
            if gross_loss
            else None,
            "health_adds": sum(len(trade.get("adds") or []) for trade in model_trades),
            "reductions": sum(
                len(trade.get("reductions") or []) for trade in model_trades
            ),
        }
    return attribution


def close_position(trades, open_pos, bar, exit_price, reason, realized_equity, max_open_loss, open_max_capital):
    pnl = (exit_price - open_pos["avg_price"]) * open_pos["qty"]
    open_pos.update(
        {
            "exit_time": bar["t"].isoformat(),
            "exit_price": exit_price,
            "exit_reason": reason,
            "pnl": pnl,
            "return_percent": (exit_price / open_pos["avg_price"] - 1) * 100,
            "bars_held": bar["index"] - open_pos["entry_index"],
            "max_unrealized_loss": max_open_loss,
            "max_capital": open_max_capital,
        }
    )
    trades.append(open_pos)
    return realized_equity + pnl, None, 0.0, 0.0


def simulate_independent_legacy(symbol, config, bars, market_bars):
    """Historical independent-account model retained only for report comparison."""
    if len(bars) < 80 or len(market_bars) < 80:
        return {"symbol": symbol, "error": "not_enough_bars", "bars": len(bars)}

    trades = []
    open_pos = None
    realized_equity = 0.0
    realized_peak = 0.0
    max_closed_drawdown = 0.0
    max_unrealized_drawdown = 0.0
    max_capital_deployed = 0.0
    max_open_loss = 0.0
    open_max_capital = 0.0
    ladder_fills = 0
    daily_entries = {}
    blocked = {"risk_cap": 0, "market_regime": 0, "max_ladders": 0, "unfilled_limit": 0}

    for index, raw_bar in enumerate(bars):
        bar = {**raw_bar, "index": index}
        if index < 60:
            continue

        if open_pos:
            unrealized = (bar["c"] - open_pos["avg_price"]) * open_pos["qty"]
            max_unrealized_drawdown = min(max_unrealized_drawdown, realized_equity + unrealized - realized_peak)
            max_open_loss = min(max_open_loss, unrealized)
            open_max_capital = max(open_max_capital, open_pos["qty"] * bar["c"])
            max_capital_deployed = max(max_capital_deployed, open_max_capital)

            if bar["l"] <= open_pos["floor_price"]:
                realized_equity, open_pos, max_open_loss, open_max_capital = close_position(
                    trades,
                    open_pos,
                    bar,
                    open_pos["floor_price"],
                    "stop_floor",
                    realized_equity,
                    max_open_loss,
                    open_max_capital,
                )
                realized_peak = max(realized_peak, realized_equity)
                max_closed_drawdown = max(max_closed_drawdown, realized_peak - realized_equity)
                continue

            entry_price = open_pos["initial_entry_price"]
            base_floor = managed_initial_floor_price(
                config, entry_price, {"latest_bar": bar}
            )
            trail_step = float(config.get("trail_trigger_step_percent", 5)) / 100
            rung = max(0, int((bar["c"] / entry_price - 1) / trail_step)) if trail_step > 0 else 0
            open_pos["highest_trail_rung"] = max(open_pos["highest_trail_rung"], rung)
            if open_pos["highest_trail_rung"] > 0:
                trail_pct = trail_below_current_percent(config, open_pos["highest_trail_rung"])
                candidate_floor = bar["c"] * (1 - trail_pct / 100)
            else:
                candidate_floor = base_floor
            open_pos["floor_price"] = max(open_pos["floor_price"], base_floor, candidate_floor)

            market_window = market_bars[max(0, index - 100) : index + 1]
            context = {"latest_bar": bar, "market_ok": market_ok_at(market_window, bar["t"])}
            max_ladder_count, _ = adaptive_ladder_limit(config, context)
            for step in ladder_steps(config, entry_price, context):
                if step["key"] in open_pos["filled_ladder_steps"]:
                    continue
                if len(open_pos["filled_ladder_steps"]) >= max_ladder_count:
                    blocked["max_ladders"] += 1
                    continue
                if bar["c"] > step["trigger_price"]:
                    continue
                if risk_setting(config, "ladder_requires_market_ok", False) and not context["market_ok"]:
                    blocked["market_regime"] += 1
                    continue
                requested = int(config.get("ladder_buy_quantity", 0) or 0)
                requested, _ = adaptive_ladder_quantity(
                    config,
                    requested,
                    open_pos.get("base_qty", open_pos["qty"]),
                    context,
                )
                requested = ladder_cap_qty(config, open_pos, requested, bar["c"])
                qty = cap_qty(config, requested, bar["c"], open_pos["qty"])
                if qty <= 0:
                    blocked["risk_cap"] += 1
                    continue
                open_pos["avg_price"] = (
                    open_pos["avg_price"] * open_pos["qty"] + bar["c"] * qty
                ) / (open_pos["qty"] + qty)
                open_pos["qty"] += qty
                open_pos["ladder_qty"] += qty
                open_pos["ladder_notional"] += qty * bar["c"]
                open_pos["filled_ladder_steps"].append(step["key"])
                open_pos["ladder_fills"].append({"time": bar["t"].isoformat(), "price": bar["c"], "qty": qty, **step})
                ladder_fills += 1
            continue

        day = bar["t"].date().isoformat()
        if daily_entries.get(day, 0) >= int(config.get("reentry_max_per_day", 1) or 1):
            continue
        plan = dynamic_entry_plan(
            symbol,
            bars[max(0, index - 100) : index + 1],
            market_bars[max(0, index - 100) : index + 1],
            exit_trade=None,
            ignore_ledger=True,
            sector_bars=market_bars[max(0, index - 100) : index + 1],
            config=config,
        )
        if plan.get("status") != "active_signal":
            continue
        limit_price = float(plan["limit_price"])
        fill_bar = bar
        fill_index = index
        if not (bar["l"] <= limit_price <= bar["h"]):
            if index + 1 < len(bars) and bars[index + 1]["l"] <= limit_price:
                fill_bar = {**bars[index + 1], "index": index + 1}
                fill_index = index + 1
            else:
                blocked["unfilled_limit"] += 1
                continue
        qty = cap_qty(config, dynamic_qty(config, plan), limit_price, 0)
        if qty <= 0:
            blocked["risk_cap"] += 1
            continue
        open_pos = {
            "entry_time": fill_bar["t"].isoformat(),
            "entry_index": fill_index,
            "entry_mode": plan.get("mode"),
            "initial_entry_price": limit_price,
            "avg_price": limit_price,
            "qty": qty,
            "base_qty": qty,
            "floor_price": initial_floor_price(config, limit_price, {"latest_bar": fill_bar}),
            "highest_trail_rung": 0,
            "ladder_qty": 0,
            "ladder_notional": 0.0,
            "filled_ladder_steps": [],
            "ladder_fills": [],
            "profile": config.get("risk_profile"),
        }
        open_max_capital = qty * limit_price
        max_capital_deployed = max(max_capital_deployed, open_max_capital)
        daily_entries[day] = daily_entries.get(day, 0) + 1

    if open_pos:
        last = {**bars[-1], "index": len(bars) - 1}
        realized_equity, open_pos, max_open_loss, open_max_capital = close_position(
            trades,
            open_pos,
            last,
            last["c"],
            "end_open_mark",
            realized_equity,
            max_open_loss,
            open_max_capital,
        )
        realized_peak = max(realized_peak, realized_equity)

    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_win = sum(trade["pnl"] for trade in wins)
    gross_loss = -sum(trade["pnl"] for trade in losses)
    return {
        "symbol": symbol,
        "profile": config.get("risk_profile"),
        "bars": len(bars),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "total_pnl": round(sum(trade["pnl"] for trade in trades), 2),
        "avg_pnl": round(sum(trade["pnl"] for trade in trades) / len(trades), 2) if trades else 0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown": round(max_closed_drawdown, 2),
        "max_unrealized_drawdown": round(max_unrealized_drawdown, 2),
        "max_capital_deployed": round(max_capital_deployed, 2),
        "ladder_fills": ladder_fills,
        "trades_with_ladder": sum(1 for trade in trades if trade["ladder_fills"]),
        "best_trade": round(max((trade["pnl"] for trade in trades), default=0), 2),
        "worst_trade": round(min((trade["pnl"] for trade in trades), default=0), 2),
        "buy_hold_pct": round((bars[-1]["c"] / bars[0]["c"] - 1) * 100, 2),
        "blocked": blocked,
        "trades_detail": trades,
    }


def portfolio_equity(cash, positions, last_prices):
    return cash + sum(
        position["qty"] * last_prices.get(symbol, position["avg_price"])
        for symbol, position in positions.items()
    )


def historical_health_context(symbol, timestamp, config, health_bars_by_symbol):
    settings = position_health_config(config)
    timeframe = str(settings.get("timeframe", "1Hour")).lower()
    if timeframe not in ("1hour", "60min"):
        raise ValueError(f"portfolio backtest supports 1Hour position health, got {timeframe}")
    completed_at = timestamp + datetime.timedelta(minutes=5) - datetime.timedelta(hours=1)
    symbol_bars = health_bars_by_symbol.get(symbol, [])
    benchmark = str(
        (settings.get("benchmark_by_symbol") or {}).get(symbol)
        or settings.get("benchmark_symbol")
        or "QQQ"
    ).upper()
    benchmark_bars = health_bars_by_symbol.get(benchmark, [])
    symbol_end = bisect.bisect_right([bar["t"] for bar in symbol_bars], completed_at)
    benchmark_end = bisect.bisect_right(
        [bar["t"] for bar in benchmark_bars], completed_at
    )
    if symbol_end < 51 or benchmark_end < 51:
        return None
    stock = symbol_bars[:symbol_end]
    market = benchmark_bars[:benchmark_end]
    latest = dict(stock[-1])
    latest["ema21_slope"] = (
        (latest["ema21"] - stock[-6]["ema21"]) / stock[-6]["ema21"]
        if stock[-6]["ema21"]
        else 0
    )
    relative_lookback = min(len(stock) - 1, len(market) - 1, 30)
    relative_strength = (
        stock[-1]["c"] / stock[-1 - relative_lookback]["c"]
        - market[-1]["c"] / market[-1 - relative_lookback]["c"]
    )
    as_of = latest["t"].isoformat()
    return {
        "as_of": as_of,
        "bar_id": f"{symbol}:1Hour:{as_of}",
        "timeframe_minutes": 60,
        "latest_bar": latest,
        "relative_strength_5d": relative_strength,
        "market_ok": market_ok_at(market, latest["t"]),
        "benchmark_symbol": benchmark,
    }


def portfolio_result_by_symbol(symbol, config, bars, trades, blocked):
    symbol_trades = [trade for trade in trades if trade["symbol"] == symbol]
    wins = [trade for trade in symbol_trades if trade["pnl"] > 0]
    losses = [trade for trade in symbol_trades if trade["pnl"] < 0]
    gross_win = sum(trade["pnl"] for trade in wins)
    gross_loss = -sum(trade["pnl"] for trade in losses)
    realized = 0.0
    realized_peak = 0.0
    max_closed_drawdown = 0.0
    for trade in sorted(symbol_trades, key=lambda item: item["exit_time"]):
        realized += trade["pnl"]
        realized_peak = max(realized_peak, realized)
        max_closed_drawdown = max(max_closed_drawdown, realized_peak - realized)
    blocked_counts = {
        key: int(blocked.get(symbol, {}).get(key, 0))
        for key in (
            "risk_cap",
            "market_regime",
            "max_ladders",
            "unfilled_limit",
            "expired_day_order",
            "shared_cash",
            "reentry_cooldown",
            "replaced_open_order",
            "ladder_observation_only",
        )
    }
    return {
        "symbol": symbol,
        "profile": config.get("risk_profile"),
        "bars": len(bars),
        "trades": len(symbol_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(symbol_trades) * 100, 2)
        if symbol_trades
        else 0,
        "total_pnl": round(sum(trade["pnl"] for trade in symbol_trades), 2),
        "avg_pnl": round(
            sum(trade["pnl"] for trade in symbol_trades) / len(symbol_trades), 2
        )
        if symbol_trades
        else 0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_closed_drawdown": round(max_closed_drawdown, 2),
        "max_unrealized_drawdown": round(
            min((trade["max_unrealized_loss"] for trade in symbol_trades), default=0), 2
        ),
        "max_capital_deployed": round(
            max((trade["max_capital"] for trade in symbol_trades), default=0), 2
        ),
        "ladder_fills": sum(len(trade["ladder_fills"]) for trade in symbol_trades),
        "trades_with_ladder": sum(1 for trade in symbol_trades if trade["ladder_fills"]),
        "best_trade": round(max((trade["pnl"] for trade in symbol_trades), default=0), 2),
        "worst_trade": round(min((trade["pnl"] for trade in symbol_trades), default=0), 2),
        "buy_hold_pct": round((bars[-1]["c"] / bars[0]["c"] - 1) * 100, 2),
        "blocked": blocked_counts,
        "trades_detail": symbol_trades,
    }


def simulate_portfolio(
    configs,
    bars_by_symbol,
    market_bars,
    starting_equity=ACCOUNT_EQUITY,
    health_bars_by_symbol=None,
    regime_bars_by_symbol=None,
    entry_filter_context_at=None,
    entry_window=None,
):
    """Run all symbols on one clock and one cash balance.

    Signals are observed at a bar close. Their day-limit orders cannot fill until
    a later bar for that symbol, eliminating same-bar look-ahead fills.
    """
    eligible = {
        symbol: bars
        for symbol, bars in bars_by_symbol.items()
        if symbol in configs and len(bars) >= 80
    }
    health_bars_by_symbol = health_bars_by_symbol or {}
    regime_bars_by_symbol = {"QQQ": market_bars, **(regime_bars_by_symbol or {})}
    for symbol in eligible:
        settings = position_health_config(configs[symbol])
        if settings.get("enabled", True) and not settings.get("shadow_mode", True):
            benchmark = str(
                (settings.get("benchmark_by_symbol") or {}).get(symbol)
                or settings.get("benchmark_symbol")
                or "QQQ"
            ).upper()
            if symbol not in health_bars_by_symbol or benchmark not in health_bars_by_symbol:
                raise ValueError(
                    f"live-parity backtest requires 1Hour health bars for {symbol} and {benchmark}"
                )
    events = collections.defaultdict(list)
    for symbol, bars in eligible.items():
        for index, bar in enumerate(bars):
            events[bar["t"]].append((symbol, index, bar))

    market_times = [bar["t"] for bar in market_bars]
    regime_times = {
        symbol: [bar["t"] for bar in bars]
        for symbol, bars in regime_bars_by_symbol.items()
    }
    cash = float(starting_equity)
    positions = {}
    pending_orders = {}
    exit_trades = {}
    last_prices = {}
    trades = []
    blocked = collections.defaultdict(collections.Counter)
    daily_entries = collections.Counter()
    equity_peak = float(starting_equity)
    max_drawdown = 0.0
    max_drawdown_percent = 0.0
    exposure_observations = []
    max_gross_exposure = 0.0
    candidate_counts = collections.defaultdict(collections.Counter)

    def market_context(timestamp):
        end = bisect.bisect_right(market_times, timestamp)
        return market_bars[max(0, end - 100) : end]

    def sector_context(symbol, timestamp):
        settings = position_health_config(configs[symbol])
        benchmark = str(
            (settings.get("benchmark_by_symbol") or {}).get(symbol)
            or settings.get("benchmark_symbol")
            or "QQQ"
        ).upper()
        bars = regime_bars_by_symbol.get(benchmark, market_bars)
        times = regime_times.get(benchmark, market_times)
        end = bisect.bisect_right(times, timestamp)
        return bars[max(0, end - 100) : end]

    def close_portfolio_position(symbol, bar, exit_price, reason):
        nonlocal cash
        position = positions.pop(symbol)
        closing_pnl = (exit_price - position["avg_price"]) * position["qty"]
        pnl = position.get("realized_pnl", 0.0) + closing_pnl
        cash += exit_price * position["qty"]
        position.update(
            {
                "exit_time": bar["t"].isoformat(),
                "exit_price": exit_price,
                "exit_reason": reason,
                "pnl": pnl,
                "return_percent": pnl / position["initial_notional"] * 100
                if position["initial_notional"]
                else 0,
                "bars_held": position["bars_seen"] - 1,
            }
        )
        initial_risk_dollars = (
            float(position.get("initial_risk_per_share") or 0)
            * int(position.get("base_qty") or 0)
        )
        position["realized_r"] = (
            pnl / initial_risk_dollars if initial_risk_dollars > 0 else None
        )
        trades.append(position)
        exit_trades[symbol] = {
            "exit_price": exit_price,
            "exit_time": bar["t"],
            "realized_pl": pnl,
            "exit_qty": position["qty"],
        }

    for timestamp in sorted(events):
        current_events = sorted(events[timestamp], key=lambda item: item[0])
        event_by_symbol = {symbol: (index, bar) for symbol, index, bar in current_events}
        for symbol, _, bar in current_events:
            last_prices[symbol] = bar["c"]

        # Existing positions are managed before new capital is allocated.
        for symbol, index, bar in current_events:
            position = positions.get(symbol)
            if not position or index <= position["entry_index"]:
                continue
            position["bars_seen"] += 1
            position["max_capital"] = max(
                position["max_capital"], position["qty"] * bar["c"]
            )
            position["max_unrealized_loss"] = min(
                position["max_unrealized_loss"],
                (bar["c"] - position["avg_price"]) * position["qty"],
            )
            if bar["l"] <= position["floor_price"]:
                stop_fill = min(float(position["floor_price"]), float(bar["o"]))
                close_portfolio_position(symbol, bar, stop_fill, "stop_floor")
                continue

            config = configs[symbol]
            reduction = adverse_reduction_details(
                config, position["avg_price"], position["qty"]
            )
            hard_reduction_due = (
                not position.get("adverse_reduction_completed")
                and bar["c"] <= reduction["trigger_price"]
            )

            entry_price = position["initial_entry_price"]
            base_floor = managed_initial_floor_price(
                config, entry_price, {"latest_bar": bar}
            )
            initial_risk = float(
                position["episode"].get("initial_risk_per_share") or 0
            )
            if initial_risk > 0:
                rung = max(0, int((bar["c"] - entry_price) / initial_risk))
            else:
                trail_step = float(config.get("trail_trigger_step_percent", 5)) / 100
                rung = (
                    max(0, int((bar["c"] / entry_price - 1) / trail_step))
                    if trail_step
                    else 0
                )
            position["highest_trail_rung"] = max(position["highest_trail_rung"], rung)
            if position["highest_trail_rung"]:
                trail_pct = trail_below_current_percent(
                    config, position["highest_trail_rung"]
                )
                candidate_floor = max(
                    entry_price,
                    bar["c"] * (1 - trail_pct / 100),
                )
            else:
                candidate_floor = base_floor
            position["floor_price"] = max(
                position["floor_price"], base_floor, candidate_floor
            )

            settings = position_health_config(config)
            if settings.get("enabled", True) and not settings.get("shadow_mode", True):
                health_context = historical_health_context(
                    symbol, timestamp, config, health_bars_by_symbol
                )
                if (
                    health_context
                    and health_context["bar_id"] != position.get("health_last_bar_id")
                ):
                    health = evaluate_position_health(
                        {
                            "symbol": symbol,
                            "qty": position["qty"],
                            "avg_entry_price": position["avg_price"],
                            "current_price": bar["c"],
                            "market_value": position["qty"] * bar["c"],
                        },
                        position["episode"],
                        health_context,
                        {
                            "stop_price": position["floor_price"],
                            "stop_qty": position["qty"],
                        },
                        settings,
                        now=timestamp + datetime.timedelta(minutes=5),
                    )
                    action = health.get("recommended_action")
                    if action in ("reduce", "exit"):
                        if position.get("health_confirmation_action") == action:
                            position["health_confirmation_count"] += 1
                        else:
                            position["health_confirmation_action"] = action
                            position["health_confirmation_count"] = 1
                    else:
                        position["health_confirmation_action"] = action
                        position["health_confirmation_count"] = 0
                    position["health_last_bar_id"] = health_context["bar_id"]
                    position["latest_health"] = health

                    required = int(
                        settings[
                            "exit_confirmation_bars"
                            if action == "exit"
                            else "reduction_confirmation_bars"
                        ]
                    ) if action in ("reduce", "exit") else 0
                    confirmed = (
                        action in ("reduce", "exit")
                        and position["health_confirmation_count"] >= required
                    )
                    if confirmed and action == "exit":
                        close_portfolio_position(symbol, bar, bar["c"], "health_exit")
                        continue
                    if (
                        confirmed
                        and action == "reduce"
                        and not position.get("adverse_reduction_completed")
                        and not hard_reduction_due
                    ):
                        reduction_qty = min(
                            position["qty"],
                            max(
                                1,
                                math.ceil(
                                    position["qty"]
                                    * float(settings["health_reduction_fraction"])
                                ),
                            ),
                        )
                        if reduction_qty >= position["qty"]:
                            close_portfolio_position(
                                symbol, bar, bar["c"], "health_reduction"
                            )
                        else:
                            realized = (
                                bar["c"] - position["avg_price"]
                            ) * reduction_qty
                            cash += bar["c"] * reduction_qty
                            position["qty"] -= reduction_qty
                            position["realized_pnl"] += realized
                            position["adverse_reduction_completed"] = True
                            position["episode"]["adverse_reduction_completed"] = True
                            position["reductions"].append(
                                {
                                    "time": bar["t"].isoformat(),
                                    "price": bar["c"],
                                    "qty": reduction_qty,
                                    "pnl": realized,
                                    "reason": "health_reduce",
                                }
                            )
                        continue

                    if settings.get("additions_enabled", False) and not hard_reduction_due:
                        equity = portfolio_equity(cash, positions, last_prices)
                        add_eligibility = evaluate_add_eligibility(
                            health,
                            {
                                "score": position["episode"]["entry_setup_score"],
                                "status": position["episode"]["entry_setup_status"],
                            },
                            position["episode"],
                            {
                                "current_price": bar["c"],
                                "avg_entry_price": position["avg_price"],
                                "qty": position["qty"],
                                "market_value": position["qty"] * bar["c"],
                            },
                            {
                                "stop_price": position["floor_price"],
                                "stop_qty": position["qty"],
                            },
                            equity,
                            settings,
                        )
                        if add_eligibility["eligible"]:
                            reserve = equity * float(
                                config.get("min_cash_balance_percent", 20)
                            ) / 100
                            add_qty = min(
                                int(add_eligibility["qty"]),
                                math.floor(max(0, cash - reserve) / bar["c"]),
                            )
                            if add_qty > 0:
                                cash -= add_qty * bar["c"]
                                position["avg_price"] = (
                                    position["avg_price"] * position["qty"]
                                    + bar["c"] * add_qty
                                ) / (position["qty"] + add_qty)
                                position["qty"] += add_qty
                                position["episode"]["add_count"] += 1
                                position["adds"].append(
                                    {
                                        "time": bar["t"].isoformat(),
                                        "price": bar["c"],
                                        "qty": add_qty,
                                        "reason": "position_health_add",
                                    }
                                )

            if hard_reduction_due:
                reduction_qty = reduction["qty"]
                if reduction_qty >= position["qty"]:
                    close_portfolio_position(symbol, bar, bar["c"], "hard_reduction")
                else:
                    realized = (bar["c"] - position["avg_price"]) * reduction_qty
                    cash += bar["c"] * reduction_qty
                    position["qty"] -= reduction_qty
                    position["realized_pnl"] += realized
                    position["adverse_reduction_completed"] = True
                    position["episode"]["adverse_reduction_completed"] = True
                    position["reductions"].append(
                        {
                            "time": bar["t"].isoformat(),
                            "price": bar["c"],
                            "qty": reduction_qty,
                            "pnl": realized,
                            "reason": "hard_reduction",
                        }
                    )
                continue

            context = {
                "latest_bar": bar,
                "market_ok": market_ok_at(market_context(timestamp), timestamp),
            }
            for step in ladder_steps(config, entry_price, context):
                if bar["c"] <= step["trigger_price"]:
                    blocked[symbol]["ladder_observation_only"] += 1

        # Orders created by earlier bars compete for shared capital by signal quality.
        executable = []
        for symbol, order in list(pending_orders.items()):
            event = event_by_symbol.get(symbol)
            if not event:
                continue
            index, bar = event
            if bar["t"].date() != order["session_date"]:
                blocked[symbol]["expired_day_order"] += 1
                pending_orders.pop(symbol, None)
                continue
            if index <= order["created_index"]:
                continue
            if bar["l"] <= order["limit_price"]:
                executable.append((order["priority"], symbol, index, bar, order))

        for _, symbol, index, bar, order in sorted(executable, reverse=True):
            if symbol in positions or symbol not in pending_orders:
                continue
            config = configs[symbol]
            limit_price = order["limit_price"]
            fill_price = min(limit_price, float(bar["o"]))
            equity = portfolio_equity(cash, positions, last_prices)
            max_qty = order["desired_qty"]
            max_total_qty = risk_setting(config, "max_total_position_qty")
            if max_total_qty not in (None, ""):
                max_qty = min(max_qty, int(float(max_total_qty)))
            max_notional_pct = risk_setting(config, "max_symbol_notional_percent")
            if max_notional_pct not in (None, "", 0):
                max_qty = min(
                    max_qty,
                    math.floor(equity * float(max_notional_pct) / 100 / fill_price),
                )
            reserve = equity * float(config.get("min_cash_balance_percent", 20)) / 100
            affordable = math.floor(max(0, cash - reserve) / fill_price)
            qty = min(max_qty, affordable)
            if qty <= 0:
                blocked[symbol]["shared_cash"] += 1
                pending_orders.pop(symbol, None)
                continue
            cash -= qty * fill_price
            plan = order["plan"]
            target_price, target_source = initial_entry_target(
                config, {"dynamic_entry_plan": plan}, fill_price
            )
            episode = {
                "symbol": symbol,
                "origin_model_id": plan_model_id(plan),
                "origin_model_version": int(plan.get("model_version") or 0),
                "entry_setup_score": entry_setup_score(plan),
                "entry_setup_status": plan.get("status"),
                "entry_setup_mode": plan.get("mode"),
                "original_target_price": target_price,
                "target_source": target_source,
                "initial_qty": qty,
                "add_count": 0,
                "adverse_reduction_completed": False,
                "entry_plan": plan,
                "initial_stop_price": plan.get("stop_price"),
                "initial_risk_per_share": plan.get("risk_per_share"),
            }
            positions[symbol] = {
                "symbol": symbol,
                "entry_time": bar["t"].isoformat(),
                "signal_time": order["signal_time"].isoformat(),
                "entry_index": index,
                "entry_mode": plan.get("mode"),
                "entry_model_id": plan_model_id(plan),
                "entry_model_version": int(plan.get("model_version") or 0),
                "initial_entry_price": fill_price,
                "avg_price": fill_price,
                "qty": qty,
                "base_qty": qty,
                "initial_notional": qty * fill_price,
                "initial_risk_per_share": plan.get("risk_per_share"),
                "floor_price": managed_initial_floor_price(
                    config, fill_price, {"latest_bar": bar}, plan=plan
                ),
                "highest_trail_rung": 0,
                "ladder_qty": 0,
                "ladder_notional": 0.0,
                "filled_ladder_steps": [],
                "ladder_fills": [],
                "profile": config.get("risk_profile"),
                "bars_seen": 1,
                "max_unrealized_loss": 0.0,
                "max_capital": qty * fill_price,
                "realized_pnl": 0.0,
                "adverse_reduction_completed": False,
                "reductions": [],
                "adds": [],
                "episode": episode,
                "health_last_bar_id": None,
                "health_confirmation_action": None,
                "health_confirmation_count": 0,
            }
            pending_orders.pop(symbol, None)

        # Generate new orders only after all current-bar prices and indicators are known.
        for symbol, index, bar in current_events:
            if index < 60 or symbol in positions:
                continue
            if entry_window and not (entry_window[0] <= timestamp < entry_window[1]):
                continue
            config = configs[symbol]
            exit_trade = exit_trades.get(symbol)
            if exit_trade:
                cooldown = int(config.get("reentry_cooldown_seconds", 600))
                if (bar["t"] - exit_trade["exit_time"]).total_seconds() < cooldown:
                    blocked[symbol]["reentry_cooldown"] += 1
                    continue
            plan_args = {
                "exit_trade": exit_trade,
                "ignore_ledger": not exit_trade,
                "sector_bars": sector_context(symbol, timestamp),
                "config": config,
            }
            if entry_filter_context_at:
                plan_args["filter_context"] = entry_filter_context_at(
                    symbol, timestamp
                )
            plan = dynamic_entry_plan(
                symbol,
                eligible[symbol][max(0, index - 100) : index + 1],
                market_context(timestamp),
                **plan_args,
            )
            for candidate in plan.get("entry_candidates") or []:
                model_id = candidate.get("model_id") or "unknown"
                candidate_counts[model_id]["evaluated"] += 1
                if candidate.get("status") == "active_signal":
                    candidate_counts[model_id]["qualified"] += 1
            if plan.get("status") == "active_signal":
                candidate_counts[plan_model_id(plan)]["selected"] += 1
            if symbol in pending_orders:
                if (
                    config.get("dynamic_replace_open_orders", True)
                    and plan.get("status") == "active_signal"
                    and plan.get("limit_price")
                ):
                    order = pending_orders[symbol]
                    desired_limit = float(plan["limit_price"])
                    min_change = float(
                        config.get("dynamic_replace_min_change_percent", 0.25)
                    ) / 100
                    desired_qty = dynamic_qty(
                        config,
                        plan,
                        equity=portfolio_equity(cash, positions, last_prices),
                    )
                    if (
                        desired_qty != order["desired_qty"]
                        or abs(desired_limit / order["limit_price"] - 1) >= min_change
                    ):
                        order.update(
                            {
                                "signal_time": bar["t"],
                                "created_index": index,
                                "limit_price": desired_limit,
                                "desired_qty": desired_qty,
                                "priority": float(plan.get("volume_ratio") or 0),
                                "plan": plan,
                            }
                        )
                        blocked[symbol]["replaced_open_order"] += 1
                continue

            day_key = (symbol, bar["t"].date().isoformat())
            if daily_entries[day_key] >= int(config.get("reentry_max_per_day", 1) or 1):
                continue
            if plan.get("status") != "active_signal":
                continue
            pending_orders[symbol] = {
                "symbol": symbol,
                "signal_time": bar["t"],
                "session_date": bar["t"].date(),
                "created_index": index,
                "limit_price": float(plan["limit_price"]),
                "desired_qty": dynamic_qty(
                    config,
                    plan,
                    equity=portfolio_equity(cash, positions, last_prices),
                ),
                "priority": float(plan.get("volume_ratio") or 0),
                "plan": plan,
            }
            daily_entries[day_key] += 1

        equity = portfolio_equity(cash, positions, last_prices)
        gross_exposure = sum(
            position["qty"] * last_prices.get(symbol, position["avg_price"])
            for symbol, position in positions.items()
        )
        exposure_observations.append(gross_exposure / equity if equity > 0 else 0)
        max_gross_exposure = max(max_gross_exposure, gross_exposure)
        equity_peak = max(equity_peak, equity)
        max_drawdown = max(max_drawdown, equity_peak - equity)
        if equity_peak:
            max_drawdown_percent = max(
                max_drawdown_percent, (equity_peak - equity) / equity_peak * 100
            )

    for symbol in sorted(list(positions)):
        bar = eligible[symbol][-1]
        close_portfolio_position(symbol, bar, bar["c"], "end_open_mark")

    ending_equity = cash
    results = [
        portfolio_result_by_symbol(
            symbol, configs[symbol], eligible[symbol], trades, blocked
        )
        for symbol in sorted(eligible)
    ]
    return {
        "portfolio": {
            "starting_equity": round(float(starting_equity), 2),
            "ending_equity": round(ending_equity, 2),
            "net_pnl": round(ending_equity - starting_equity, 2),
            "return_percent": round((ending_equity / starting_equity - 1) * 100, 4),
            "max_drawdown": round(max_drawdown, 2),
            "max_drawdown_percent": round(max_drawdown_percent, 4),
            "average_gross_exposure_percent": round(
                100 * sum(exposure_observations) / len(exposure_observations), 4
            )
            if exposure_observations
            else 0,
            "max_gross_exposure": round(max_gross_exposure, 2),
            "trades": len(trades),
            "pending_orders_at_end": len(pending_orders),
            "execution_model": "signal at bar close; day-limit eligible from next symbol bar",
        },
        "model_attribution": model_attribution(trades, candidate_counts),
        "results": results,
        "trades_detail": trades,
    }


def simulate(symbol, config, bars, market_bars, starting_equity=ACCOUNT_EQUITY):
    """Compatibility wrapper using the corrected portfolio execution model."""
    simulation = simulate_portfolio(
        {symbol: config},
        {symbol: bars},
        market_bars,
        starting_equity=starting_equity,
    )
    if not simulation["results"]:
        return {"symbol": symbol, "error": "not_enough_bars", "bars": len(bars)}
    return {**simulation["results"][0], "portfolio": simulation["portfolio"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_env()
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets/v2").rstrip("/")
    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }
    configs = load_strategy_configs("config/watchers.json")
    base_index_config = {
        "risk_profile": "index_etf",
        "entry_quantity": 100,
        "reentry_max_per_day": 1,
        "dynamic_entry_notional": 25000,
        "initial_stop_loss_percent": 20,
        "trail_trigger_step_percent": 5,
        "trail_stop_below_current_percent": 2.5,
        "ladder_buy_quantity": 100,
        "max_symbol_notional_percent": 25,
        "dynamic_timeframe": TIMEFRAME,
        "dynamic_lookback_days": 7,
    }
    configs["SPY"] = {**base_index_config, "symbol": "SPY"}
    configs["QQQ"] = {**base_index_config, "symbol": "QQQ"}

    market_bars = fetch_bars("QQQ", data_url, headers)
    bars_by_symbol = {}
    errors = []
    for index, symbol in enumerate(args.symbols, 1):
        try:
            bars = market_bars if symbol == "QQQ" else fetch_bars(symbol, data_url, headers)
            if symbol not in configs:
                raise KeyError(f"no strategy config for {symbol}")
            bars_by_symbol[symbol] = bars
            print(f"{index}/{len(args.symbols)} {symbol}: bars={len(bars)}")
        except Exception as exc:
            error = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}
            errors.append(error)
            print(f"{index}/{len(args.symbols)} {symbol}: ERROR {error['error']}")

    regime_symbols = set()
    health_symbols = set()
    for symbol in bars_by_symbol:
        settings = position_health_config(configs[symbol])
        benchmark = str(
            (settings.get("benchmark_by_symbol") or {}).get(symbol)
            or settings.get("benchmark_symbol")
            or "QQQ"
        ).upper()
        if configs[symbol].get("dynamic_require_sector_regime", True):
            regime_symbols.add(benchmark)
        if settings.get("enabled", True) and not settings.get("shadow_mode", True):
            health_symbols.add(symbol)
            health_symbols.add(benchmark)
    regime_bars_by_symbol = {"QQQ": market_bars}
    for index, symbol in enumerate(sorted(regime_symbols - {"QQQ"}), 1):
        regime_bars_by_symbol[symbol] = fetch_bars(symbol, data_url, headers)
        print(
            f"REGIME {index}/{max(1, len(regime_symbols - {'QQQ'}))} {symbol}: "
            f"bars={len(regime_bars_by_symbol[symbol])}"
        )
    health_bars_by_symbol = {}
    for index, symbol in enumerate(sorted(health_symbols), 1):
        try:
            health_bars_by_symbol[symbol] = fetch_bars(
                symbol, data_url, headers, timeframe="1Hour"
            )
            print(
                f"HEALTH {index}/{len(health_symbols)} {symbol}: "
                f"bars={len(health_bars_by_symbol[symbol])}"
            )
        except Exception as exc:
            error = {
                "symbol": symbol,
                "dataset": "position_health_1Hour",
                "error": f"{type(exc).__name__}: {exc}",
            }
            errors.append(error)
            print(f"HEALTH {index}/{len(health_symbols)} {symbol}: ERROR {error['error']}")

    simulation = simulate_portfolio(
        configs,
        bars_by_symbol,
        market_bars,
        health_bars_by_symbol=health_bars_by_symbol,
        regime_bars_by_symbol=regime_bars_by_symbol,
    )
    results = simulation["results"] + errors
    print(
        "PORTFOLIO: "
        f"trades={simulation['portfolio']['trades']} "
        f"pnl={simulation['portfolio']['net_pnl']} "
        f"return={simulation['portfolio']['return_percent']}%"
    )

    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "start": iso_utc(START),
        "end": iso_utc(END),
        "timeframe": TIMEFRAME,
        "account_equity_assumption": ACCOUNT_EQUITY,
        "model": "unified cash portfolio; signals at bar close; next-bar-or-later day-limit fills; managed stop floor; no commissions/slippage; final open positions marked at last close",
        "portfolio": simulation["portfolio"],
        "live_parity": {
            "shared_dynamic_entry_plan": True,
            "separate_entry_models": True,
            "deterministic_entry_arbiter": True,
            "shared_initial_and_catastrophic_floor": True,
            "adverse_reduction": True,
            "ledger_aware_reentry": True,
            "reentry_cooldown": True,
            "position_health_actions": True,
            "position_health_additions": True,
            "direct_ladder_authority": False,
            "shared_cash_and_symbol_caps": True,
            "known_execution_approximation": "market orders fill at the current completed bar close; stop gaps fill at the worse of stop or bar open",
        },
        "results": results,
        "model_attribution": simulation["model_attribution"],
        "errors": errors,
        "trades_detail": simulation["trades_detail"],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"WROTE {output}")


if __name__ == "__main__":
    raise SystemExit(main())
