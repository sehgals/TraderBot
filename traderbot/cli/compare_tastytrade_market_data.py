"""Collect separate Alpaca and tastytrade observations for offline comparison."""

import argparse
import datetime as dt
import json
import sys
import urllib.error

from traderbot.core_strategy_engine.engine import AlpacaClient, load_env
from traderbot.data.feed_comparison import append_observation, compare_source_observations
from traderbot.data.tastytrade_market_data import TastytradeMarketDataClient


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="US equity symbols")
    parser.add_argument("--timeframe", default="5Min",
                        choices=("1Min", "5Min", "15Min", "30Min", "1Hour", "1Day"))
    parser.add_argument("--lookback-hours", type=float, default=8.0)
    parser.add_argument("--stream-seconds", type=float, default=8.0)
    parser.add_argument("--output-root", default="runtime/market_data")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    load_env()
    observed = dt.datetime.now(dt.timezone.utc)
    start = observed - dt.timedelta(hours=args.lookback_hours)
    start_text = start.isoformat().replace("+00:00", "Z")
    end_text = observed.isoformat().replace("+00:00", "Z")
    symbols = list(dict.fromkeys(symbol.upper() for symbol in args.symbols))
    try:
        tastytrade = TastytradeMarketDataClient()
        tastytrade_data = tastytrade.stream_snapshot(
            symbols, start_text, args.timeframe, args.stream_seconds
        )
    except (OSError, RuntimeError, TimeoutError, ValueError, urllib.error.URLError) as exc:
        print(f"tastytrade market-data collection failed: {exc}", file=sys.stderr)
        return 2

    alpaca = AlpacaClient()
    output = []
    for symbol in symbols:
        common = {
            "schema_version": 2, "symbol": symbol, "observed_at": end_text,
            "timeframe": args.timeframe, "start": start_text, "end": end_text,
        }
        alpaca_record = {
            **common, "source": "alpaca", "quote": alpaca.latest_quote(symbol),
            "bars": alpaca.stock_bars(symbol, start_text, end_text, args.timeframe),
        }
        observed_tastytrade = tastytrade_data[symbol]
        tastytrade_record = {
            **common, "source": "tastytrade",
            "quote": observed_tastytrade["quote"],
            "bars": observed_tastytrade["bars"],
        }
        comparison = compare_source_observations(
            symbol, alpaca_record, tastytrade_record, end_text,
            "alpaca", "tastytrade",
        )
        append_observation(args.output_root, "alpaca", alpaca_record)
        append_observation(args.output_root, "tastytrade", tastytrade_record)
        append_observation(args.output_root, "comparisons", comparison)
        output.append(comparison)
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
