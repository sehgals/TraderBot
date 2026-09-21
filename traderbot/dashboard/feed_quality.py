"""Read-only aggregation of Alpaca/tastytrade comparison observations."""

import json
import statistics
from collections import defaultdict
from pathlib import Path


def _number(value):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _median(values):
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def _grade(score):
    if score >= 90:
        return "Excellent"
    if score >= 80:
        return "Good"
    if score >= 65:
        return "Fair"
    return "Weak"


def _score(symbol, records):
    records = sorted(records, key=lambda row: str(row.get("observed_at") or ""))[-100:]
    primary = str(records[-1].get("primary_source") or "alpaca")
    secondary = str(records[-1].get("secondary_source") or "tastytrade")
    midpoint_bps = _median([
        abs(value) for value in (_number(row.get("midpoint_difference_bps")) for row in records)
        if value is not None
    ])
    primary_bars = sum(_number(row.get(f"{primary}_bar_count")) or 0 for row in records)
    secondary_bars = sum(_number(row.get(f"{secondary}_bar_count")) or 0 for row in records)
    largest_bar_total = max(primary_bars, secondary_bars)
    bar_ratio = min(primary_bars, secondary_bars) / largest_bar_total if largest_bar_total else 0
    primary_spread = _median([_number(row.get(f"{primary}_spread_percent")) for row in records])
    secondary_spread = _median([_number(row.get(f"{secondary}_spread_percent")) for row in records])
    valid_quotes = sum(
        _number(row.get(f"{primary}_midpoint")) is not None
        and _number(row.get(f"{secondary}_midpoint")) is not None
        for row in records
    )
    quote_ratio = valid_quotes / len(records)
    agreement = max(0.0, 1.0 - (midpoint_bps if midpoint_bps is not None else 5.0) / 5.0)
    confidence = min(1.0, len(records) / 20.0)
    score = round(35 * bar_ratio + 30 * agreement + 20 * quote_ratio + 15 * confidence)

    if primary_bars > secondary_bars * 1.01:
        preferred = primary
        reason = "more complete candles"
    elif secondary_bars > primary_bars * 1.01:
        preferred = secondary
        reason = "more complete candles"
    elif primary_spread is not None and secondary_spread is not None:
        if primary_spread < secondary_spread * 0.95:
            preferred, reason = primary, "narrower median spread"
        elif secondary_spread < primary_spread * 0.95:
            preferred, reason = secondary, "narrower median spread"
        else:
            preferred, reason = "comparable", "similar completeness and spreads"
    else:
        preferred, reason = "insufficient data", "spread comparison unavailable"

    return {
        "symbol": symbol,
        "score": score,
        "grade": _grade(score),
        "preferred_source": preferred,
        "reason": reason,
        "samples": len(records),
        "median_midpoint_difference_bps": midpoint_bps,
        "bar_completeness_percent": round(bar_ratio * 100, 2),
        f"{primary}_median_spread_percent": primary_spread,
        f"{secondary}_median_spread_percent": secondary_spread,
        "observed_at": records[-1].get("observed_at"),
    }


def feed_quality_snapshot(root, maximum_days=20):
    directory = Path(root) / "runtime/market_data/comparisons"
    grouped = defaultdict(list)
    for path in sorted(directory.glob("*.jsonl"))[-maximum_days:]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if symbol and row.get("secondary_source") == "tastytrade":
                grouped[symbol].append(row)
    return sorted((_score(symbol, rows) for symbol, rows in grouped.items()),
                  key=lambda row: row["symbol"])


__all__ = ["feed_quality_snapshot"]
