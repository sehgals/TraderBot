"""Deterministic portfolio ranking and constrained entry allocation."""

import math


DEFAULT_CONTROLS = {
    "maximum_simultaneous_positions": 12,
    "maximum_aggregate_open_risk_percent": 6.0,
    "maximum_sector_exposure_percent": 30.0,
    "maximum_correlated_factor_exposure_percent": 35.0,
    "maximum_symbol_notional_percent": 15.0,
    "maximum_daily_entries": 4,
    "maximum_daily_turnover_percent": 25.0,
    "minimum_cash_reserve_percent": 20.0,
    "minimum_score": 80.0,
    "minimum_score_separation": 0.0,
    "sector_penalty": 0.25,
    "correlated_factor_penalty": 0.15,
    "regime_exposure_bands": {
        "defensive": {"minimum_percent": 0.0, "maximum_percent": 25.0},
        "neutral": {"minimum_percent": 25.0, "maximum_percent": 55.0},
        "favorable": {"minimum_percent": 50.0, "maximum_percent": 80.0},
    },
}


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _candidate_value(candidate, sector_exposure, factor_exposure, equity, controls):
    score = _number(candidate.get("score"))
    reward_risk = max(0.0, _number(candidate.get("expected_reward_risk")))
    base = score * reward_risk
    sector_ratio = sector_exposure.get(candidate["sector"], 0.0) / equity if equity else 1
    factor_ratio = factor_exposure.get(candidate["factor"], 0.0) / equity if equity else 1
    penalty = (
        100 * _number(controls["sector_penalty"]) * sector_ratio
        + 100 * _number(controls["correlated_factor_penalty"]) * factor_ratio
    )
    return round(base - penalty, 6)


def classify_exposure_regime(market_bars, timestamp=None):
    """Classify only from bars available at the supplied decision timestamp."""
    available = [
        bar for bar in (market_bars or [])
        if timestamp is None or bar.get("t") <= timestamp
    ]
    if len(available) < 6:
        return "defensive"
    bar = available[-1]
    previous = available[-6]
    ema21 = _number(bar.get("ema21"))
    ema50 = _number(bar.get("ema50"))
    previous_ema21 = _number(previous.get("ema21"))
    close = _number(bar.get("c"))
    slope = (ema21 - previous_ema21) / previous_ema21 if previous_ema21 > 0 else 0
    if close > ema21 > ema50 and slope >= 0:
        return "favorable"
    if close < ema21 < ema50 and slope < 0:
        return "defensive"
    return "neutral"


def regime_exposure_band(regime, settings=None):
    controls = {**DEFAULT_CONTROLS, **(settings or {})}
    bands = controls["regime_exposure_bands"]
    selected = bands.get(regime) or bands.get("defensive") or {}
    minimum = max(0.0, _number(selected.get("minimum_percent")))
    maximum = max(minimum, _number(selected.get("maximum_percent"), minimum))
    return {"minimum_percent": minimum, "maximum_percent": maximum}


