"""Build an episode-aware baseline of realized exits and post-exit recovery."""
import argparse
import datetime
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traderbot.backtester.reentry_backtest import (  # noqa: E402
    iso_utc,
    load_strategy_configs,
    pair_completed_trades,
)
from traderbot.core_strategy_engine.engine import AlpacaClient, load_env, load_json  # noqa: E402


UTC = datetime.timezone.utc


def parse_time(value):
    parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_exit_attribution(watchers_path):
    watchers_file = Path(watchers_path).resolve()
    root = watchers_file.parent.parent if watchers_file.parent.name == "config" else watchers_file.parent
    payload = load_json(watchers_file, {})
    attribution = {}
    for watcher in payload.get("managed_watchers", payload.get("watchers", [])):
        state = load_json(root / watcher["state"], {})
        for order_id, item in (state.get("exit_order_intents") or {}).items():
            attribution[order_id] = item
        for episode in state.get("closed_position_episodes") or []:
            for event in episode.get("exit_events") or []:
                if event.get("order_id"):
                    attribution[event["order_id"]] = event
    return attribution


def enrich_fills(fills, attribution):
    enriched = []
    for fill in fills:
        item = dict(fill)
        details = attribution.get(fill.get("order_id")) or {}
        item["episode_id"] = details.get("episode_id")
        item["exit_reason"] = details.get("exit_reason") or "unknown"
        enriched.append(item)
    return enriched


def recovery_metrics(client, trades, end, horizons=(1, 3, 5, 10)):
    by_symbol = defaultdict(list)
    for trade in trades:
        by_symbol[trade["symbol"]].append(trade)
    analyzed = []
    start = iso_utc(min(trade["exit_time"] for trade in trades)) if trades else end
    for symbol, symbol_trades in sorted(by_symbol.items()):
        raw_bars = client.stock_bars(symbol, start, end, "1Day")
        bars = [
            (parse_time(bar["t"]), float(bar["h"]), float(bar["l"]), float(bar["c"]))
            for bar in raw_bars
        ]
        for trade in symbol_trades:
            row = dict(trade)
            future = [bar for bar in bars if bar[0].date() > trade["exit_time"].date()]
            for horizon in horizons:
                window = future[:horizon]
                if len(window) < horizon:
                    continue
                row[f"peak_{horizon}d_pct"] = (
                    max(bar[1] for bar in window) / trade["exit_price"] - 1
                ) * 100
                row[f"close_{horizon}d_pct"] = (
                    window[-1][3] / trade["exit_price"] - 1
                ) * 100
                row[f"drawdown_{horizon}d_pct"] = (
                    min(bar[2] for bar in window) / trade["exit_price"] - 1
                ) * 100
                row[f"recovered_entry_{horizon}d"] = (
                    max(bar[1] for bar in window) >= trade["avg_entry"]
                )
            analyzed.append(row)
    return analyzed


def summarize_window(rows, horizon):
    rows = [row for row in rows if f"peak_{horizon}d_pct" in row]
    if not rows:
        return {"sample_size": 0}
    return {
        "sample_size": len(rows),
        "median_peak_pct": round(statistics.median(row[f"peak_{horizon}d_pct"] for row in rows), 4),
        "median_close_pct": round(statistics.median(row[f"close_{horizon}d_pct"] for row in rows), 4),
        "median_drawdown_pct": round(statistics.median(row[f"drawdown_{horizon}d_pct"] for row in rows), 4),
        "percent_closed_above_exit": round(100 * sum(row[f"close_{horizon}d_pct"] > 0 for row in rows) / len(rows), 2),
        "percent_recovered_entry": round(100 * sum(row[f"recovered_entry_{horizon}d"] for row in rows) / len(rows), 2),
    }


def build_report(fills, trades, rows, start, end):
    winners = [row for row in rows if row["realized_pl"] >= 0]
    losers = [row for row in rows if row["realized_pl"] < 0]
    episodes = defaultdict(list)
    for trade in trades:
        episode_id = trade.get("episode_id") or (
            f"unknown:{trade['symbol']}:{trade['entry_time']}"
        )
        episodes[episode_id].append(trade)
    return {
        "schema_version": 1,
        "generated_at": iso_utc(datetime.datetime.now(UTC)),
        "window": {"start": start, "end": end},
        "fill_count": len(fills),
        "exit_event_count": len(trades),
        "position_episode_count": len(episodes),
        "realized_pl": round(sum(trade["realized_pl"] for trade in trades), 2),
        "exit_reasons": dict(sorted(Counter(trade.get("exit_reason") or "unknown" for trade in trades).items())),
        "recovery": {
            group: {str(horizon): summarize_window(items, horizon) for horizon in (1, 3, 5, 10)}
            for group, items in (("all", rows), ("winners", winners), ("losers", losers))
        },
        "episodes": [
            {
                "episode_id": episode_id,
                "symbol": events[0]["symbol"],
                "entry_time": iso_utc(min(event["entry_time"] for event in events)),
                "last_exit_time": iso_utc(max(event["exit_time"] for event in events)),
                "exit_events": len(events),
                "realized_pl": round(sum(event["realized_pl"] for event in events), 2),
                "exit_reasons": sorted(set(event.get("exit_reason") or "unknown" for event in events)),
            }
            for episode_id, events in episodes.items()
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchers", default="config/watchers.json")
    parser.add_argument("--start", default="2026-06-01T00:00:00Z")
    parser.add_argument("--end", default=iso_utc(datetime.datetime.now(UTC)))
    parser.add_argument("--output")
    args = parser.parse_args()

    load_env()
    client = AlpacaClient()
    symbols = set(load_strategy_configs(args.watchers))
    fills = enrich_fills(client.fills(args.start, args.end), load_exit_attribution(args.watchers))
    trades = pair_completed_trades(fills, symbols)
    rows = recovery_metrics(client, trades, args.end)
    report = build_report(fills, trades, rows, args.start, args.end)
    output = Path(args.output) if args.output else ROOT / "runtime" / "reports" / f"exit_ledger_baseline_{datetime.date.today().isoformat()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("fill_count", "exit_event_count", "position_episode_count", "realized_pl", "exit_reasons", "recovery")}, indent=2))
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
