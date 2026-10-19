"""Experiment 1.2: Stop-Width Sweep.

Evaluates out-of-sample walk-forward performance across varying stop designs:
1. ATR multiples: 1.5x, 2.0x, 2.5x, 3.0x, 3.5x ATR
2. Fixed percentage floors: 2.0%, 3.0%, 4.0%, 5.0%, 6.0%

Uses cached 5Min/1Hour bar data to run offline in-memory simulations across all 7 walk-forward windows.
Outputs comparative metrics: Profit Factor, Win Rate, Payoff, Net PnL, Max Drawdown, and Trade Count.
"""
import argparse
import copy
import datetime
import json
import math
import os
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_walk_forward_backtest import (
    HistoricalFilterContext,
    fetch_bars,
    load_optional,
    parse_time,
    prepare_filter_configs,
    slice_bars,
    walk_forward_windows,
)
from scripts.run_risk_control_backtest import (
    load_env,
    position_health_config,
    simulate_portfolio,
)
from traderbot.backtester.reentry_backtest import (
    isolate_entry_model_configs,
    load_strategy_configs,
)

CACHE_DIR = ROOT / "runtime" / "cache" / "bars"
SYMBOLS_DEFAULT = ["MSFT", "NFLX", "ORCL", "GOOG", "IBM", "SMCI", "PLTR", "MU", "ANET", "MRVL", "AMKR", "WDC"]
START_DEFAULT = "2026-01-15T00:00:00Z"
END_DEFAULT = "2026-10-16T00:00:00Z"


