"""Experiment 1.3: Hourly Swing Variant vs Baseline.

Evaluates out-of-sample walk-forward performance across:
1. Baseline (confirmation_only: entry_gate off, standard 5-min stops)
2. Hourly Gate (position health hourly gate enforced, standard stops)
3. Hourly Swing (hourly structure + 2.5x ATR stops, standard trail)
4. Hourly Swing Slow Trail (hourly structure stops + slow trail at 2.0R)
5. Hourly Swing Calibrated (hourly structure + 1.5x ATR stops + slow trail)

Runs offline across all 7 walk-forward windows using cached 5Min & 1Hour bars.
Outputs comparative metrics and writes results to runtime/reports/experiments/experiment_1_3_hourly_swing.json.
"""
import argparse
import copy
import datetime
import json
import math
import os
import pickle
import sys
from collections import Counter
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
    simulate_portfolio,
)
from traderbot.backtester.reentry_backtest import load_strategy_configs
from traderbot.backtester.swing_research import research_configs, lifecycle_metrics

CACHE_DIR = ROOT / "runtime" / "cache" / "bars"
SYMBOLS_DEFAULT = ["MSFT", "NFLX", "ORCL", "GOOG", "IBM", "SMCI", "PLTR", "MU", "ANET", "MRVL", "AMKR", "WDC"]
START_DEFAULT = "2026-01-15T00:00:00Z"
END_DEFAULT = "2026-10-16T00:00:00Z"


def load_cached_bars(symbols, start_dt, end_dt):
    cache_path = CACHE_DIR / f"bars_{len(symbols)}_syms_{start_dt.date()}_{end_dt.date()}.pkl"
    if not cache_path.exists():
        raise FileNotFoundError(f"Bar cache not found at {cache_path}. Run experiment_1_2_stop_sweep.py first.")
    print(f"Loading bars from cache: {cache_path}")
    with open(cache_path, "rb") as f:
        return pickle.load(f)


def calculate_max_drawdown(trades, starting_equity=100000.0):
    sorted_trades = sorted(trades, key=lambda t: t.get("exit_time", ""))
    equity = starting_equity
    peak = starting_equity
    max_dd_dollars = 0.0
    max_dd_pct = 0.0

    for t in sorted_trades:
        pnl = float(t.get("pnl") or 0.0)
        equity += pnl
        if equity > peak:
            peak = equity
        dd_dollars = peak - equity
        dd_pct = (dd_dollars / peak * 100.0) if peak > 0 else 0.0
        if dd_dollars > max_dd_dollars:
            max_dd_dollars = dd_dollars
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    return round(max_dd_dollars, 2), round(max_dd_pct, 2)


