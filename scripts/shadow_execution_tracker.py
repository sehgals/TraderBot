"""Phase 2: Shadow Execution Tracker.

Monitors and compares live/paper active execution vs shadow execution (3.5% structural stops,
2-bar regime hysteresis, and Target Mode v2 multi-tranche exits: 50% at 1.0R, 25% at 2.0R, 25% runner).

Scans strategy states and exit ledger to produce real-time and historical comparison telemetry.
Outputs report to runtime/reports/shadow_execution_daily.json and prints markdown summary.
"""
import argparse
import datetime
import glob
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from traderbot.core_strategy_engine.rollout import get_symbol_rollout_cohort


def load_json_safe(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def scan_active_and_shadow_positions(state_dir):
    state_files = glob.glob(os.path.join(state_dir, "*_strategy_state.json"))
    positions = []

    for sf in state_files:
        st = load_json_safe(sf)
        episode = st.get("position_episode") or {}
        if not episode or episode.get("state") not in ("OPEN", "OPEN_PARTIAL"):
            continue

        symbol = episode.get("symbol") or Path(sf).stem.replace("_strategy_state", "").upper()
        entry_price = float(episode.get("average_entry_price") or 0.0)
        qty = int(float(episode.get("current_qty") or 0))
        if entry_price <= 0 or qty <= 0:
            continue

        active_stop = float(episode.get("initial_stop_price") or st.get("active_stop_price") or 0.0)
        active_risk = round(entry_price - active_stop, 2) if active_stop else 0.0

        shadow = episode.get("shadow_tracking") or {}
        shadow_stop = float(shadow.get("shadow_stop_price") or entry_price * 0.965)
        shadow_targets = shadow.get("shadow_targets") or {}
        t1 = shadow_targets.get("tranche_1_price")
        t2 = shadow_targets.get("tranche_2_price")

        cohort = episode.get("rollout_cohort") or get_symbol_rollout_cohort(symbol)
        positions.append({
            "symbol": symbol,
            "cohort": cohort,
            "opened_at": episode.get("opened_at"),
            "entry_price": entry_price,
            "qty": qty,
            "max_price": float(episode.get("max_price") or entry_price),
            "min_price": float(episode.get("min_price") or entry_price),
            "mfe_percent": float(episode.get("mfe_percent") or 0.0),
            "mae_percent": float(episode.get("mae_percent") or 0.0),
            "active": {
                "stop_price": active_stop,
                "risk_per_share": active_risk,
                "stop_percent": round((entry_price - active_stop) / entry_price * 100, 2) if entry_price and active_stop else None,
            },
            "shadow": {
                "stop_price": shadow_stop,
                "stop_percent": float(shadow.get("calibrated_stop_percent") or 3.5),
                "tranche_1_price": t1,
                "tranche_1_filled": shadow.get("tranche_1_filled", False),
                "tranche_2_price": t2,
                "tranche_2_filled": shadow.get("tranche_2_filled", False),
                "shadow_realized_pnl": float(shadow.get("shadow_realized_pnl") or 0.0),
                "shadow_remaining_qty": shadow.get("shadow_remaining_qty", qty),
                "shadow_closed": shadow.get("shadow_closed", False),
                "shadow_exit_reason": shadow.get("shadow_exit_reason"),
            },
        })

    return positions


def main():
    parser = argparse.ArgumentParser(description="Shadow Execution Tracker")
    parser.add_argument("--state-dir", default="runtime/state")
    parser.add_argument("--output", default="runtime/reports/shadow_execution_daily.json")
    args = parser.parse_args()

    state_path = ROOT / args.state_dir
    positions = scan_active_and_shadow_positions(str(state_path))

    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "open_positions_count": len(positions),
        "open_positions": positions,
    }

    out_file = ROOT / args.output
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("=" * 95)
    print("PHASE 2: SHADOW EXECUTION TRACKER SUMMARY")
    print("=" * 95)
    print(f"Active Monitored Positions: {len(positions)}")
    if positions:
        print(f"{'Symbol':<8} | {'Cohort':<8} | {'Entry':<8} | {'Active Stop':<12} | {'Shadow Stop':<12} | {'Tranche 1 (1.0R)':<16} | {'Tranche 2 (2.0R)':<16} | {'Shadow Realized P/L':<15}")
        print("-" * 95)
        for p in positions:
            act = p["active"]
            sh = p["shadow"]
            t1_str = f"${sh['tranche_1_price']:.2f} ({'FILLED' if sh['tranche_1_filled'] else 'PENDING'})" if sh['tranche_1_price'] else "N/A"
            t2_str = f"${sh['tranche_2_price']:.2f} ({'FILLED' if sh['tranche_2_filled'] else 'PENDING'})" if sh['tranche_2_price'] else "N/A"
            print(f"{p['symbol']:<8} | {p.get('cohort', 'control').upper():<8} | ${p['entry_price']:<7.2f} | ${act['stop_price']:<6.2f} ({act['stop_percent']}%) | ${sh['stop_price']:<6.2f} ({sh['stop_percent']}%) | {t1_str:<16} | {t2_str:<16} | ${sh['shadow_realized_pnl']:<14.2f}")
    else:
        print("No active open positions currently in runtime state.")
    print("=" * 95)
    print(f"Wrote shadow tracking report to {out_file}")


if __name__ == "__main__":
    main()
