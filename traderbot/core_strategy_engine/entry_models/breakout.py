from traderbot.core_strategy_engine.entry_models.candidate import (
    build_candidate,
    model_config,
    reward_risk_ok,
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
    trend_ok = (
        bar["c"] > bar["vwap"]
        and bar["c"] > bar["ema9"] > bar["ema21"]
        and bar["ema21"] >= bar["ema50"]
        and features["ema21_slope_5bars"]
        >= float(settings.get("minimum_ema21_slope", 0.002))
    )
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
        "trend_ok": trend_ok,
        "volume_ok": features["relative_dollar_volume"] >= minimum_rvol,
        # The ledger is an eligibility ceiling, not an order price.  Clamping a
        # breakout to an old exit-relative cap can manufacture an order, stop,
        # and target far below the live setup while making this check tautological.
        "ledger_price_ok": limit_price <= features["ledger_cap"],
        "reward_risk_ok": reward_risk_ok(
            limit_price,
            target_price,
            atr,
            minimum=minimum_rr,
            stop_price=stop_price,
        ),
    }
    return build_candidate(
        features=features,
        model_id="breakout_continuation",
        legacy_mode="dynamic_breakout_continuation",
        checks=checks,
        limit_price=limit_price,
        stop_price=stop_price,
        target_price=target_price,
        details={
            "entry_filter_metrics": features["entry_filters"]["metrics"],
            "minimum_reward_risk": minimum_rr,
            "breakout_trigger": recent_high,
            "next_signal_trigger": recent_high if recent_high >= bar["c"] else None,
        },
    )


__all__ = ["evaluate_breakout"]
