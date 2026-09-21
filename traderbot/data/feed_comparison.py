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


def compare_source_observations(symbol, primary, secondary, observed_at,
                                primary_name="alpaca", secondary_name="secondary"):
    primary_quote = primary.get("quote", {})
    secondary_quote = secondary.get("quote", {})
    primary_midpoint = _midpoint(primary_quote)
    secondary_midpoint = _midpoint(secondary_quote)
    latest_primary = (primary.get("bars") or [{}])[-1]
    latest_secondary = (secondary.get("bars") or [{}])[-1]
    return {
        "schema_version": 1,
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
        f"{primary_name}_latest_bar": latest_primary or None,
        f"{secondary_name}_latest_bar": latest_secondary or None,
        f"{primary_name}_bar_count": len(primary.get("bars") or []),
        f"{secondary_name}_bar_count": len(secondary.get("bars") or []),
    }


def append_observation(root, source, payload):
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    path = Path(root) / source / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True) + "\n")
    return path


__all__ = ["append_observation", "compare_source_observations"]
