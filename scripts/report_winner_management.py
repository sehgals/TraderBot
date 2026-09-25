"""Snapshot paper winner-management state for real-time observation."""
import argparse
import datetime
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traderbot.core_strategy_engine.engine import AlpacaClient, iso_utc, load_env, load_json  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchers", default="config/watchers.json")
    parser.add_argument("--output")
    args = parser.parse_args()
    watchers_path = Path(args.watchers).resolve()
    project_root = watchers_path.parent.parent if watchers_path.parent.name == "config" else watchers_path.parent
    config = load_json(watchers_path, {})
    load_env()
    client = AlpacaClient()
    positions = {item["symbol"]: item for item in client.positions()}
    rows = []
    events = []
    for watcher in config.get("managed_watchers", config.get("watchers", [])):
        symbol = watcher["symbol"]
        state = load_json(project_root / watcher["state"], {})
        episode = state.get("position_episode") or {}
        for event in episode.get("exit_events") or []:
            if event.get("exit_reason") in ("profit_tranche_2r", "runner_trailing_stop"):
                events.append({"symbol": symbol, **event})
        position = positions.get(symbol)
        if not position and not state.get("winner_partial_completed"):
            continue
        entry_price = float((position or {}).get("avg_entry_price") or episode.get("average_entry_price") or 0)
        current_price = float((position or {}).get("current_price") or 0)
        initial_risk = float(episode.get("initial_risk_per_share") or 0)
        if initial_risk <= 0:
            initial_stop = float(episode.get("initial_stop_price") or 0)
            initial_risk = entry_price - initial_stop if 0 < initial_stop < entry_price else 0
        trigger_r = float((config.get("winner_management") or {}).get("partial_exit_trigger_r", 2))
        trigger_price = entry_price + trigger_r * initial_risk if initial_risk > 0 else None
        current_r = (current_price - entry_price) / initial_risk if initial_risk > 0 else None
        position_qty = float((position or {}).get("qty") or 0)
        active_stop_qty = float(state.get("active_stop_qty") or 0)
        rows.append(
            {
                "symbol": symbol,
                "episode_id": episode.get("episode_id"),
                "position_qty": position_qty,
                "average_entry_price": entry_price,
                "current_price": current_price,
                "initial_risk_per_share": initial_risk or None,
                "current_r": round(current_r, 4) if current_r is not None else None,
                "partial_trigger_price": round(trigger_price, 4) if trigger_price is not None else None,
                "atr_ready": float(state.get("position_health_atr14") or 0) > 0,
                "partial_completed": bool(state.get("winner_partial_completed")),
                "partial_order_id": state.get("winner_partial_order_id"),
                "partial_qty": state.get("winner_partial_qty"),
                "partial_filled_at": state.get("winner_partial_filled_at"),
                "runner_high_price": state.get("runner_high_price"),
                "runner_stop_price": state.get("runner_stop_price"),
                "hourly_atr14": state.get("position_health_atr14"),
                "hourly_atr_as_of": state.get("position_health_atr_as_of"),
                "active_stop_price": state.get("active_stop_price"),
                "active_stop_qty": active_stop_qty,
                "stop_fully_covers_position": active_stop_qty >= position_qty > 0,
                "active_stop_reason": state.get("active_stop_reason"),
            }
        )
    now = datetime.datetime.now(datetime.timezone.utc)
    report = {
        "generated_at": iso_utc(now),
        "paper_account": "paper-api" in client.trade_base_url.lower(),
        "configuration": config.get("winner_management") or {},
        "active_positions": rows,
        "winner_exit_events": events,
    }
    output = Path(args.output) if args.output else project_root / "runtime" / "reports" / f"winner_management_{now.date().isoformat()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