def run_variant_evaluation(configs, windows, cached_data, variant_spec, context):
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

    winners = [t for t in test_trades if float(t.get("pnl") or 0) > 0]
    losers = [t for t in test_trades if float(t.get("pnl") or 0) < 0]
    gross_win = sum(float(t["pnl"]) for t in winners)
    gross_loss = abs(sum(float(t["pnl"]) for t in losers))
    net_pnl = gross_win - gross_loss

    total_trades = len(test_trades)
    win_rate = (len(winners) / total_trades * 100.0) if total_trades else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    avg_win = (gross_win / len(winners)) if winners else 0.0
    avg_loss = (gross_loss / len(losers)) if losers else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else 0.0

    max_dd_dollars, max_dd_pct = calculate_max_drawdown(test_trades, 100000.0)
    profitable_windows = sum(1 for p in window_pnls if p > 0)
    lifecycle = lifecycle_metrics(test_trades)

    return {
        "variant": variant_spec["name"],
        "description": variant_spec["description"],
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(pf, 3),
        "payoff_ratio": round(payoff, 2),
        "net_pnl": round(net_pnl, 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown_dollars": max_dd_dollars,
        "max_drawdown_pct": max_dd_pct,
        "profitable_windows": f"{profitable_windows}/{len(windows)}",
        "window_pnls": [round(p, 2) for p in window_pnls],
        "average_holding_hours": round(lifecycle.get("average_holding_hours") or 0.0, 1),
        "exit_reasons": lifecycle.get("exit_reasons", {}),
    }


def main():
    parser = argparse.ArgumentParser(description="Experiment 1.3: Hourly Swing Variants")
    parser.add_argument("--start", default=START_DEFAULT)
    parser.add_argument("--end", default=END_DEFAULT)
    parser.add_argument("--output", default="runtime/reports/experiments/experiment_1_3_hourly_swing.json")
    args = parser.parse_args()

    start_dt = parse_time(args.start)
    end_dt = parse_time(args.end)
    windows = walk_forward_windows(start_dt, end_dt, train_days=90, test_days=30, purge_days=1)
    print(f"Loaded {len(windows)} walk-forward windows.")

    cached_data = load_cached_bars(SYMBOLS_DEFAULT, start_dt, end_dt)

    base_configs = load_strategy_configs("config/watchers.json")
    base_configs = {s: base_configs[s] for s in SYMBOLS_DEFAULT if s in base_configs}
    base_configs, _ = prepare_filter_configs(base_configs, has_quotes=False, has_calendar=False, liquidity_scale=1.0)
    context = HistoricalFilterContext(None, None)

    variants_to_test = [
        {
            "name": "baseline_confirmation_only",
            "description": "Baseline: Entry gate off, 5-min ATR stop (1.5x), standard trailing",
            "configs": research_configs(base_configs, "confirmation_only"),
        },
        {
            "name": "hourly_gate_enforced",
            "description": "Hourly Gate: 1Hour Position Health entry gate enforced, 5-min ATR stop",
            "configs": research_configs(base_configs, "hourly_gate"),
        },
        {
            "name": "hourly_swing_standard_trail",
            "description": "Hourly Swing: 1Hour structure + 2.5x ATR stops, standard trailing",
            "configs": research_configs(base_configs, "hourly_swing"),
        },
        {
            "name": "hourly_swing_slow_trail",
            "description": "Hourly Swing Slow Trail: 1Hour structure + 2.5x ATR stops, trailing activates at 2.0R",
            "configs": research_configs(base_configs, "hourly_swing_slow_trail"),
        },
    ]

    # Add a calibrated 1.5x hourly swing variant
    calibrated_configs = copy.deepcopy(base_configs)
    for cfg in calibrated_configs.values():
        cfg.setdefault("position_health", {})["entry_gate_mode"] = "enforce"
        cfg["research_swing_stops"] = {
            "atr_multiple": 1.5,
            "structure_buffer_atr": 0.1,
            "slow_trail": True,
            "trail_activation_r": 1.5,
        }
    variants_to_test.append({
        "name": "hourly_swing_calibrated_1.5x",
        "description": "Hourly Swing Calibrated: 1Hour structure + 1.5x ATR stops, trail activates at 1.5R",
        "configs": calibrated_configs,
    })

    results = []
    print("\n" + "=" * 90)
    print(f"{'Variant':<32} | {'Trades':<6} | {'WR %':<6} | {'PF':<6} | {'Payoff':<6} | {'Net P/L':<11} | {'Win Windows':<10}")
    print("=" * 90)

    for spec in variants_to_test:
        res = run_variant_evaluation(spec["configs"], windows, cached_data, spec, context)
        results.append(res)
        print(
            f"{res['variant']:<32} | "
            f"{res['total_trades']:<6} | "
            f"{res['win_rate_pct']:<6} | "
            f"{res['profit_factor']:<6} | "
            f"{res['payoff_ratio']:<6} | "
            f"${res['net_pnl']:<10.2f} | "
            f"{res['profitable_windows']:<10}"
        )

    print("=" * 90)

    # Save output
    out_path = ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(
            {
                "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "windows_count": len(windows),
                "symbols": SYMBOLS_DEFAULT,
                "variants": results,
            },
            f,
            indent=2,
        )
    print(f"\nWrote results to {out_path}")


if __name__ == "__main__":
    main()
