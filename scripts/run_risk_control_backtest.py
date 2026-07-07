import argparse
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
    calculate_indicators,
    dynamic_entry_plan,
    initial_floor_price,
    iso_utc,
    ladder_steps,
    load_env,
    market_ok_at,
    risk_setting,
    trail_below_current_percent,
)


TIMEFRAME = "5Min"
ACCOUNT_EQUITY = 100000.0
START = datetime.datetime(2025, 7, 7, 20, 0, tzinfo=datetime.timezone.utc)
END = datetime.datetime(2026, 7, 7, 20, 0, tzinfo=datetime.timezone.utc)


def fetch_bars(symbol, data_url, headers):
    bars = []
    page_token = None
    while True:
        params = {
            "symbols": symbol,
            "timeframe": TIMEFRAME,
            "start": iso_utc(START),
            "end": iso_utc(END),
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


def dynamic_qty(config, plan):
    if config.get("dynamic_entry_notional"):
        return max(1, math.floor(float(config["dynamic_entry_notional"]) / float(plan["limit_price"])))
    return int(config.get("entry_quantity", 1))


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


def simulate(symbol, config, bars, market_bars):
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
            base_floor = initial_floor_price(config, entry_price, {"latest_bar": bar})
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
    results = []
    errors = []
    for index, symbol in enumerate(args.symbols, 1):
        try:
            bars = market_bars if symbol == "QQQ" else fetch_bars(symbol, data_url, headers)
            result = simulate(symbol, configs[symbol], bars, market_bars)
            results.append(result)
            if result.get("error"):
                errors.append(result)
            print(f"{index}/{len(args.symbols)} {symbol}: trades={result.get('trades')} pnl={result.get('total_pnl')}")
        except Exception as exc:
            error = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}
            errors.append(error)
            results.append(error)
            print(f"{index}/{len(args.symbols)} {symbol}: ERROR {error['error']}")

    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "start": iso_utc(START),
        "end": iso_utc(END),
        "timeframe": TIMEFRAME,
        "account_equity_assumption": ACCOUNT_EQUITY,
        "model": "dynamic_entry_plan + managed stop floor + new risk controls; no commissions/slippage; final open positions marked at last close",
        "results": results,
        "errors": errors,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"WROTE {output}")


if __name__ == "__main__":
    raise SystemExit(main())
