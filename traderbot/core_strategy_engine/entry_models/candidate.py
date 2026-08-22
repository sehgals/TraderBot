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
):
    active = all(checks.values())
    risk_per_share = limit_price - stop_price
    expected_reward_risk = (
        (target_price - limit_price) / risk_per_share
        if risk_per_share > 0
        else None
    )
    return {
        "model_id": model_id,
        "model_version": 1,
        "legacy_mode": legacy_mode,
        "symbol": features["symbol"],
        "status": "active_signal" if active else "watch",
        "as_of": features["as_of"].isoformat(),
        "last_price": features["bar"]["c"],
        "setup_score": candidate_score(checks),
        "checks": checks,
        "blockers": [name for name, passed in checks.items() if not passed],
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
    "model_config",
    "reward_risk_ok",
    "structural_stop_price",
]
