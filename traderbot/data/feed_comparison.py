"""Persistence and comparison helpers for isolated market-data observations."""

import datetime as dt
import json
from pathlib import Path


def _midpoint(quote):
    bid, ask = quote.get("bid_price"), quote.get("ask_price")
    return (float(bid) + float(ask)) / 2 if bid and ask else None


def _spread_percent(quote):
    midpoint = _midpoint(quote)
    if not midpoint:
        return None
    return (float(quote["ask_price"]) - float(quote["bid_price"])) / midpoint * 100


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _bar_window_stats(record, observed_at):
    bars = record.get("bars") or []
    start = _parse_time(record.get("start"))
    end = _parse_time(record.get("end")) or _parse_time(observed_at)
    valid = []
    for bar in bars:
        timestamp = _parse_time(bar.get("t"))
        if timestamp and (start is None or timestamp >= start) and (end is None or timestamp <= end):
            valid.append(bar)
    latest = valid[-1] if valid else None
    latest_at = _parse_time((latest or {}).get("t"))
    observed = _parse_time(observed_at)
    return {
        "count": len(valid),
        "window_valid": len(valid) == len(bars),
        "latest": latest,
        "latest_age_seconds": (
            (observed - latest_at).total_seconds()
            if observed is not None and latest_at is not None else None
        ),
    }


def compare_source_observations(symbol, primary, secondary, observed_at,
                                primary_name="alpaca", secondary_name="secondary"):
    primary_quote = primary.get("quote", {})
    secondary_quote = secondary.get("quote", {})
    primary_midpoint = _midpoint(primary_quote)
    secondary_midpoint = _midpoint(secondary_quote)
    primary_bars = _bar_window_stats(primary, observed_at)
    secondary_bars = _bar_window_stats(secondary, observed_at)
    return {
        "schema_version": 2,
        "symbol": symbol,
        "observed_at": observed_at,
        "primary_source": primary_name,
        "secondary_source": secondary_name,
        f"{primary_name}_feed": primary_quote.get("feed"),
        f"{secondary_name}_feed": secondary_quote.get("feed"),
        f"{primary_name}_midpoint": primary_midpoint,
        f"{secondary_name}_midpoint": secondary_midpoint,
        "midpoint_difference": (
            secondary_midpoint - primary_midpoint
            if primary_midpoint is not None and secondary_midpoint is not None else None
        ),
        "midpoint_difference_bps": (
            (secondary_midpoint - primary_midpoint) / primary_midpoint * 10_000
            if primary_midpoint and secondary_midpoint is not None else None
        ),
        f"{primary_name}_spread_percent": _spread_percent(primary_quote),
        f"{secondary_name}_spread_percent": _spread_percent(secondary_quote),
        f"{primary_name}_latest_bar": primary_bars["latest"],
        f"{secondary_name}_latest_bar": secondary_bars["latest"],
        f"{primary_name}_bar_count": len(primary.get("bars") or []),
        f"{secondary_name}_bar_count": len(secondary.get("bars") or []),
        f"{primary_name}_bar_count_in_window": primary_bars["count"],
        f"{secondary_name}_bar_count_in_window": secondary_bars["count"],
        f"{primary_name}_bar_window_valid": primary_bars["window_valid"],
        f"{secondary_name}_bar_window_valid": secondary_bars["window_valid"],
        f"{primary_name}_latest_bar_age_seconds": primary_bars["latest_age_seconds"],
        f"{secondary_name}_latest_bar_age_seconds": secondary_bars["latest_age_seconds"],
    }


def append_observation(root, source, payload):
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    path = Path(root) / source / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True) + "\n")
    return path


__all__ = ["append_observation", "compare_source_observations"]
