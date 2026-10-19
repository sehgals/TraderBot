"""Experiment 1.1: MAE/MFE Distribution Study of Live and Simulated Trades.

Calculates the Maximum Adverse Excursion (MAE) and Maximum Favorable Excursion (MFE)
for winning and losing trades to determine:
1. The stop quantile / width that retains >=70%, >=80%, >=90% of winners.
2. The point at which losing trades become unrecoverable.
3. The MFE distribution to design realistic profit targets and scale-out thresholds.
"""
import argparse
import datetime
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traderbot.core_strategy_engine.engine import AlpacaClient, load_env, load_json, parse_alpaca_time


def quantile(values, q):
    """Compute empirical quantile (q between 0 and 1) with linear interpolation."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    idx = (n - 1) * q
    low = int(math.floor(idx))
    high = int(math.ceil(idx))
    weight = idx - low
    return round(s[low] * (1.0 - weight) + s[high] * weight, 4)


def distribution_summary(values):
    """Return percentiles, mean, min, max for a list of floats."""
    if not values:
        return {"count": 0}
    clean = [float(v) for v in values if v is not None and not math.isnan(v)]
    if not clean:
        return {"count": 0}
    clean.sort()
    return {
        "count": len(clean),
        "mean": round(sum(clean) / len(clean), 4),
        "min": round(clean[0], 4),
        "p25": quantile(clean, 0.25),
        "p50_median": quantile(clean, 0.50),
        "p70": quantile(clean, 0.70),
        "p75": quantile(clean, 0.75),
        "p80": quantile(clean, 0.80),
        "p90": quantile(clean, 0.90),
        "p95": quantile(clean, 0.95),
        "max": round(clean[-1], 4),
    }


def analyze_simulated_trades(backtest_path):
    """Analyze MAE and MFE for simulated trades from a walk-forward run."""
    data = load_json(backtest_path, {})
    trades = data.get("trades", [])
    if not trades:
        print(f"Warning: No trades found in {backtest_path}")
        return {}

    winners = []
    losers = []
    all_analyzed = []

    for t in trades:
        init_notional = float(t.get("initial_notional") or 0)
        if init_notional <= 0:
            continue
        pnl = float(t.get("pnl") or t.get("realized_pnl") or 0)
        base_qty = float(t.get("base_qty") or t.get("qty") or 1)
        init_risk_per_share = float(t.get("initial_risk_per_share") or 0)
        init_risk_dollars = init_risk_per_share * base_qty if init_risk_per_share > 0 else None

        # MAE: peak unrealized drawdown in trade
        max_unrealized_loss = abs(min(0.0, float(t.get("max_unrealized_loss") or 0)))
        mae_pct = (max_unrealized_loss / init_notional) * 100.0
        mae_r = (max_unrealized_loss / init_risk_dollars) if init_risk_dollars and init_risk_dollars > 0 else None

        # MFE: peak unrealized gain in trade
        max_capital = float(t.get("max_capital") or init_notional)
        peak_gain = max(0.0, max_capital - init_notional)
        mfe_pct = (peak_gain / init_notional) * 100.0
        mfe_r = (peak_gain / init_risk_dollars) if init_risk_dollars and init_risk_dollars > 0 else None

        # Efficiency: realized gain vs MFE (for winners)
        efficiency = (pnl / peak_gain) if (pnl > 0 and peak_gain > 0) else None

        row = {
            "symbol": t.get("symbol"),
            "entry_time": t.get("entry_time"),
            "exit_time": t.get("exit_time"),
            "exit_reason": t.get("exit_reason"),
            "pnl": pnl,
            "return_pct": float(t.get("return_percent") or (pnl / init_notional * 100.0)),
            "realized_r": (pnl / init_risk_dollars) if init_risk_dollars and init_risk_dollars > 0 else None,
            "mae_pct": round(mae_pct, 4),
            "mae_r": round(mae_r, 4) if mae_r is not None else None,
            "mfe_pct": round(mfe_pct, 4),
            "mfe_r": round(mfe_r, 4) if mfe_r is not None else None,
            "efficiency": round(efficiency, 4) if efficiency is not None else None,
            "bars_held": int(t.get("bars_held") or 0),
        }
        all_analyzed.append(row)
        if pnl > 0:
            winners.append(row)
        else:
            losers.append(row)

    return {
        "sample_size": len(all_analyzed),
        "winners_count": len(winners),
        "losers_count": len(losers),
        "win_rate_pct": round(len(winners) / len(all_analyzed) * 100.0, 2) if all_analyzed else 0,
        "winner_mae_pct": distribution_summary([w["mae_pct"] for w in winners]),
        "winner_mae_r": distribution_summary([w["mae_r"] for w in winners if w["mae_r"] is not None]),
        "loser_mae_pct": distribution_summary([l["mae_pct"] for l in losers]),
        "loser_mae_r": distribution_summary([l["mae_r"] for l in losers if l["mae_r"] is not None]),
        "winner_mfe_pct": distribution_summary([w["mfe_pct"] for w in winners]),
        "winner_mfe_r": distribution_summary([w["mfe_r"] for w in winners if w["mfe_r"] is not None]),
        "loser_mfe_pct": distribution_summary([l["mfe_pct"] for l in losers]),
        "loser_mfe_r": distribution_summary([l["mfe_r"] for l in losers if l["mfe_r"] is not None]),
        "winner_efficiency": distribution_summary([w["efficiency"] for w in winners if w["efficiency"] is not None]),
        "trades": all_analyzed,
    }


def analyze_live_trades(ledger_path, client=None):
    """Analyze holding times and adverse/favorable outcomes of live episodes."""
    data = load_json(ledger_path, {})
    episodes = data.get("episodes", [])
    if not episodes:
        print(f"Warning: No episodes found in {ledger_path}")
        return {}

    winners = []
    losers = []
    holding_hours_all = []
    holding_hours_winners = []
    holding_hours_losers = []

    for ep in episodes:
        pnl = float(ep.get("realized_pl") or 0)
        h_hours = ep.get("holding_hours")
        if h_hours is None and ep.get("entry_time") and ep.get("last_exit_time"):
            try:
                t0 = parse_alpaca_time(ep["entry_time"])
                t1 = parse_alpaca_time(ep["last_exit_time"])
                if t0 and t1:
                    h_hours = round((t1 - t0).total_seconds() / 3600, 2)
            except Exception:
                pass

        row = {
            "episode_id": ep.get("episode_id"),
            "symbol": ep.get("symbol"),
            "entry_time": ep.get("entry_time"),
            "last_exit_time": ep.get("last_exit_time"),
            "exit_events": ep.get("exit_events"),
            "realized_pl": pnl,
            "exit_reasons": ep.get("exit_reasons", []),
            "holding_hours": h_hours,
            "mfe_percent": ep.get("mfe_percent"),
            "mae_percent": ep.get("mae_percent"),
            "mfe_r": ep.get("mfe_r"),
            "mae_r": ep.get("mae_r"),
        }

        if h_hours is not None:
            holding_hours_all.append(h_hours)
            if pnl > 0:
                holding_hours_winners.append(h_hours)
            else:
                holding_hours_losers.append(h_hours)

        if pnl > 0:
            winners.append(row)
        else:
            losers.append(row)

    # If client is provided and we want to sample bar-level MAE for live trades:
    # We can fetch daily/hourly bars for each trade
    live_trades_with_bars = 0
    if client:
        by_symbol = defaultdict(list)
        for ep in episodes:
            by_symbol[ep["symbol"]].append(ep)
        
        for symbol, sym_episodes in by_symbol.items():
            valid_eps = [
                e for e in sym_episodes
                if e.get("entry_time") and e.get("last_exit_time") and e.get("mae_percent") is None
            ]
            if not valid_eps:
                continue
            min_t = min(e["entry_time"] for e in valid_eps)
            max_t = max(e["last_exit_time"] for e in valid_eps)
            try:
                raw_bars = client.stock_bars(symbol, min_t, max_t, "1Day")
                if not raw_bars:
                    continue
                bar_list = [
                    (parse_alpaca_time(b["t"]), float(b["h"]), float(b["l"]), float(b["c"]))
                    for b in raw_bars
                ]
                for ep in valid_eps:
                    t_in = parse_alpaca_time(ep["entry_time"])
                    t_out = parse_alpaca_time(ep["last_exit_time"])
                    trade_bars = [b for b in bar_list if t_in <= b[0] <= t_out]
                    if trade_bars:
                        highs = [b[1] for b in trade_bars]
                        lows = [b[2] for b in trade_bars]
                        ep["sampled_max_price"] = max(highs)
                        ep["sampled_min_price"] = min(lows)
                        live_trades_with_bars += 1
            except Exception as exc:
                print(f"Note: Could not fetch bars for {symbol}: {exc}", file=sys.stderr)

    return {
        "sample_size": len(episodes),
        "winners_count": len(winners),
        "losers_count": len(losers),
        "win_rate_pct": round(len(winners) / len(episodes) * 100.0, 2) if episodes else 0,
        "holding_hours_all": distribution_summary(holding_hours_all),
        "holding_hours_winners": distribution_summary(holding_hours_winners),
        "holding_hours_losers": distribution_summary(holding_hours_losers),
        "winner_mfe_pct": distribution_summary([w["mfe_percent"] for w in winners if w.get("mfe_percent") is not None]),
        "winner_mae_pct": distribution_summary([w["mae_percent"] for w in winners if w.get("mae_percent") is not None]),
        "loser_mfe_pct": distribution_summary([l["mfe_percent"] for l in losers if l.get("mfe_percent") is not None]),
        "loser_mae_pct": distribution_summary([l["mae_percent"] for l in losers if l.get("mae_percent") is not None]),
        "episodes": episodes,
    }


def main():
    parser = argparse.ArgumentParser(description="Experiment 1.1: MAE/MFE Study")
    parser.add_argument(
        "--backtest",
        default="runtime/reports/backtests/liquidity_scale_1.0.json",
        help="Path to simulated walk-forward backtest JSON",
    )
    parser.add_argument(
        "--ledger",
        default="runtime/reports/exit_ledger_baseline_2026-10-17.json",
        help="Path to exit ledger baseline JSON",
    )
    parser.add_argument(
        "--output",
        default="runtime/reports/experiments/experiment_1_1_mae_mfe.json",
        help="Path to output JSON results",
    )
    args = parser.parse_args()

    backtest_file = ROOT / args.backtest
    ledger_file = ROOT / args.ledger
    output_file = ROOT / args.output
    output_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"Analyzing simulated trades from {backtest_file}...")
    sim_results = analyze_simulated_trades(backtest_file)

    print(f"Analyzing live trades from {ledger_file}...")
    live_results = analyze_live_trades(ledger_file)

    report = {
        "experiment": "1.1 MAE/MFE Study of Live and Simulated Trades",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "simulated": {
            "source_file": str(backtest_file),
            "sample_size": sim_results.get("sample_size", 0),
            "win_rate_pct": sim_results.get("win_rate_pct", 0),
            "winner_mae_pct": sim_results.get("winner_mae_pct"),
            "winner_mae_r": sim_results.get("winner_mae_r"),
            "loser_mae_pct": sim_results.get("loser_mae_pct"),
            "loser_mae_r": sim_results.get("loser_mae_r"),
            "winner_mfe_pct": sim_results.get("winner_mfe_pct"),
            "winner_mfe_r": sim_results.get("winner_mfe_r"),
            "loser_mfe_pct": sim_results.get("loser_mfe_pct"),
            "loser_mfe_r": sim_results.get("loser_mfe_r"),
            "winner_efficiency": sim_results.get("winner_efficiency"),
        },
        "live": {
            "source_file": str(ledger_file),
            "sample_size": live_results.get("sample_size", 0),
            "win_rate_pct": live_results.get("win_rate_pct", 0),
            "holding_hours_all": live_results.get("holding_hours_all"),
            "holding_hours_winners": live_results.get("holding_hours_winners"),
            "holding_hours_losers": live_results.get("holding_hours_losers"),
        },
        "recommendations": {
            "stop_retention_quantile_70": {
                "winner_mae_pct_p70": (sim_results.get("winner_mae_pct") or {}).get("p70"),
                "winner_mae_r_p70": (sim_results.get("winner_mae_r") or {}).get("p70"),
                "implication": "Setting a structural stop tighter than p70 prematurely terminates >30% of natural winning trades.",
            },
            "stop_retention_quantile_80": {
                "winner_mae_pct_p80": (sim_results.get("winner_mae_pct") or {}).get("p80"),
                "winner_mae_r_p80": (sim_results.get("winner_mae_r") or {}).get("p80"),
                "implication": "Setting a structural stop at p80 gives 80% of winning trades sufficient room to develop without taking excess risk.",
            },
            "target_mfe_levels": {
                "winner_mfe_pct_median": (sim_results.get("winner_mfe_pct") or {}).get("p50_median"),
                "winner_mfe_r_median": (sim_results.get("winner_mfe_r") or {}).get("p50_median"),
                "winner_mfe_pct_p75": (sim_results.get("winner_mfe_pct") or {}).get("p75"),
                "winner_mfe_r_p75": (sim_results.get("winner_mfe_r") or {}).get("p75"),
                "implication": "Optimal first profit tranche should be placed near median MFE (locking in bulk of wins) with remainder trailing to capture p75+ runners.",
            },
        },
    }

    output_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote results to {output_file}")

    # Print summary to stdout
    print("\n" + "=" * 60)
    print("EXPERIMENT 1.1 RESULTS SUMMARY")
    print("=" * 60)
    print(f"Simulated Trades: {report['simulated']['sample_size']} trades (Win Rate: {report['simulated']['win_rate_pct']}%)")
    w_mae = report['simulated']['winner_mae_pct']
    w_mae_r = report['simulated']['winner_mae_r']
    print("\n--- WINNER MAXIMUM ADVERSE EXCURSION (MAE) ---")
    print(f"Median (50%): {w_mae.get('p50_median')}% ({w_mae_r.get('p50_median')} R)")
    print(f"70th Quantile: {w_mae.get('p70')}% ({w_mae_r.get('p70')} R)")
    print(f"80th Quantile: {w_mae.get('p80')}% ({w_mae_r.get('p80')} R)")
    print(f"90th Quantile: {w_mae.get('p90')}% ({w_mae_r.get('p90')} R)")

    l_mae = report['simulated']['loser_mae_pct']
    l_mae_r = report['simulated']['loser_mae_r']
    print("\n--- LOSER MAXIMUM ADVERSE EXCURSION (MAE) ---")
    print(f"Median (50%): {l_mae.get('p50_median')}% ({l_mae_r.get('p50_median')} R)")
    print(f"75th Quantile: {l_mae.get('p75')}% ({l_mae_r.get('p75')} R)")

    w_mfe = report['simulated']['winner_mfe_pct']
    w_mfe_r = report['simulated']['winner_mfe_r']
    print("\n--- WINNER MAXIMUM FAVORABLE EXCURSION (MFE) ---")
    print(f"Median (50%): +{w_mfe.get('p50_median')}% (+{w_mfe_r.get('p50_median')} R)")
    print(f"75th Quantile: +{w_mfe.get('p75')}% (+{w_mfe_r.get('p75')} R)")
    print(f"90th Quantile: +{w_mfe.get('p90')}% (+{w_mfe_r.get('p90')} R)")

    print("\n--- LIVE EPISODES HOLDING TIME ---")
    live_hold = report['live']['holding_hours_all']
    print(f"Total Live Episodes: {report['live']['sample_size']}")
    print(f"Holding Hours Median: {live_hold.get('p50_median')}h (Mean: {live_hold.get('mean')}h, P75: {live_hold.get('p75')}h)")
    print("=" * 60)


if __name__ == "__main__":
    main()