def load_or_fetch_all_bars(symbols, start_dt, end_dt, data_url, headers):
    """Load bars from disk cache if present; otherwise fetch from Alpaca and cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = f"bars_{len(symbols)}_syms_{start_dt.date()}_{end_dt.date()}.pkl"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        print(f"Loading bars from cache: {cache_path}")
        with open(cache_path, "rb") as f:
            data = pickle.load(f)
        health_bars = data.get("health_bars", {})
        updated = False
        for sym in ("SPY", "QQQ", "XLC", "XLK", "SOXX", "XLI"):
            if sym not in health_bars:
                print(f"Fetching benchmark 1Hour health bars for {sym}...")
                health_bars[sym] = fetch_bars(sym, data_url, headers, timeframe="1Hour", start=start_dt, end=end_dt)
                updated = True
        if updated:
            data["health_bars"] = health_bars
            with open(cache_path, "wb") as f:
                pickle.dump(data, f)
        return data

    print(f"Fetching bars for {len(symbols)} symbols from Alpaca...")
    market_bars = fetch_bars("QQQ", data_url, headers, start=start_dt, end=end_dt)
    bars_by_symbol = {}
    for idx, sym in enumerate(symbols, 1):
        print(f"  [{idx}/{len(symbols)}] {sym}")
        bars_by_symbol[sym] = (
            market_bars if sym == "QQQ" else fetch_bars(sym, data_url, headers, start=start_dt, end=end_dt)
        )

    # Health bars (1Hour) for all symbols + benchmarks
    health_symbols = list(symbols) + ["SPY", "QQQ", "XLC", "XLK", "SOXX", "XLI"]
    health_bars = {}
    for sym in health_symbols:
        try:
            health_bars[sym] = fetch_bars(sym, data_url, headers, timeframe="1Hour", start=start_dt, end=end_dt)
        except Exception:
            pass

    cached_data = {
        "market_bars": market_bars,
        "bars_by_symbol": bars_by_symbol,
        "health_bars": health_bars,
    }
    with open(cache_path, "wb") as f:
        pickle.dump(cached_data, f)
    print(f"Saved bars to cache: {cache_path}")
    return cached_data


def apply_stop_override(configs, stop_setting):
    """Deep-copy configs and override stop rules according to test specification."""
    c = copy.deepcopy(configs)
    mode = stop_setting["mode"]

    for sym, cfg in c.items():
        if mode == "atr":
            atr_mult = stop_setting["atr_multiple"]
            cfg["structural_stop_atr_multiple"] = atr_mult
            cfg["structural_stop_buffer_atr"] = 0.1
            # Unclamp artificial 6% ceiling so ATR stop has room to breathe
            cfg["structural_stop_max_percent"] = max(10.0, atr_mult * 2.5)
            cfg["initial_stop_loss_percent"] = max(10.0, atr_mult * 2.5)
        elif mode == "fixed_pct":
            pct = stop_setting["pct"]
            cfg["initial_stop_loss_percent"] = pct
            cfg["structural_stop_min_percent"] = pct
            cfg["structural_stop_max_percent"] = pct
    return c


def run_sweep_evaluation(configs, windows, cached_data, stop_setting, context):
    """Run full walk-forward across all test windows for one stop setting."""
    market_bars = cached_data["market_bars"]
    bars_by_symbol = cached_data["bars_by_symbol"]
    health_bars = cached_data["health_bars"]
    regime_bars = {"QQQ": market_bars}

    test_trades = []
    window_pnls = []

    for w_idx, window in enumerate(windows, 1):
        sliced_symbols = {
            s: slice_bars(bars, window["test_start"], window["test_end"])
            for s, bars in bars_by_symbol.items()
        }
        sliced_market = slice_bars(market_bars, window["test_start"], window["test_end"])
        sliced_regime = {"QQQ": sliced_market}
        sliced_health = {
            s: slice_bars(bars, window["test_start"], window["test_end"])
            for s, bars in health_bars.items()
        }

        sim = simulate_portfolio(
            configs,
            sliced_symbols,
            sliced_market,
            starting_equity=100000.0,
            health_bars_by_symbol=sliced_health,
            regime_bars_by_symbol=sliced_regime,
            entry_filter_context_at=context.at,
            entry_window=(window["test_start"], window["test_end"]),
            estimated_slippage_bps=5.0,
        )
        pnl = sim["portfolio"]["net_pnl"]
        window_pnls.append(pnl)
        test_trades.extend(sim.get("trades_detail") or [])

    # Metrics aggregation
    winners = [t for t in test_trades if float(t.get("pnl") or 0) > 0]
    losers = [t for t in test_trades if float(t.get("pnl") or 0) < 0]
    gross_win = sum(float(t["pnl"]) for t in winners)
    gross_loss = abs(sum(float(t["pnl"]) for t in losers))
    net_pnl = gross_win - gross_loss

    total_trades = len(test_trades)
    win_rate = (len(winners) / total_trades * 100.0) if total_trades else 0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    avg_win = (gross_win / len(winners)) if winners else 0
    avg_loss = (gross_loss / len(losers)) if losers else 0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else 0

    profitable_windows = sum(1 for p in window_pnls if p > 0)

    return {
        "setting_label": stop_setting["label"],
        "mode": stop_setting["mode"],
        "parameter": stop_setting.get("atr_multiple") or stop_setting.get("pct"),
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(pf, 3),
        "payoff_ratio": round(payoff, 2),
        "net_pnl": round(net_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profitable_windows": f"{profitable_windows}/{len(windows)}",
        "window_pnls": [round(p, 2) for p in window_pnls],
    }


def main():
    parser = argparse.ArgumentParser(description="Experiment 1.2: Stop-Width Sweep")
    parser.add_argument("--start", default=START_DEFAULT)
    parser.add_argument("--end", default=END_DEFAULT)
    parser.add_argument(
        "--output",
        default="runtime/reports/experiments/experiment_1_2_stop_sweep.json",
    )
    args = parser.parse_args()

    start_dt = parse_time(args.start)
    end_dt = parse_time(args.end)
    windows = walk_forward_windows(start_dt, end_dt, train_days=90, test_days=30, purge_days=1)

    load_env()
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets/v2").rstrip("/")
    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }

    cached_data = load_or_fetch_all_bars(SYMBOLS_DEFAULT, start_dt, end_dt, data_url, headers)

    base_configs = load_strategy_configs("config/watchers.json")
    base_configs = {s: base_configs[s] for s in SYMBOLS_DEFAULT if s in base_configs}
    base_configs, _ = prepare_filter_configs(base_configs, has_quotes=False, has_calendar=False, liquidity_scale=1.0)
    context = HistoricalFilterContext(None, None)

    sweep_grid = [
        # Baseline
        {"label": "ATR 1.5x (Baseline)", "mode": "atr", "atr_multiple": 1.5},
        # ATR Sweep
        {"label": "ATR 2.0x", "mode": "atr", "atr_multiple": 2.0},
        {"label": "ATR 2.5x", "mode": "atr", "atr_multiple": 2.5},
        {"label": "ATR 3.0x", "mode": "atr", "atr_multiple": 3.0},
        {"label": "ATR 3.5x", "mode": "atr", "atr_multiple": 3.5},
        # Fixed Percentage Sweep
        {"label": "Fixed 2.0%", "mode": "fixed_pct", "pct": 2.0},
        {"label": "Fixed 3.0%", "mode": "fixed_pct", "pct": 3.0},
        {"label": "Fixed 4.0%", "mode": "fixed_pct", "pct": 4.0},
        {"label": "Fixed 5.0%", "mode": "fixed_pct", "pct": 5.0},
        {"label": "Fixed 6.0%", "mode": "fixed_pct", "pct": 6.0},
    ]

    print(f"\nRunning stop-width sweep over {len(sweep_grid)} configurations across {len(windows)} walk-forward windows...")
    results = []
    for setting in sweep_grid:
        configs = apply_stop_override(base_configs, setting)
        res = run_sweep_evaluation(configs, windows, cached_data, setting, context)
        results.append(res)
        print(f"  {res['setting_label']:<20} | Trades: {res['total_trades']:<4} | WR: {res['win_rate_pct']:>5}% | PF: {res['profit_factor']:>5} | Payoff: {res['payoff_ratio']:>4} | PnL: ${res['net_pnl']:>8} | WinWindows: {res['profitable_windows']}")

    out_path = ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"experiment": "1.2 Stop-Width Sweep", "results": results}, indent=2), encoding="utf-8")
    print(f"\nWrote full sweep results to {out_path}")


if __name__ == "__main__":
    main()
