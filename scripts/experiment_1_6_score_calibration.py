"""Experiment 1.6: Score Calibration & Threshold Optimization.

Evaluates the monotonicity of setup_score vs forward trade returns and sweeps minimum_setup_score:
1. Bucket-wise return, win rate, and profit factor analysis across setup_score deciles (80-84, 85-89, 90-94, 95-100).
2. Spearman rank correlation between score and realized return / R-multiple.
3. Threshold sweep: Test minimum setup score thresholds (70, 75, 80, 85) across all walk-forward windows.

Outputs results to runtime/reports/experiments/experiment_1_6_score_calibration.json.
"""
import argparse
import copy
import datetime
import json
import math
import os
import pickle
import sys
from collections import defaultdict
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


def calculate_spearman(x, y):
    if len(x) < 3 or len(y) < 3:
        return 0.0
    n = len(x)
    def rank(arr):
        sorted_indices = sorted(range(n), key=lambda i: arr[i])
        ranks = [0.0] * n
        for r, idx in enumerate(sorted_indices):
            ranks[idx] = r + 1
        return ranks
    rx = rank(x)
    ry = rank(y)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    num = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    den_x = math.sqrt(sum((rx[i] - mean_rx) ** 2 for i in range(n)))
    den_y = math.sqrt(sum((ry[i] - mean_ry) ** 2 for i in range(n)))
    if den_x * den_y == 0:
        return 0.0
    return round(num / (den_x * den_y), 4)


def run_threshold_sweep(base_configs, windows, cached_data, thresholds, context):
    market_bars = cached_data["market_bars"]
    bars_by_symbol = cached_data["bars_by_symbol"]
    health_bars = cached_data["health_bars"]
    regime_bars = {"QQQ": market_bars}

    results = []

    for threshold in thresholds:
        configs = copy.deepcopy(base_configs)
        for sym, cfg in configs.items():
            cfg["initial_stop_loss_percent"] = 3.5
            cfg["structural_stop_min_percent"] = 3.5
            cfg["structural_stop_max_percent"] = 3.5
            cfg.setdefault("position_health", {})["entry_gate_mode"] = "off"
            # Set minimum setup score
            cfg["minimum_setup_score"] = threshold
            cfg["entry_model_min_score"] = threshold

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

        results.append({
            "threshold": threshold,
            "total_trades": total,
            "win_rate_pct": round(wr, 2),
            "profit_factor": round(pf, 3),
            "net_pnl": round(net_pnl, 2),
            "profitable_windows": f"{win_windows}/{len(windows)}",
            "trades": all_trades,
        })

    return results


