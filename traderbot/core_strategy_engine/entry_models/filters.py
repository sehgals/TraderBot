import datetime
import math
from traderbot.core_strategy_engine.entry_models.entry_safety import quote_diagnostics


PROFILE_LIQUIDITY_DEFAULTS = {
    "index_etf": 1_000_000,
    "large_cap": 500_000,
    "high_vol_growth": 250_000,
    "speculative": 100_000,
}


def _settings(config, name):
    value = (config.get("entry_filters") or {}).get(name, {})
    if isinstance(value, bool):
        return {"enabled": value}
    return dict(value or {})


def _missing_passes(settings):
    return settings.get("missing_data_policy", "block") == "allow"


def _calendar_covered(calendar, symbol, as_of):
    coverage = (calendar or {}).get("coverage") or {}
    if not coverage.get("point_in_time"):
        return False
    symbols = {str(item).upper() for item in coverage.get("symbols", [])}
    if not symbols and not coverage.get("all_symbols"):
        return False
    if symbols and symbol.upper() not in symbols:
        return False
    start = coverage.get("start")
    end = coverage.get("end")
    day = as_of.date()
    if start and day < datetime.date.fromisoformat(start):
        return False
    if end and day > datetime.date.fromisoformat(end):
        return False
    return True


def _known_events(calendar, symbol, as_of, event_types):
    known = []
    for event in (calendar or {}).get("events", []):
        if str(event.get("symbol", "")).upper() != symbol.upper():
            continue
        if str(event.get("type", "")).lower() not in event_types:
            continue
        known_at = event.get("known_at")
        if not known_at:
            continue
        timestamp = datetime.datetime.fromisoformat(str(known_at).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
        if timestamp <= as_of.astimezone(datetime.timezone.utc):
            known.append(event)
    return known


def _event_window_clear(events, as_of, days_before, days_after):
    current_day = as_of.date()
    for event in events:
        event_day = datetime.date.fromisoformat(event["date"])
        delta = (event_day - current_day).days
        if -days_after <= delta <= days_before:
            return False, event
    return True, None


def evaluate_entry_filters(features, config=None, context=None):
    """Evaluate model-neutral, pre-trade filters without external side effects."""
    config = config or {}
    context = context or {}
    bar = features["bar"]
    symbol = features["symbol"]
    as_of = features["as_of"]
    checks = {}
    metrics = {}

    liquidity = _settings(config, "liquidity")
    if liquidity.get("enabled", False):
        minimum = float(
            liquidity.get(
                "minimum_intraday_dollar_volume", liquidity.get("minimum_average_dollar_volume",
                PROFILE_LIQUIDITY_DEFAULTS.get(config.get("risk_profile"), 250_000)),
            )
        )
        sample_size = int(bar.get("dollar_volume_sample_size") or 0)
        average = bar.get("matched_average_dollar_volume")
        if not isinstance(average, (float, int)) or not math.isfinite(average) or average < 0:
            average = None
        enough_samples = sample_size >= int(liquidity.get("minimum_sample_size", 5))
        checks["liquidity_data_available"] = average is not None and enough_samples
        checks["liquidity_ok"] = bool(
            average is not None and enough_samples and float(average) >= minimum
        )
        metrics.update(
            {
                "average_dollar_volume": average,
                "matched_intraday_average_dollar_volume": average,
                "liquidity_feed": context.get("feed", "unknown"),
                "liquidity_coverage": "single_exchange" if context.get("feed") == "iex" else "feed_specific",
                "liquidity_timeframe": context.get("timeframe", config.get("dynamic_timeframe", "5Min")),
                "dollar_volume_sample_size": sample_size,
                "minimum_average_dollar_volume": minimum,
            }
        )

    daily = context.get("daily_liquidity") or {}
    metrics.update(daily)
    daily_min = liquidity.get("minimum_daily_dollar_volume")
    if liquidity.get("enabled", False) and daily_min is not None:
        average = daily.get("average_daily_dollar_volume")
        available = isinstance(average, (int, float)) and math.isfinite(average) and daily.get("daily_sample_size", 0) >= int(liquidity.get("minimum_daily_sample_size", 5))
        checks["daily_liquidity_data_available"] = available
        checks["daily_liquidity_ok"] = available and average >= float(daily_min)
        metrics["minimum_daily_dollar_volume"] = float(daily_min)

    spread = _settings(config, "spread")
    if spread.get("enabled", False):
        quote = context.get("quote") or {}
        diagnostics = quote_diagnostics(quote, context.get("evaluated_at") or as_of, spread)
        valid = diagnostics["valid"]
        spread_percent = diagnostics["quoted_spread_percent"] if valid else None
        quote_time = quote.get("timestamp")
        metrics.update(diagnostics)
        metrics["quote_attempts"] = context.get("quote_attempts", [])
        checks["spread_data_available"] = valid or _missing_passes(spread)
        checks["spread_ok"] = (
            spread_percent is not None
            and spread_percent <= float(spread.get("maximum_percent", 0.50))
        ) or (spread_percent is None and _missing_passes(spread))
        metrics.update(
            {
                "quoted_spread_percent": spread_percent,
                "maximum_spread_percent": float(spread.get("maximum_percent", 0.50)),
                "spread_source": "quote" if spread_percent is not None else None,
                "quote_timestamp": quote_time,
            }
        )

    gap = _settings(config, "gap")
    if gap.get("enabled", False):
        gap_percent = bar.get("session_gap_percent")
        available = gap_percent is not None
        maximum = float(gap.get("maximum_absolute_percent", 8.0))
        checks["gap_data_available"] = available or _missing_passes(gap)
        checks["gap_ok"] = (
            available and abs(float(gap_percent)) <= maximum
        ) or (not available and _missing_passes(gap))
        metrics.update(
            {
                "session_gap_percent": gap_percent,
                "maximum_absolute_gap_percent": maximum,
            }
        )

    calendar = context.get("event_calendar")
    for name, event_types, default_before, default_after in (
        ("earnings", {"earnings"}, 2, 1),
        (
            "corporate_actions",
            {
                "split",
                "forward_split",
                "reverse_split",
                "unit_split",
                "merger",
                "cash_merger",
                "stock_merger",
                "stock_and_cash_merger",
                "spinoff",
                "spin_off",
                "reorganization",
                "name_change",
                "symbol_change",
                "redemption",
            },
            3,
            1,
        ),
    ):
        settings = _settings(config, name)
        if not settings.get("enabled", False):
            continue
        covered = _calendar_covered(calendar, symbol, as_of)
        events = _known_events(calendar, symbol, as_of, event_types) if covered else []
        clear, blocking_event = _event_window_clear(
            events,
            as_of,
            int(settings.get("blackout_days_before", default_before)),
            int(settings.get("blackout_days_after", default_after)),
        )
        checks[f"{name}_data_available"] = covered or _missing_passes(settings)
        checks[f"{name}_ok"] = (covered and clear) or (
            not covered and _missing_passes(settings)
        )
        metrics[f"{name}_blocking_event"] = blocking_event

    return {
        "checks": checks,
        "blockers": [name for name, passed in checks.items() if not passed],
        "metrics": metrics,
    }


__all__ = ["PROFILE_LIQUIDITY_DEFAULTS", "evaluate_entry_filters"]
