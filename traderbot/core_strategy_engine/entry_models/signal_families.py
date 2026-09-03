"""Phase 7 independently configurable entry signal families."""

import datetime

from traderbot.core_strategy_engine.entry_models.candidate import (
    average_check_score,
    bounded_score,
    build_candidate,
    model_config,
    reward_risk_ok,
    score_weights,
    structural_stop_price,
)


def _return(bars, periods=20):
    if len(bars) <= periods or float(bars[-1 - periods]["c"]) <= 0:
        return None
    return float(bars[-1]["c"]) / float(bars[-1 - periods]["c"]) - 1


def _safety_checks(features, settings):
    return {
        **features["entry_filters"]["checks"],
        "model_enabled": settings.get("enabled", False),
        "above_exit": features["above_exit"],
        "no_same_day_loss_reentry": features["no_same_day_loss_reentry"],
    }


def _target_price(limit, stop, atr, settings, config, default_multiple):
    preferred = float(settings.get(
        "minimum_reward_risk", config.get("minimum_entry_reward_risk", 1.5)
    ))
    return limit + max(
        float(settings.get("target_atr_multiple", default_multiple)) * atr,
        preferred * max(0.01, limit - stop),
    )


def _build(features, config, family, model_id, checks, limit, stop, target, factors, details=None):
    settings = model_config(config, family)
    absolute_floor = float(settings.get(
        "absolute_minimum_reward_risk",
        config.get("absolute_minimum_entry_reward_risk", 1.0),
    ))
    preferred = float(settings.get(
        "minimum_reward_risk", config.get("minimum_entry_reward_risk", 1.5)
    ))
    checks.update({
        "ledger_price_ok": limit <= features["ledger_cap"],
        "risk_geometry_valid": 0 < stop < limit < target,
        "reward_risk_floor_ok": reward_risk_ok(
            limit, target, features["atr14"], absolute_floor, stop
        ),
        "reward_risk_ok": reward_risk_ok(
            limit, target, features["atr14"], preferred, stop
        ),
    })
    return build_candidate(
        features=features,
        model_id=model_id,
        legacy_mode=f"dynamic_{model_id}",
        checks=checks,
        limit_price=limit,
        stop_price=stop,
        target_price=target,
        minimum_setup_score=int(settings.get(
            "minimum_setup_score", config.get("minimum_entry_setup_score", 80)
        )),
        factor_scores=factors,
        factor_weights=score_weights(config, settings),
        details={
            "entry_filter_metrics": features["entry_filters"]["metrics"],
            "minimum_reward_risk": preferred,
            "absolute_minimum_reward_risk": absolute_floor,
            "signal_family": family,
            "risk_budget_percent": float(settings.get("risk_budget_percent", 0.25)),
            **(details or {}),
        },
    )


def evaluate_relative_strength(features, config):
    settings = model_config(config, "relative_strength")
    bar, atr = features["bar"], features["atr14"]
    stock_return = _return(features["bars"])
    benchmark_return = _return(features["sector_bars"])
    relative = (
        stock_return - benchmark_return
        if stock_return is not None and benchmark_return is not None else None
    )
    trend = bar["c"] > bar["ema9"] > bar["ema21"] >= bar["ema50"]
    continuation = bar["c"] >= features["recent_high_20"] - 0.5 * atr
    rs_ok = relative is not None and relative >= float(settings.get("minimum_relative_return", 0.02))
    limit = float(bar["c"])
    stop = structural_stop_price({**config, **settings}, limit, atr, features["recent_low_20"])
    target = _target_price(limit, stop, atr, settings, config, 2.5)
    checks = {**_safety_checks(features, settings), "relative_strength_ok": rs_ok,
              "trend_ok": trend, "continuation_ok": continuation,
              "market_ok": features["market_ok"], "sector_ok": features["sector_ok"]}
    return _build(features, config, "relative_strength", "relative_strength_continuation",
                  checks, limit, stop, target, {
                      "price_action": average_check_score(continuation),
                      "stock_trend": average_check_score(trend),
                      "market_sector_regime": average_check_score(features["market_ok"], features["sector_ok"]),
                      "volume_liquidity_quality": bounded_score(100 * features["relative_dollar_volume"]),
                      "reward_risk_geometry": 100,
                      "relative_strength_execution": bounded_score(50 + 1000 * (relative or 0)),
                  }, {"relative_return_20bars": relative})


def evaluate_low_volatility_trend(features, config):
    settings = model_config(config, "low_volatility_trend")
    bar, atr = features["bar"], features["atr14"]
    atr_percent = atr / float(bar["c"]) * 100
    low_vol = atr_percent <= float(settings.get("maximum_atr_percent", 2.0))
    trend = bar["c"] > bar["ema21"] >= bar["ema50"] and features["ema21_slope_5bars"] >= 0
    limit = float(bar["c"])
    stop = structural_stop_price({**config, **settings}, limit, atr, features["recent_low_20"])
    target = _target_price(limit, stop, atr, settings, config, 2.0)
    checks = {**_safety_checks(features, settings), "low_volatility_ok": low_vol,
              "trend_ok": trend, "market_ok": features["market_ok"], "sector_ok": features["sector_ok"]}
    return _build(features, config, "low_volatility_trend", "low_volatility_trend",
                  checks, limit, stop, target, {
                      "price_action": average_check_score(bar["c"] > bar["ema21"]),
                      "stock_trend": average_check_score(trend),
                      "market_sector_regime": average_check_score(features["market_ok"], features["sector_ok"]),
                      "volume_liquidity_quality": bounded_score(100 * features["relative_dollar_volume"]),
                      "reward_risk_geometry": 100,
                      "relative_strength_execution": bounded_score(100 * (1 - atr_percent / 4)),
                  }, {"atr_percent": atr_percent})


