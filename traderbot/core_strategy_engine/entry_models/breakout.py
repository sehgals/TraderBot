import math

from traderbot.core_strategy_engine.entry_models.trend import score_stock_trend
from traderbot.core_strategy_engine.entry_models.candidate import (
    average_check_score,
    bounded_score,
    build_candidate,
    model_config,
    reward_risk_ok,
    score_weights,
    structural_stop_price,
)


def evaluate_breakout(features, config=None):
    config = config or {}
    settings = model_config(config, "breakout")
    bar = features["bar"]
    atr = features["atr14"]
    recent_high = features["recent_high_20"]
    recent_low = features["recent_low_20"]
    minimum_rvol = float(settings.get("minimum_rvol", 1.5))
    minimum_rr = float(
        settings.get(
            "minimum_reward_risk",
            config.get("minimum_entry_reward_risk", 1.5),
        )
    )
    absolute_minimum_rr = float(
        settings.get(
            "absolute_minimum_reward_risk",
            config.get("absolute_minimum_entry_reward_risk", 1.0),
        )
    )
    minimum_setup_score = int(
        settings.get(
            "minimum_setup_score",
            config.get("minimum_entry_setup_score", 80),
        )
    )
    limit_price = recent_high + float(settings.get("limit_buffer_atr", 0.10)) * atr
    stop_price = structural_stop_price(
        {**config, **settings},
        limit_price,
        atr,
        recent_low,
    )
    target_price = limit_price + max(
        float(settings.get("target_atr_multiple", 2.5)) * atr,
        minimum_rr * (limit_price - stop_price),
    )
    breakout_now = bar["c"] > recent_high
    trend = score_stock_trend(bar, features.get("ema21_change_5bars"),
                             features.get("trend_atr14"),
                             float(settings.get("minimum_ema21_slope_atr", 0.20)))
    trend_ok = trend["all_passed"]
    baseline_atr = features.get("previous_atr14")
    full_distance = float(settings.get("full_breakout_score_atr", 0.50))
    max_distance = float(settings.get("maximum_chase_atr", 1.50))
    if not (math.isfinite(full_distance) and math.isfinite(max_distance)
            and 0 < full_distance < max_distance):
        raise ValueError("Breakout ATR thresholds require 0 < full score < maximum chase")
    atr_valid = (isinstance(baseline_atr, (int, float)) and not isinstance(baseline_atr, bool)
                 and math.isfinite(baseline_atr) and baseline_atr > 0)
    distance = (bar["c"] - recent_high) / baseline_atr if atr_valid else None
    atr_valid = atr_valid and math.isfinite(distance)
    if not atr_valid:
        distance = None
    no_chase = atr_valid and distance <= max_distance
    price_score = bounded_score(100 * distance / full_distance) if atr_valid else 0
    extension_score = bounded_score(100 * (max_distance - distance) /
                                    (max_distance - full_distance)) if atr_valid else 0
    checks = {
        **features["entry_filters"]["checks"],
        "model_enabled": settings.get("enabled", True),
        "market_ok": features["market_ok"]
        or not config.get("dynamic_require_market_regime", True),
        "sector_ok": features["sector_ok"]
        or not config.get("dynamic_require_sector_regime", True),
        "above_exit": features["above_exit"],
        "no_same_day_loss_reentry": features["no_same_day_loss_reentry"],
        "breakout_now": breakout_now,
        "breakout_confirmed": breakout_now,
        "breakout_atr_available": atr_valid,
        "breakout_chase_ok": no_chase,
        "trend_ok": trend_ok,
        "volume_ok": features["relative_dollar_volume"] >= minimum_rvol,
        # The ledger is an eligibility ceiling, not an order price.  Clamping a
        # breakout to an old exit-relative cap can manufacture an order, stop,
        # and target far below the live setup while making this check tautological.
        "ledger_price_ok": limit_price <= features["ledger_cap"],
        "risk_geometry_valid": 0 < stop_price < limit_price < target_price,
        "reward_risk_floor_ok": reward_risk_ok(
            limit_price,
            target_price,
            atr,
            minimum=absolute_minimum_rr,
            stop_price=stop_price,
        ),
        "reward_risk_ok": reward_risk_ok(
            limit_price,
            target_price,
            atr,
            minimum=minimum_rr,
            stop_price=stop_price,
        ),
    }
    expected_rr = (
        (target_price - limit_price) / (limit_price - stop_price)
        if limit_price > stop_price else 0
    )
    factor_scores = {
        "price_action": price_score,
        "stock_trend": trend["score"],
        "market_sector_regime": average_check_score(
            checks["market_ok"], checks["sector_ok"]
        ),
        "volume_liquidity_quality": bounded_score(
            100 * features["relative_dollar_volume"] / minimum_rvol
        ),
        "reward_risk_geometry": bounded_score(100 * expected_rr / minimum_rr),
        "relative_strength_execution": extension_score,
    }
    candidate = build_candidate(
        features=features,
        model_id="breakout_continuation",
        legacy_mode="dynamic_breakout_continuation",
        checks=checks,
        limit_price=limit_price,
        stop_price=stop_price,
        target_price=target_price,
        minimum_setup_score=minimum_setup_score,
        factor_scores=factor_scores,
        factor_weights=score_weights(config, settings),
        details={
            "entry_filter_metrics": features["entry_filters"]["metrics"],
            "minimum_reward_risk": minimum_rr,
            "absolute_minimum_reward_risk": absolute_minimum_rr,
            "breakout_trigger": recent_high,
            "next_signal_trigger": recent_high if recent_high >= bar["c"] else None,
        },
    )
    weight = candidate["factor_weights"]["stock_trend"]
    candidate["trend_assessment"] = {
        "version": "component-atr-v2", "as_of": candidate["as_of"],
        "ema21_change_5bars": features.get("ema21_change_5bars"),
        "atr14": features.get("trend_atr14"),
        "points": candidate["factor_contributions"]["stock_trend"], "maximum_points": weight,
        "components": [{**row, "points": round(row["score"] * weight / 100, 4),
                        "maximum_points": weight / 5} for row in trend["components"]],
    }
    candidate["breakout_assessment"] = {
        "as_of": candidate["as_of"], "resistance": recent_high,
        "close": bar["c"], "atr14": baseline_atr if atr_valid else None,
        "distance_atr": distance, "full_score_atr": full_distance,
        "maximum_chase_atr": max_distance,
        "price_action_points": candidate["factor_contributions"]["price_action"],
        "price_action_maximum": candidate["factor_weights"]["price_action"],
        "overextension_points": candidate["factor_contributions"]["relative_strength_execution"],
        "overextension_maximum": candidate["factor_weights"]["relative_strength_execution"],
        "blockers": [key for key in ("breakout_confirmed", "breakout_atr_available", "breakout_chase_ok")
                     if not checks[key]],
    }
    return candidate


__all__ = ["evaluate_breakout"]