def analyze_score_deciles(trades):
    buckets = defaultdict(list)
    scores = []
    returns = []
    r_multiples = []

    for t in trades:
        score = float(t.get("episode", {}).get("entry_setup_score") or 0.0)
        ret = float(t.get("return_percent") or 0.0)
        realized_r = t.get("realized_r")
        pnl = float(t.get("pnl") or 0.0)

        scores.append(score)
        returns.append(ret)
        if realized_r is not None:
            r_multiples.append(float(realized_r))

        if score < 80:
            bucket_key = "<80"
        elif score < 85:
            bucket_key = "80-84"
        elif score < 90:
            bucket_key = "85-89"
        elif score < 95:
            bucket_key = "90-94"
        else:
            bucket_key = "95-100"

        buckets[bucket_key].append({"score": score, "return": ret, "pnl": pnl, "realized_r": realized_r})

    bucket_stats = {}
    for b_key in ("<80", "80-84", "85-89", "90-94", "95-100"):
        items = buckets.get(b_key, [])
        if not items:
            continue
        wins = [i for i in items if i["pnl"] > 0]
        losses = [i for i in items if i["pnl"] < 0]
        gross_win = sum(i["pnl"] for i in wins)
        gross_loss = abs(sum(i["pnl"] for i in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
        avg_ret = sum(i["return"] for i in items) / len(items)

        bucket_stats[b_key] = {
            "count": len(items),
            "win_rate_pct": round(len(wins) / len(items) * 100.0, 2),
            "profit_factor": round(pf, 3),
            "avg_return_pct": round(avg_ret, 2),
            "net_pnl": round(gross_win - gross_loss, 2),
        }

    spearman_ret = calculate_spearman(scores, returns)
    spearman_r = calculate_spearman(scores[:len(r_multiples)], r_multiples) if r_multiples else None

    return {
        "bucket_stats": bucket_stats,
        "spearman_score_vs_return": spearman_ret,
        "spearman_score_vs_r_multiple": spearman_r,
    }


def main():
    parser = argparse.ArgumentParser(description="Experiment 1.6: Score Calibration")
    parser.add_argument("--start", default=START_DEFAULT)
    parser.add_argument("--end", default=END_DEFAULT)
    parser.add_argument("--output", default="runtime/reports/experiments/experiment_1_6_score_calibration.json")
    args = parser.parse_args()

    start_dt = parse_time(args.start)
    end_dt = parse_time(args.end)
    windows = walk_forward_windows(start_dt, end_dt, train_days=90, test_days=30, purge_days=1)
    cached_data = load_cached_bars(SYMBOLS_DEFAULT, start_dt, end_dt)

    base_configs = load_strategy_configs("config/watchers.json")
    base_configs = {s: base_configs[s] for s in SYMBOLS_DEFAULT if s in base_configs}
    base_configs, _ = prepare_filter_configs(base_configs, has_quotes=False, has_calendar=False, liquidity_scale=1.0)
    context = HistoricalFilterContext(None, None)

    thresholds = [70, 75, 80, 85]
    print(f"Sweeping setup score thresholds: {thresholds}")
    sweep_results = run_threshold_sweep(base_configs, windows, cached_data, thresholds, context)

    # Pick the baseline threshold (80) trades for decile analysis
    t80_trades = next(r["trades"] for r in sweep_results if r["threshold"] == 80)
    decile_analysis = analyze_score_deciles(t80_trades)

    print("\n" + "=" * 80)
    print("THRESHOLD SWEEP RESULTS (Fixed 3.5% Stop)")
    print("=" * 80)
    print(f"{'Threshold':<12} | {'Trades':<8} | {'WR %':<8} | {'PF':<8} | {'Net P/L':<12} | {'Win Windows':<10}")
    print("-" * 80)
    for r in sweep_results:
        print(f"{r['threshold']:<12} | {r['total_trades']:<8} | {r['win_rate_pct']:<8} | {r['profit_factor']:<8} | ${r['net_pnl']:<11.2f} | {r['profitable_windows']:<10}")
    print("=" * 80)

    print("\n" + "=" * 80)
    print("SETUP SCORE DECILE ANALYSIS (Threshold 80)")
    print("=" * 80)
    print(f"{'Score Bucket':<14} | {'Trades':<8} | {'WR %':<8} | {'PF':<8} | {'Avg Ret %':<10} | {'Net P/L':<12}")
    print("-" * 80)
    for b, s in decile_analysis["bucket_stats"].items():
        print(f"{b:<14} | {s['count']:<8} | {s['win_rate_pct']:<8} | {s['profit_factor']:<8} | {s['avg_return_pct']:<10} | ${s['net_pnl']:<11.2f}")
    print("=" * 80)
    print(f"Spearman rank correlation (Score vs Return): {decile_analysis['spearman_score_vs_return']}")

    # Strip raw trades before dumping JSON
    clean_sweep = []
    for r in sweep_results:
        c = dict(r)
        c.pop("trades", None)
        clean_sweep.append(c)

    out_path = ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(
            {
                "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "threshold_sweep": clean_sweep,
                "decile_analysis": decile_analysis,
            },
            f,
            indent=2,
        )
    print(f"\nWrote score calibration report to {out_path}")


if __name__ == "__main__":
    main()
