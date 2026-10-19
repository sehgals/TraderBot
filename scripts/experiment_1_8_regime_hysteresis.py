"""Experiment 1.8: Market-Regime Hysteresis Evaluation.

Evaluates regime flip frequency and compares 1-bar, 2-bar, and 3-bar confirmation hysteresis
across all 7 walk-forward windows on 12 symbols with Fixed 3.5% stops.

Outputs metrics to runtime/reports/experiments/experiment_1_8_regime_hysteresis.json.
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
    parse_time,
    prepare_filter_configs,
    slice_bars,
    walk_forward_windows,
)
from scripts.run_risk_control_backtest import simulate_portfolio
from traderbot.backtester.reentry_backtest import load_strategy_configs

CACHE_DIR = ROOT / "runtime" / "cache" / "bars"
SYMBOLS_DEFAULT = ["MSFT", "NFLX", "ORCL", "GOOG", "IBM", "SMCI", "PLTR", "MU", "ANET", "MRVL", "AMKR", "WDC"]
START_DEFAULT = "2026-01-15T00:00:00Z"
END_DEFAULT = "2026-10-16T00:00:00Z"


def load_cached_bars(symbols, start_dt, end_dt):
    cache_path = CACHE_DIR / f"bars_{len(symbols)}_syms_{start_dt.date()}_{end_dt.date()}.pkl"
    if not cache_path.exists():
        raise FileNotFoundError(f"Bar cache not found at {cache_path}")
    with open(cache_path, "rb") as f:
        return pickle.load(f)


def calculate_regime_flicker(market_bars):
    """Analyze flip frequency of the 1-bar market_ok condition."""
    states = []
    for i in range(6, len(market_bars)):
        bar = market_bars[i]
        prev = market_bars[i - 5]
        slope = (bar["ema21"] - prev["ema21"]) / prev["ema21"] if prev.get("ema21") else 0
        ok = bool(bar.get("c", 0) > bar.get("vwap", 0) and bar.get("c", 0) > bar.get("ema21", 0) and slope >= 0)
        states.append(ok)

    flips = 0
    one_bar_whipsaws = 0
    for i in range(1, len(states)):
        if states[i] != states[i - 1]:
            flips += 1
        if i >= 2 and states[i] == states[i - 2] and states[i] != states[i - 1]:
            one_bar_whipsaws += 1

    return {
        "total_bars_evaluated": len(states),
        "total_regime_flips": flips,
        "one_bar_whipsaws": one_bar_whipsaws,
        "whipsaw_pct_of_flips": round(one_bar_whipsaws / flips * 100.0, 2) if flips else 0.0,
    }


def wrap_market_ok_hysteresis(hysteresis_bars):
    """Factory creating a market_ok_at function that requires N consecutive bars of market_ok."""
    def custom_market_ok_at(market_bars, timestamp):
        prior = [bar for bar in market_bars if bar["t"] <= timestamp]
        if len(prior) < 6 + hysteresis_bars:
            return True
        for offset in range(hysteresis_bars):
            idx = -1 - offset
            bar = prior[idx]
            prev = prior[idx - 5]
            slope = (bar["ema21"] - prev["ema21"]) / prev["ema21"] if prev.get("ema21") else 0
            ok = bar["c"] > bar["vwap"] and bar["c"] > bar["ema21"] and slope >= 0
            if not ok:
                return False
        return True
    return custom_market_ok_at


def run_hysteresis_evaluation(base_configs, windows, cached_data, hysteresis_bars, context):
    import traderbot.core_strategy_engine.entry_models.features as feat_mod
    import traderbot.core_strategy_engine.engine as eng_mod
    import traderbot.core_strategy_engine.dynamic_entry as dyn_mod

    original_fn = feat_mod.market_ok_at
    custom_fn = wrap_market_ok_hysteresis(hysteresis_bars)

    # Monkey-patch market_ok_at during simulation
    feat_mod.market_ok_at = custom_fn
    eng_mod.market_ok_at = custom_fn
    dyn_mod.market_ok_at = custom_fn

    try:
        market_bars = cached_data["market_bars"]
        bars_by_symbol = cached_data["bars_by_symbol"]
        health_bars = cached_data["health_bars"]
        regime_bars = {"QQQ": market_bars}

        configs = copy.deepcopy(base_configs)
        for sym, cfg in configs.items():
            cfg["initial_stop_loss_percent"] = 3.5
            cfg["structural_stop_min_percent"] = 3.5
            cfg["structural_stop_max_percent"] = 3.5
            cfg.setdefault("position_health", {})["entry_gate_mode"] = "off"
            cfg["dynamic_require_market_regime"] = True

        all_trades = []
        window_pnls = []

        for w in windows:
            sliced_symbols = {s: slice_bars(bars, w["test_start"], w["test_end"]) for s, bars in bars_by_symbol.items()}
            sliced_market = slice_bars(market_bars, w["test_start"], w["test_end"])
            sliced_regime = {"QQQ": sliced_market}
            sliced_health = {s: slice_bars(bars, w["test_start"], w["test_end"]) for s, bars in health_bars.items()}

            sim = simulate_portfolio(
                configs,
                sliced_symbols,
                sliced_market,
                starting_equity=100000.0,
                health_bars_by_symbol=sliced_health,
                regime_bars_by_symbol=sliced_regime,
                entry_filter_context_at=context.at,
                entry_window=(w["test_start"], w["test_end"]),
                estimated_slippage_bps=5.0,
            )
            pnl = sim["portfolio"]["net_pnl"]
            window_pnls.append(pnl)
            all_trades.extend(sim.get("trades_detail") or [])

        winners = [t for t in all_trades if float(t.get("pnl") or 0) > 0]
        losers = [t for t in all_trades if float(t.get("pnl") or 0) < 0]
        gross_win = sum(float(t["pnl"]) for t in winners)
        gross_loss = abs(sum(float(t["pnl"]) for t in losers))
        net_pnl = gross_win - gross_loss
        total = len(all_trades)
        wr = (len(winners) / total * 100.0) if total else 0.0
        pf = (gross_win / gross_loss) if gross_loss > 0 else 0.0
        win_windows = sum(1 for p in window_pnls if p > 0)

        return {
            "hysteresis_bars": hysteresis_bars,
            "label": f"{hysteresis_bars}-bar confirmation",
            "total_trades": total,
            "win_rate_pct": round(wr, 2),
            "profit_factor": round(pf, 3),
            "net_pnl": round(net_pnl, 2),
            "profitable_windows": f"{win_windows}/{len(windows)}",
            "window_pnls": [round(p, 2) for p in window_pnls],
        }
    finally:
        feat_mod.market_ok_at = original_fn
        eng_mod.market_ok_at = original_fn
        dyn_mod.market_ok_at = original_fn


def main():
    parser = argparse.ArgumentParser(description="Experiment 1.8: Market-Regime Hysteresis")
    parser.add_argument("--start", default=START_DEFAULT)
    parser.add_argument("--end", default=END_DEFAULT)
    parser.add_argument("--output", default="runtime/reports/experiments/experiment_1_8_regime_hysteresis.json")
    args = parser.parse_args()

    start_dt = parse_time(args.start)
    end_dt = parse_time(args.end)
    windows = walk_forward_windows(start_dt, end_dt, train_days=90, test_days=30, purge_days=1)
    cached_data = load_cached_bars(SYMBOLS_DEFAULT, start_dt, end_dt)

    flicker = calculate_regime_flicker(cached_data["market_bars"])
    print("\n" + "=" * 80)
    print("REGIME FLICKER ANALYSIS (QQQ 5-min bars)")
    print("=" * 80)
    print(f"Total Bars Evaluated: {flicker['total_bars_evaluated']}")
    print(f"Total Regime Flips: {flicker['total_regime_flips']}")
    print(f"1-Bar Whipsaws (Flip and immediately reverse): {flicker['one_bar_whipsaws']} ({flicker['whipsaw_pct_of_flips']}%)")
    print("=" * 80)

    base_configs = load_strategy_configs("config/watchers.json")
    base_configs = {s: base_configs[s] for s in SYMBOLS_DEFAULT if s in base_configs}
    base_configs, _ = prepare_filter_configs(base_configs, has_quotes=False, has_calendar=False, liquidity_scale=1.0)
    context = HistoricalFilterContext(None, None)

    results = []
    print("\n" + "=" * 80)
    print("HYSTERESIS WALK-FORWARD EVALUATION (Fixed 3.5% Stop)")
    print("=" * 80)
    print(f"{'Hysteresis':<16} | {'Trades':<8} | {'WR %':<8} | {'PF':<8} | {'Net P/L':<12} | {'Win Windows':<10}")
    print("-" * 80)

    for h in (1, 2, 3):
        res = run_hysteresis_evaluation(base_configs, windows, cached_data, h, context)
        results.append(res)
        print(f"{res['label']:<16} | {res['total_trades']:<8} | {res['win_rate_pct']:<8} | {res['profit_factor']:<8} | ${res['net_pnl']:<11.2f} | {res['profitable_windows']:<10}")

    print("=" * 80)

    out_path = ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(
            {
                "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "flicker_analysis": flicker,
                "hysteresis_results": results,
            },
            f,
            indent=2,
        )
    print(f"\nWrote regime hysteresis report to {out_path}")


if __name__ == "__main__":
    main()
