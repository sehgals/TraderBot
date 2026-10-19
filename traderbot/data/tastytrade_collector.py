"""Automatic, observation-only Alpaca/tastytrade market-data collection."""

import datetime as dt
from pathlib import Path

from traderbot.core_strategy_engine.engine import AlpacaClient
from traderbot.data.feed_comparison import append_observation, compare_source_observations
from traderbot.data.market_session import session_open_utc
from traderbot.data.tastytrade_market_data import TastytradeMarketDataClient


def collect_tastytrade_comparison(settings, symbols, project_root, observed_at=None,
                                  alpaca_client=None, tastytrade_client=None,
                                  tastytrade_stream=None):
    observed = observed_at or dt.datetime.now(dt.timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    observed = observed.astimezone(dt.timezone.utc)
    timeframe = settings.get("timeframe", "5Min")
    stream_seconds = max(0.1, float(settings.get("stream_seconds", 8)))
    maximum_symbols = min(100, max(1, int(settings.get("maximum_symbols", 100))))
    selected = list(dict.fromkeys(str(symbol).upper() for symbol in symbols if symbol))
    selected = selected[:maximum_symbols]
    if not selected:
        return {"status": "tastytrade_collection_skipped", "reason": "no_symbols"}

    alpaca = alpaca_client or AlpacaClient()
    start = session_open_utc(observed, alpaca)
    start_text = start.isoformat().replace("+00:00", "Z")
    end_text = observed.isoformat().replace("+00:00", "Z")
    if tastytrade_stream is None:
        tastytrade = tastytrade_client or TastytradeMarketDataClient()
        tastytrade_data = tastytrade.stream_snapshot(
            selected, start_text, timeframe, stream_seconds
        )
        stream_events = None
    else:
        if not tastytrade_stream.wait_ready(stream_seconds):
            return {"status": "tastytrade_collection_skipped", "reason": "stream_not_connected"}
        try:
            tastytrade_data, stream_events = tastytrade_stream.snapshot(selected, observed)
        except RuntimeError as exc:
            return {"status": "tastytrade_collection_skipped", "reason": "stream_disconnected",
                    "error": str(exc)}
    output_root = Path(project_root) / settings.get("output_root", "runtime/market_data")
    collected, failures, no_candle_symbols = [], [], []
    for symbol in selected:
        try:
            if tastytrade_stream is not None and not tastytrade_data[symbol]["bars"]:
                no_candle_symbols.append(symbol)
                continue
            common = {
                "schema_version": 3 if tastytrade_stream is not None else 2,
                "symbol": symbol, "observed_at": end_text,
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
            if tastytrade_stream is not None:
                comparison["schema_version"] = 3
                comparison["collection_mode"] = "continuous_stream"
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
        "no_candle_symbols": no_candle_symbols,
        "stream_events": stream_events,
        "stream_pid": getattr(tastytrade_stream, "pid", None),
    }


__all__ = ["collect_tastytrade_comparison"]