def _latest_positive_earnings(features, settings):
    calendar = features.get("event_calendar") or {}
    coverage = calendar.get("coverage") or {}
    covered = bool(coverage.get("point_in_time"))
    latest = None
    for event in calendar.get("events", []):
        if str(event.get("symbol", "")).upper() != features["symbol"].upper() or str(event.get("type", "")).lower() != "earnings":
            continue
        known_at = event.get("known_at")
        if not known_at:
            continue
        known = datetime.datetime.fromisoformat(str(known_at).replace("Z", "+00:00"))
        if known.tzinfo is None:
            known = known.replace(tzinfo=datetime.timezone.utc)
        event_day = datetime.date.fromisoformat(event["date"])
        age = (features["as_of"].date() - event_day).days
        if known <= features["as_of"] and 1 <= age <= int(settings.get("maximum_days_after_earnings", 10)):
            latest = event
    surprise = latest.get("surprise_percent") if latest else None
    positive = surprise is not None and float(surprise) >= float(settings.get("minimum_surprise_percent", 2.0))
    return covered and latest is not None and surprise is not None, positive, latest


def evaluate_post_earnings_drift(features, config):
    settings = model_config(config, "post_earnings_drift")
    bar, atr = features["bar"], features["atr14"]
    available, positive, event = _latest_positive_earnings(features, settings)
    drift = bar["c"] > bar["ema9"] > bar["ema21"] and features["ema21_slope_5bars"] > 0
    limit = float(bar["c"])
    stop = structural_stop_price({**config, **settings}, limit, atr, features["recent_low_20"])
    target = _target_price(limit, stop, atr, settings, config, 3.0)
    checks = {**_safety_checks(features, settings),
              "post_earnings_data_available": available,
              "positive_earnings_surprise": positive, "post_earnings_drift_ok": drift,
              "market_ok": features["market_ok"], "sector_ok": features["sector_ok"]}
    return _build(features, config, "post_earnings_drift", "post_earnings_drift",
                  checks, limit, stop, target, {
                      "price_action": average_check_score(drift), "stock_trend": average_check_score(drift),
                      "market_sector_regime": average_check_score(features["market_ok"], features["sector_ok"]),
                      "volume_liquidity_quality": bounded_score(100 * features["relative_dollar_volume"]),
                      "reward_risk_geometry": 100,
                      "relative_strength_execution": bounded_score(50 + 5 * float((event or {}).get("surprise_percent") or 0)),
                  }, {"earnings_event": event})


def evaluate_trend_mean_reversion(features, config):
    settings = model_config(config, "trend_mean_reversion")
    bar, previous, atr = features["bar"], features["previous_bar"], features["atr14"]
    bars = features["bars"]
    long_trend = bar["ema50"] > bars[-21]["ema50"] and bar["c"] > bar["ema50"]
    pullback = bar["c"] <= bar["ema21"] and bar["c"] >= bar["ema21"] - float(settings.get("maximum_pullback_atr", 1.5)) * atr
    reversal = bar["c"] > previous["c"]
    limit = float(bar["c"])
    stop = structural_stop_price({**config, **settings}, limit, atr, features["recent_low_20"])
    target = max(
        _target_price(limit, stop, atr, settings, config, 2.0),
        float(bar["ema21"]), features["recent_high_20"],
    )
    checks = {**_safety_checks(features, settings), "long_term_trend_ok": long_trend,
              "mean_reversion_pullback_ok": pullback, "reversal_ok": reversal,
              "market_ok": features["market_ok"], "sector_ok": features["sector_ok"]}
    return _build(features, config, "trend_mean_reversion", "trend_mean_reversion_pullback",
                  checks, limit, stop, target, {
                      "price_action": average_check_score(pullback, reversal), "stock_trend": average_check_score(long_trend),
                      "market_sector_regime": average_check_score(features["market_ok"], features["sector_ok"]),
                      "volume_liquidity_quality": bounded_score(100 * features["relative_dollar_volume"]),
                      "reward_risk_geometry": 100, "relative_strength_execution": average_check_score(reversal),
                  })


def evaluate_defensive_etf(features, config):
    settings = model_config(config, "defensive_etf")
    bar, atr = features["bar"], features["atr14"]
    allowed = features["symbol"] in set(settings.get("symbols", ["SPY", "QQQ", "VTI", "USMV"]))
    trend = bar["c"] > bar["ema21"] and features["ema21_slope_5bars"] >= 0
    limit = float(bar["c"])
    stop = structural_stop_price({**config, **settings}, limit, atr, features["recent_low_20"])
    target = _target_price(limit, stop, atr, settings, config, 2.0)
    checks = {**_safety_checks(features, settings), "eligible_defensive_etf": allowed,
              "trend_ok": trend, "market_ok": True, "sector_ok": True}
    return _build(features, config, "defensive_etf", "defensive_broad_market_etf",
                  checks, limit, stop, target, {
                      "price_action": average_check_score(trend), "stock_trend": average_check_score(trend),
                      "market_sector_regime": 100, "volume_liquidity_quality": bounded_score(100 * features["relative_dollar_volume"]),
                      "reward_risk_geometry": 100, "relative_strength_execution": 100,
                  }, {"fallback_only": True})


def evaluate_signal_families(features, config):
    return [
        evaluate_relative_strength(features, config),
        evaluate_low_volatility_trend(features, config),
        evaluate_post_earnings_drift(features, config),
        evaluate_trend_mean_reversion(features, config),
        evaluate_defensive_etf(features, config),
    ]


__all__ = ["evaluate_signal_families"]
