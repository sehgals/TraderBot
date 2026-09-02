from traderbot.core_strategy_engine.entry_models.candidate import (
    average_check_score,
    bounded_score,
    build_candidate,
    model_config,
    reward_risk_ok,
    score_weights,
    structural_stop_price,
)


def evaluate_pullback(features, config=None):
    config = config or {}
    settings = model_config(config, "pullback")
    bar = features["bar"]
    previous = features["previous_bar"]
    atr = features["atr14"]
    recent_high = features["recent_high_20"]
    recent_low = features["recent_low_20"]
    minimum_rvol = float(settings.get("minimum_rvol", 1.15))
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
    pullback_zone = max(
        bar["ema21"],
        bar["vwap"],
        recent_low + 0.382 * (recent_high - recent_low),
    )
    touch_trigger = pullback_zone * 1.005
    reclaim_trigger = max(bar["ema9"], bar["vwap"], previous["c"])
    limit_price = pullback_zone + float(settings.get("limit_buffer_atr", 0.10)) * atr
    stop_price = structural_stop_price(
        {**config, **settings},
        limit_price,
        atr,
        min(recent_low, previous["l"]),
    )
    target_price = recent_high
    trend_base = (
        bar["c"] > bar["vwap"]
        and bar["c"] > bar["ema21"]
        and bar["ema21"] >= bar["ema50"]
    )
    no_chase = bar["c"] <= bar["ema21"] + float(
        settings.get("maximum_chase_atr", 0.75)
    ) * atr
    touched_pullback = previous["l"] <= touch_trigger
    reclaimed = bar["c"] > reclaim_trigger
    vwap_stability = sum(
        1
        for item in features["recent_three_bars"]
        if item["c"] > item["vwap"]
    ) >= int(settings.get("minimum_vwap_stable_bars", 2))
    checks = {
        **features["entry_filters"]["checks"],
        "model_enabled": settings.get("enabled", True),
        "market_ok": features["market_ok"]
        or not config.get("dynamic_require_market_regime", True),
        "sector_ok": features["sector_ok"]
        or not config.get("dynamic_require_sector_regime", True),
        "above_exit": features["above_exit"],
        "no_same_day_loss_reentry": features["no_same_day_loss_reentry"],
        "touched_pullback": touched_pullback,
        "reclaimed": reclaimed,
        "trend_ok": trend_base,
        "ema21_slope_ok": features["ema21_slope_5bars"] >= float(
            settings.get("minimum_ema21_slope", 0.002)
        ),
        "no_chase": no_chase,
        "vwap_stability": vwap_stability,
        "volume_ok": features["relative_dollar_volume"] >= minimum_rvol,
        # Preserve the setup-derived price and use the ledger only to decide
        # eligibility.  Repricing to the cap distorts downstream risk levels.
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
        "price_action": average_check_score(touched_pullback, reclaimed),
        "stock_trend": average_check_score(
            trend_base, checks["ema21_slope_ok"]
        ),
        "market_sector_regime": average_check_score(
            checks["market_ok"], checks["sector_ok"]
        ),
        "volume_liquidity_quality": bounded_score(
            100 * features["relative_dollar_volume"] / minimum_rvol
        ),
        "reward_risk_geometry": bounded_score(100 * expected_rr / minimum_rr),
        "relative_strength_execution": average_check_score(
            no_chase, vwap_stability
        ),
    }
    return build_candidate(
        features=features,
        model_id="pullback_reclaim",
        legacy_mode="dynamic_pullback_reclaim",
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
            "pullback_zone": pullback_zone,
            "touch_trigger": touch_trigger,
            "reclaim_trigger": reclaim_trigger,
            "next_signal_trigger": reclaim_trigger if reclaim_trigger >= bar["c"] else None,
        },
    )


__all__ = ["evaluate_pullback"]
