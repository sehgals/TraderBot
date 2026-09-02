import datetime

from traderbot.core_strategy_engine.entry_models.candidate import (
    build_candidate,
    weighted_factor_score,
)


def features():
    return {
        "symbol": "TEST",
        "as_of": datetime.datetime(2026, 9, 1, 14, tzinfo=datetime.timezone.utc),
        "bar": {"c": 100},
        "relative_dollar_volume": 1.5,
        "market_ok": True,
        "sector_ok": True,
    }


def candidate(checks, threshold=80):
    return build_candidate(
        features=features(),
        model_id="test_model",
        legacy_mode="test_mode",
        checks=checks,
        limit_price=100,
        stop_price=95,
        target_price=110,
        details={},
        minimum_setup_score=threshold,
    )


def test_hard_safety_failure_blocks_even_with_perfect_soft_score():
    result = candidate(
        {
            "model_enabled": True,
            "risk_geometry_valid": True,
            "reward_risk_floor_ok": True,
            "spread_data_available": True,
            "spread_ok": False,
            "market_ok": True,
            "sector_ok": True,
            "trend_ok": True,
        }
    )

    assert result["setup_score"] == 100
    assert result["status"] == "watch"
    assert result["hard_blockers"] == ["spread_ok"]
    assert result["soft_blockers"] == []
    assert result["decision_reasons"] == ["spread_ok"]


def test_soft_failure_reduces_score_without_automatically_blocking():
    result = candidate(
        {
            "model_enabled": True,
            "risk_geometry_valid": True,
            "reward_risk_floor_ok": True,
            "market_ok": False,
            "sector_ok": True,
            "trend_ok": True,
            "volume_ok": True,
            "reward_risk_ok": True,
        }
    )

    assert result["setup_score"] == 80
    assert result["status"] == "active_signal"
    assert result["hard_blockers"] == []
    assert result["soft_blockers"] == ["market_ok"]
    assert result["decision_reasons"] == []


def test_soft_score_below_configured_threshold_blocks_candidate():
    result = candidate(
        {
            "model_enabled": True,
            "risk_geometry_valid": True,
            "reward_risk_floor_ok": True,
            "market_ok": False,
            "sector_ok": False,
            "trend_ok": True,
            "volume_ok": True,
            "reward_risk_ok": True,
        },
        threshold=80,
    )

    assert result["setup_score"] == 60
    assert result["status"] == "watch"
    assert result["hard_blockers"] == []
    assert result["soft_blockers"] == ["market_ok", "sector_ok"]
    assert result["decision_reasons"] == ["setup_score_below_minimum"]


def test_weighted_factor_score_is_normalized_and_auditable():
    score, weights, contributions = weighted_factor_score(
        {"price_action": 80, "stock_trend": 50},
        {"price_action": 75, "stock_trend": 25},
    )

    assert score == 72.5
    assert weights == {"price_action": 75.0, "stock_trend": 25.0}
    assert contributions == {"price_action": 60.0, "stock_trend": 12.5}


def test_candidate_uses_weighted_factors_without_weakening_hard_gates():
    result = build_candidate(
        features=features(), model_id="weighted", legacy_mode="weighted",
        checks={"model_enabled": True, "spread_ok": False, "trend_ok": False},
        limit_price=100, stop_price=95, target_price=110, details={},
        minimum_setup_score=70,
        factor_scores={"price_action": 100, "stock_trend": 50},
        factor_weights={"price_action": 80, "stock_trend": 20},
    )

    assert result["setup_score"] == 90
    assert result["status"] == "watch"
    assert result["hard_blockers"] == ["spread_ok"]
    assert result["factor_contributions"]["price_action"] == 80
