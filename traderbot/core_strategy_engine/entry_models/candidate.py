HARD_ENTRY_CHECKS = frozenset(
    {
        "above_exit",
        "corporate_actions_data_available",
        "corporate_actions_ok",
        "earnings_data_available",
        "earnings_ok",
        "gap_data_available",
        "gap_ok",
        "ledger_price_ok",
        "liquidity_data_available",
        "liquidity_ok",
        "model_enabled",
        "no_same_day_loss_reentry",
        "reward_risk_floor_ok",
        "risk_geometry_valid",
        "spread_data_available",
        "spread_ok",
    }
)

DEFAULT_ENTRY_SCORE_WEIGHTS = {
    "price_action": 25.0,
    "stock_trend": 20.0,
    "market_sector_regime": 15.0,
    "volume_liquidity_quality": 15.0,
    "reward_risk_geometry": 15.0,
    "relative_strength_execution": 10.0,
}


def model_config(config, model_name):
    return dict(((config.get("entry_models") or {}).get(model_name) or {}))


def reward_risk_ok(entry_price, target_price, atr, minimum=1.5, stop_price=None):
    stop_price = (
        float(stop_price)
        if stop_price not in (None, "")
        else max(entry_price * 0.98, entry_price - atr)
    )
    risk = entry_price - stop_price
    reward = target_price - entry_price
    return risk > 0 and reward / risk >= minimum


def structural_stop_price(config, entry_price, atr, setup_low):
    """Risk stop shared by signal qualification, sizing, and management."""
    atr_multiple = float(config.get("structural_stop_atr_multiple", 1.5))
    buffer_atr = float(config.get("structural_stop_buffer_atr", 0.1))
    max_loss_percent = float(config.get("structural_stop_max_percent", 6.0))
    raw_stop = min(
        entry_price - atr_multiple * atr,
        float(setup_low) - buffer_atr * atr,
    )
    capped_stop = max(raw_stop, entry_price * (1 - max_loss_percent / 100))
    return min(capped_stop, entry_price - max(0.01, 0.1 * atr))


def candidate_score(checks):
    if not checks:
        return 0
    return round(sum(1 for passed in checks.values() if passed) / len(checks) * 100)


def bounded_score(value):
    return round(max(0.0, min(100.0, float(value))), 2)


def average_check_score(*values):
    available = [bool(value) for value in values if value is not None]
    return bounded_score(100 * sum(available) / len(available)) if available else None


def weighted_factor_score(factor_scores, weights=None):
    """Return a 0-100 score and auditable normalized contributions."""
    scores = {
        name: bounded_score(value)
        for name, value in (factor_scores or {}).items()
        if value is not None
    }
    requested = {**DEFAULT_ENTRY_SCORE_WEIGHTS, **(weights or {})}
    positive = {name: max(0.0, float(requested.get(name, 0))) for name in scores}
    total = sum(positive.values())
    if not scores or total <= 0:
        return None, {}, {}
    normalized = {name: round(weight / total * 100, 4) for name, weight in positive.items()}
    contributions = {
        name: round(scores[name] * normalized[name] / 100, 4) for name in scores
    }
    return round(sum(contributions.values()), 2), normalized, contributions


def score_weights(config, settings):
    return {
        **DEFAULT_ENTRY_SCORE_WEIGHTS,
        **(config.get("entry_score_weights") or {}),
        **(settings.get("score_weights") or {}),
    }


def classify_entry_checks(checks):
    hard_checks = {
        name: bool(passed)
        for name, passed in checks.items()
        if name in HARD_ENTRY_CHECKS
    }
    soft_checks = {
        name: bool(passed)
        for name, passed in checks.items()
        if name not in HARD_ENTRY_CHECKS
    }
    return hard_checks, soft_checks


def build_candidate(
    *,
    features,
    model_id,
    legacy_mode,
    checks,
    limit_price,
    stop_price,
    target_price,
    details,
    minimum_setup_score=80,
    factor_scores=None,
    factor_weights=None,
):
    risk_per_share = limit_price - stop_price
    expected_reward_risk = (
        (target_price - limit_price) / risk_per_share
        if risk_per_share > 0
        else None
    )
    hard_checks, soft_checks = classify_entry_checks(checks)
    hard_blockers = [name for name, passed in hard_checks.items() if not passed]
    soft_blockers = [name for name, passed in soft_checks.items() if not passed]
    soft_check_score = candidate_score(soft_checks)
    weighted_score, normalized_weights, contributions = weighted_factor_score(
        factor_scores, factor_weights
    )
    setup_score = soft_check_score if weighted_score is None else weighted_score
    score_ok = setup_score >= int(minimum_setup_score)
    active = not hard_blockers and score_ok
    decision_reasons = list(hard_blockers)
    if not score_ok:
        decision_reasons.append("setup_score_below_minimum")
    return {
        "model_id": model_id,
        "model_version": 2,
        "legacy_mode": legacy_mode,
        "symbol": features["symbol"],
        "status": "active_signal" if active else "watch",
        "as_of": features["as_of"].isoformat(),
        "last_price": features["bar"]["c"],
        "setup_score": setup_score,
        "soft_check_score": soft_check_score,
        "factor_scores": {
            name: bounded_score(value)
            for name, value in (factor_scores or {}).items()
            if value is not None
        },
        "factor_weights": normalized_weights,
        "factor_contributions": contributions,
        "overall_check_score": candidate_score(checks),
        "minimum_setup_score": int(minimum_setup_score),
        "setup_score_meets_threshold": score_ok,
        "checks": checks,
        "hard_checks": hard_checks,
        "soft_checks": soft_checks,
        "blockers": [name for name, passed in checks.items() if not passed],
        "hard_blockers": hard_blockers,
        "soft_blockers": soft_blockers,
        "decision_reasons": decision_reasons,
        "qualification_policy": "hard_safety_plus_weighted_factor_score_v2",
        "limit_price": limit_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "risk_per_share": risk_per_share,
        "expected_reward_risk": expected_reward_risk,
        "relative_dollar_volume": features["relative_dollar_volume"],
        "market_ok": features["market_ok"],
        "sector_ok": features["sector_ok"],
        **details,
    }


__all__ = [
    "build_candidate",
    "candidate_score",
    "average_check_score",
    "bounded_score",
    "classify_entry_checks",
    "DEFAULT_ENTRY_SCORE_WEIGHTS",
    "HARD_ENTRY_CHECKS",
    "model_config",
    "reward_risk_ok",
    "score_weights",
    "structural_stop_price",
    "weighted_factor_score",
]