def allocate_candidates(candidates, portfolio, settings=None):
    """Rank a completed-bar snapshot and allocate without exceeding any limit.

    Candidates must already have passed strategy hard gates. Rejections are
    returned with explicit reasons so the same input always yields the same audit.
    """
    controls = {**DEFAULT_CONTROLS, **(settings or {})}
    equity = max(0.0, _number(portfolio.get("equity")))
    cash = max(0.0, _number(portfolio.get("cash")))
    positions = int(portfolio.get("position_count") or 0)
    aggregate_risk = max(0.0, _number(portfolio.get("aggregate_open_risk")))
    daily_entries = int(portfolio.get("daily_entries") or 0)
    daily_turnover = max(0.0, _number(portfolio.get("daily_turnover")))
    symbol_exposure = dict(portfolio.get("symbol_exposure") or {})
    sector_exposure = dict(portfolio.get("sector_exposure") or {})
    factor_exposure = dict(portfolio.get("factor_exposure") or {})
    regime = str(portfolio.get("regime") or "defensive")
    exposure_band = regime_exposure_band(regime, controls)
    gross_exposure = max(0.0, _number(portfolio.get("gross_exposure")))
    reserve = equity * _number(controls["minimum_cash_reserve_percent"]) / 100
    ranked = []
    rejected = []

    for raw in candidates or []:
        item = dict(raw)
        item["symbol"] = str(item.get("symbol") or "").upper()
        item["sector"] = str(item.get("sector") or "Unmapped")
        item["factor"] = str(item.get("factor") or item["sector"])
        item["score"] = _number(item.get("score"))
        item["notional"] = max(0.0, _number(item.get("notional")))
        item["risk_dollars"] = max(0.0, _number(item.get("risk_dollars")))
        if not item["symbol"] or not item.get("hard_safety_passed", False):
            rejected.append({**item, "reason": "hard_safety_not_passed"})
            continue
        if item["score"] < _number(controls["minimum_score"]):
            rejected.append({**item, "reason": "score_below_minimum"})
            continue
        item["rank_value"] = _candidate_value(
            item, sector_exposure, factor_exposure, equity, controls
        )
        ranked.append(item)

    ranked.sort(
        key=lambda item: (
            -item["rank_value"], -item["score"],
            -_number(item.get("expected_reward_risk")), item["symbol"],
        )
    )
    selected = []
    for index, item in enumerate(ranked):
        reasons = []
        notional = item["notional"]
        risk = item["risk_dollars"]
        if positions + len(selected) >= int(controls["maximum_simultaneous_positions"]):
            reasons.append("maximum_simultaneous_positions")
        if daily_entries + len(selected) >= int(controls["maximum_daily_entries"]):
            reasons.append("maximum_daily_entries")
        if equity <= 0 or aggregate_risk + sum(x["risk_dollars"] for x in selected) + risk > (
            equity * _number(controls["maximum_aggregate_open_risk_percent"]) / 100
        ):
            reasons.append("maximum_aggregate_open_risk")
        if symbol_exposure.get(item["symbol"], 0.0) + notional > (
            equity * _number(controls["maximum_symbol_notional_percent"]) / 100
        ):
            reasons.append("maximum_symbol_notional")
        selected_sector = sum(x["notional"] for x in selected if x["sector"] == item["sector"])
        if sector_exposure.get(item["sector"], 0.0) + selected_sector + notional > (
            equity * _number(controls["maximum_sector_exposure_percent"]) / 100
        ):
            reasons.append("maximum_sector_exposure")
        selected_factor = sum(x["notional"] for x in selected if x["factor"] == item["factor"])
        if factor_exposure.get(item["factor"], 0.0) + selected_factor + notional > (
            equity * _number(controls["maximum_correlated_factor_exposure_percent"]) / 100
        ):
            reasons.append("maximum_correlated_factor_exposure")
        if daily_turnover + sum(x["notional"] for x in selected) + notional > (
            equity * _number(controls["maximum_daily_turnover_percent"]) / 100
        ):
            reasons.append("maximum_daily_turnover")
        if cash - sum(x["notional"] for x in selected) - notional < reserve:
            reasons.append("minimum_cash_reserve")
        if gross_exposure + sum(x["notional"] for x in selected) + notional > (
            equity * exposure_band["maximum_percent"] / 100
        ):
            reasons.append("regime_exposure_ceiling")
        separation = _number(controls["minimum_score_separation"])
        previous_score = ranked[index - 1]["score"] if index > 0 else None
        next_score = ranked[index + 1]["score"] if index + 1 < len(ranked) else None
        insufficient_separation = (
            (next_score is not None and item["score"] - next_score < separation)
            or (previous_score is not None and previous_score - item["score"] < separation)
        )
        if separation > 0 and insufficient_separation:
            reasons.append("minimum_score_separation")
        if reasons:
            rejected.append({**item, "reason": reasons[0], "reasons": reasons})
        else:
            selected.append(item)

    allocated_notional = sum(item["notional"] for item in selected)
    return {
        "as_of": portfolio.get("as_of"),
        "regime": regime,
        "exposure_band": exposure_band,
        "gross_exposure_before_percent": round(gross_exposure / equity * 100, 4)
        if equity else None,
        "gross_exposure_after_percent": round(
            (gross_exposure + allocated_notional) / equity * 100, 4
        ) if equity else None,
        "below_target_band": bool(
            equity and gross_exposure + allocated_notional
            < equity * exposure_band["minimum_percent"] / 100
        ),
        "controls": controls,
        "ranked": ranked,
        "selected": selected,
        "rejected": sorted(rejected, key=lambda item: (item.get("symbol", ""), item["reason"])),
    }


__all__ = [
    "DEFAULT_CONTROLS", "allocate_candidates", "classify_exposure_regime",
    "regime_exposure_band",
]
