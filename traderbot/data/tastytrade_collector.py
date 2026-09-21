"""Automatic, observation-only Alpaca/tastytrade market-data collection."""

import datetime as dt
from pathlib import Path

from traderbot.core_strategy_engine.engine import AlpacaClient
from traderbot.data.feed_comparison import append_observation, compare_source_observations
from traderbot.data.tastytrade_market_data import TastytradeMarketDataClient


def collect_tastytrade_comparison(settings, symbols, project_root, observed_at=None,
                                  alpaca_client=None, tastytrade_client=None):
    observed = observed_at or dt.datetime.now(dt.timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    observed = observed.astimezone(dt.timezone.utc)
    timeframe = settings.get("timeframe", "5Min")
    lookback_hours = max(0.1, float(settings.get("lookback_hours", 8)))
    stream_seconds = max(0.1, float(settings.get("stream_seconds", 8)))
    maximum_symbols = min(100, max(1, int(settings.get("maximum_symbols", 100))))
    selected = list(dict.fromkeys(str(symbol).upper() for symbol in symbols if symbol))
    selected = selected[:maximum_symbols]
    if not selected:
        return {"status": "tastytrade_collection_skipped", "reason": "no_symbols"}

    start = observed - dt.timedelta(hours=lookback_hours)
    start_text = start.isoformat().replace("+00:00", "Z")
    end_text = observed.isoformat().replace("+00:00", "Z")
    tastytrade = tastytrade_client or TastytradeMarketDataClient()
    alpaca = alpaca_client or AlpacaClient()
    tastytrade_data = tastytrade.stream_snapshot(
        selected, start_text, timeframe, stream_seconds
    )
    output_root = Path(project_root) / settings.get("output_root", "runtime/market_data")
    collected, failures = [], []
    for symbol in selected:
        try:
            common = {
                "schema_version": 1, "symbol": symbol, "observed_at": end_text,
                "timeframe": timeframe, "start": start_text, "end": end_text,
            }
            alpaca_record = {
                **common, "source": "alpaca", "quote": alpaca.latest_quote(symbol),
                "bars": alpaca.stock_bars(symbol, start_text, end_text, timeframe),
            }
            source = tastytrade_data[symbol]
            tastytrade_record = {
                **common, "source": "tastytrade", "quote": source["quote"],
                "bars": source["bars"],
            }
            comparison = compare_source_observations(
                symbol, alpaca_record, tastytrade_record, end_text,
                "alpaca", "tastytrade",
            )
            append_observation(output_root, "alpaca", alpaca_record)
            append_observation(output_root, "tastytrade", tastytrade_record)
            append_observation(output_root, "comparisons", comparison)
            collected.append(symbol)
        except Exception as exc:
            failures.append({
                "symbol": symbol, "error_type": type(exc).__name__, "error": str(exc),
            })
    return {
        "status": "tastytrade_collection_completed",
        "observed_at": end_text,
        "timeframe": timeframe,
        "requested_symbols": len(selected),
        "collected_symbols": collected,
        "failures": failures,
    }


__all__ = ["collect_tastytrade_comparison"]
