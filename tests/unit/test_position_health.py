import datetime

from traderbot.core_strategy_engine.position_health import (
    context_is_fresh,
    downside_score,
    evaluate_add_eligibility,
    evaluate_position_health,
    reward_risk_score,
)


NOW = datetime.datetime(2026, 8, 11, 16, 0, tzinfo=datetime.timezone.utc)


def context(**overrides):
    result = {
        "as_of": "2026-08-11T15:00:00+00:00",
        "bar_id": "WAT:1Hour:2026-08-11T15:00:00+00:00",
        "timeframe_minutes": 60,
        "latest_bar": {
            "c": 105,
            "ema9": 103,
            "ema21": 101,
            "ema50": 98,
            "ema21_slope": 0.01,
        },
        "relative_strength_5d": 0.03,
        "market_ok": True,
    }
    result.update(overrides)
    return result


def position(current=105, qty=10):
    return {
        "symbol": "WAT",
        "qty": str(qty),
        "avg_entry_price": "100",
        "current_price": str(current),
    }


def episode(target=112):
    return {
        "symbol": "WAT",
        "episode_id": "episode-1",
        "average_entry_price": 100,
        "original_target_price": target,
    }


def protection(stop=96, qty=10):
    return {"stop_price": stop, "stop_qty": qty}


def test_downside_score_uses_policy_boundaries():
    assert downside_score(1) == 100
    assert downside_score(-3) == 70
    assert downside_score(-6) == 30
    assert downside_score(-8) == 0


def test_healthy_position_scores_components_and_holds():
    result = evaluate_position_health(
        position(), episode(), context(), protection(), now=NOW
    )

    assert result["state"] == "Healthy"
    assert result["recommended_action"] == "hold"
    assert result["score"] >= 80
    assert result["downside_score"] == 100
    assert result["trend_score"] == 100
    assert result["data_fresh"] is True


def test_six_percent_loss_is_hard_reduction_override():
    weak_context = context(
        latest_bar={
            "c": 94,
            "ema9": 95,
            "ema21": 96,
            "ema50": 97,
            "ema21_slope": -0.01,
        },
        relative_strength_5d=-0.05,
        market_ok=False,
    )
    result = evaluate_position_health(
        position(current=94), episode(), weak_context, protection(stop=92), now=NOW
    )

    assert result["state"] == "At Risk"
    assert result["recommended_action"] == "reduce"
    assert "hard_reduction_threshold" in result["reasons"]


def test_eight_percent_loss_is_exit_override():
    result = evaluate_position_health(
        position(current=92), episode(), context(), protection(stop=92), now=NOW
    )

    assert result["state"] == "Critical"
    assert result["recommended_action"] == "exit"
    assert "catastrophic_loss_threshold" in result["reasons"]


def test_incomplete_stop_coverage_is_unprotected():
    result = evaluate_position_health(
        position(), episode(), context(), protection(qty=5), now=NOW
    )

    assert result["state"] == "Unprotected"
    assert result["recommended_action"] == "restore_protection"
    assert "stop_coverage_incomplete" in result["reasons"]


def test_stale_data_freezes_discretionary_actions():
    stale = context(as_of="2026-08-11T10:00:00+00:00")
    result = evaluate_position_health(
        position(), episode(), stale, protection(), now=NOW
    )

    assert result["state"] == "Unavailable"
    assert result["recommended_action"] == "freeze"
    assert "health_data_stale" in result["reasons"]


def test_structural_trend_failure_caps_health_at_at_risk():
    broken = context(
        latest_bar={
            "c": 105,
            "ema9": 108,
            "ema21": 110,
            "ema50": 107,
            "ema21_slope": -0.01,
        },
        relative_strength_5d=0.02,
        market_ok=True,
    )
    result = evaluate_position_health(
        position(), episode(target=125), broken, protection(), now=NOW
    )

    assert result["score"] <= 44
    assert result["state"] == "At Risk"
    assert "structural_trend_failure" in result["reasons"]


def test_target_reached_with_profit_protected_has_full_reward_risk_score():
    score, remaining_r = reward_risk_score(112, 110, 105, 100)

    assert score == 100
    assert remaining_r == float("inf")


def test_context_freshness_accepts_two_bar_window():
    assert context_is_fresh(
        "2026-08-11T14:00:00+00:00", 60, 2, now=NOW
    )
    assert not context_is_fresh(
        "2026-08-11T13:00:00+00:00", 60, 2, now=NOW
    )


def test_add_eligibility_requires_profitable_healthy_position_and_both_scores():
    health = {
        "score": 90,
        "remaining_r": 2.0,
        "components": {
            "trend_checks": {
                "price_above_ema21": True,
                "price_above_ema50": True,
                "ema21_slope_positive": True,
                "market_regime_favorable": True,
            }
        },
    }
    result = evaluate_add_eligibility(
        health,
        {"score": 90, "status": "active_signal"},
        {"initial_qty": 10, "add_count": 0, "adverse_reduction_completed": False},
        {
            "qty": 10,
            "avg_entry_price": 100,
            "current_price": 105,
            "market_value": 1050,
        },
        {"stop_price": 102, "stop_qty": 10},
        50000,
        {"additions_enabled": True},
    )

    assert result["eligible"] is True
    assert result["qty"] == 5


def test_add_eligibility_blocks_averaging_down_and_post_reduction_adds():
    result = evaluate_add_eligibility(
        {
            "score": 90,
            "remaining_r": 2,
            "components": {
                "trend_checks": {
                    "price_above_ema21": True,
                    "price_above_ema50": True,
                    "ema21_slope_positive": True,
                    "market_regime_favorable": True,
                }
            },
        },
        {"score": 90, "status": "active_signal"},
        {"initial_qty": 10, "add_count": 0, "adverse_reduction_completed": True},
        {"qty": 10, "avg_entry_price": 100, "current_price": 99, "market_value": 990},
        {"stop_price": 92, "stop_qty": 10},
        50000,
        {"additions_enabled": True},
    )

    assert result["eligible"] is False
    assert "averaging_down_blocked" in result["reasons"]
    assert "adverse_reduction_lockout" in result["reasons"]
