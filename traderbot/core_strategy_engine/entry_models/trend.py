"""Auditable, equally weighted stock-trend components."""

import math


def score_stock_trend(bar, ema_change, atr, minimum_slope=0.20):
    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    slope = ema_change / atr if finite(ema_change) and finite(atr) and atr > 0 else None
    if slope is not None and not finite(slope):
        slope = None
    definitions = [
        ("Price above VWAP", bar.get("c"), bar.get("vwap"), ">"),
        ("Price above EMA9", bar.get("c"), bar.get("ema9"), ">"),
        ("EMA9 above EMA21", bar.get("ema9"), bar.get("ema21"), ">"),
        ("EMA21 at or above EMA50", bar.get("ema21"), bar.get("ema50"), ">="),
        ("EMA21 slope over five bars", slope, minimum_slope, ">="),
    ]
    components = []
    for label, observed, threshold, operator in definitions:
        available = finite(observed) and finite(threshold)
        passed = available and (observed > threshold if operator == ">" else observed >= threshold)
        components.append({"label": label, "observed": observed if finite(observed) else None,
                           "threshold": threshold if finite(threshold) else None,
                           "operator": operator, "passed": passed, "available": available,
                           "unit": "atr" if label.startswith("EMA21 slope") else "price",
                           "score": 20 if passed else 0})
    return {"score": sum(row["score"] for row in components), "components": components,
            "all_passed": all(row["passed"] for row in components)}
