import copy
import datetime as dt
from unittest.mock import patch

import pytest

from scripts import run_risk_control_backtest as backtest
from traderbot.core_strategy_engine import engine
from traderbot.core_strategy_engine.hourly_policy import (
    advance_confirmation, apply_hourly_entry_gate, hourly_entry_assessment, hourly_gate_mode,
)
from traderbot.backtester.swing_research import research_configs, transform_swing_candidate, lifecycle_metrics

NOW = dt.datetime(2026, 9, 18, 18, tzinfo=dt.timezone.utc)


def context(stamp=NOW - dt.timedelta(hours=1), **bar):
    return {"as_of": stamp.isoformat(), "bar_id": f"AAA:1Hour:{stamp.isoformat()}",
            "latest_bar": {"c": 100, "ema21": 99, "ema50": 98, "ema21_slope": 1,
                           "atr14": 1, "l": 98, **bar}}


def assessment(hour, action="reduce", **kwargs):
    return {**context(NOW + dt.timedelta(hours=hour)), "data_fresh": True,
            "data_complete": True, "recommended_action": action, **kwargs}


def test_confirmation_repoll_gap_action_change_and_episode_reset():
    first = advance_confirmation(None, assessment(0), "episode")
    assert first["count"] == 1
    assert advance_confirmation(first, assessment(0), "episode") == first
    assert advance_confirmation(first, assessment(-1), "episode") == first
    assert advance_confirmation(first, assessment(1, bar_id=first["last_bar_id"]), "episode") == first
    assert advance_confirmation(first, assessment(1, data_fresh=False), "episode") == first
    assert advance_confirmation(first, assessment(1, data_complete=False), "episode") == first
    second = advance_confirmation(first, assessment(1), "episode")
    assert second["count"] == 2
    assert advance_confirmation(second, assessment(3), "episode")["count"] == 1
    assert advance_confirmation(second, assessment(2, "exit"), "episode")["count"] == 1
    assert advance_confirmation(second, assessment(2, "hold"), "episode")["count"] == 0
    assert advance_confirmation(second, assessment(2), "new episode")["count"] == 1
    assert advance_confirmation({"count": 9, "action": "reduce"}, assessment(0), "episode")["count"] == 1


@pytest.mark.parametrize("changes,reason", [
    ({"c": 95, "ema21_slope": -1}, "hourly_structural_trend_failure"),
    ({"ema21": float("nan")}, "hourly_trend_data_unavailable"),
    ({"ema50": None}, "hourly_trend_data_unavailable"),
])
def test_hourly_gate_failures(changes, reason):
    result = hourly_entry_assessment(context(**changes), NOW)
    assert not result["passed"] and reason in result["reasons"]


def test_hourly_freshness_and_partial_trend_failure():
    assert hourly_entry_assessment(context(c=95), NOW)["passed"]
    assert not hourly_entry_assessment(context(NOW), NOW)["data_fresh"]
    assert not hourly_entry_assessment(context(NOW - dt.timedelta(hours=3)), NOW)["data_fresh"]
    assert not hourly_entry_assessment({}, NOW)["passed"]
    assert hourly_entry_assessment(context(), NOW.replace(tzinfo=None))["passed"]


def test_modes_and_model_scope_preserve_existing_blockers():
    failed = hourly_entry_assessment(context(c=95, ema21_slope=-1), NOW)
    candidate = {"model_id": "breakout_continuation", "status": "active_signal", "hard_blockers": ["spread_ok"]}
    for mode in ("off", "shadow"):
        result = apply_hourly_entry_gate(copy.deepcopy(candidate), failed, mode)
        assert result["status"] == "active_signal"
        assert result["hard_blockers"] == ["spread_ok"]
    blocked = apply_hourly_entry_gate(copy.deepcopy(candidate), failed, "enforce")
    assert blocked["status"] == "watch"
    assert "spread_ok" in blocked["hard_blockers"]
    assert "hourly_structural_trend_failure" in blocked["hard_blockers"]
    unrelated = {**candidate, "model_id": "mean_reversion"}
    assert apply_hourly_entry_gate(copy.deepcopy(unrelated), failed, "enforce") == unrelated
    with pytest.raises(ValueError):
        hourly_gate_mode({"position_health": {"entry_gate_mode": "invalid"}})


@pytest.mark.parametrize("mode,passed", [("shadow", True), ("enforce", False)])
def test_submission_rechecks_hourly_data(mode, passed):
    now = dt.datetime.now(dt.timezone.utc)
    class Client:
        def latest_quote(self, symbol):
            return {"bid_price": 100, "ask_price": 100.01, "timestamp": now.isoformat()}
    plan = {"model_id": "pullback_reclaim", "status": "active_signal", "last_bar_time": now.isoformat(),
            "limit_price": 100, "stop_price": 99, "target_price": 103}
    with patch.object(engine, "live_hourly_entry_context", side_effect=RuntimeError("unavailable")):
        result = engine.validate_entry_submission(Client(), {"symbol": "AAA", "position_health": {"entry_gate_mode": mode}}, plan)
    assert result["passed"] == passed
    assert "hourly_trend_data_unavailable" in result["hourly_entry_gate"]["reasons"]


def test_historical_hourly_context_excludes_unfinished_and_future_bars():
    bars = [{"t": NOW + dt.timedelta(hours=i - 51), "ema21": i + 1, "l": i + 1} for i in range(53)]
    result = backtest.historical_hourly_entry_context("AAA", NOW - dt.timedelta(minutes=5), {"AAA": bars})
    assert result["as_of"] == (NOW - dt.timedelta(hours=1)).isoformat()
    assert result["latest_bar"]["ema21_slope"] == 5
    assert result["structure_low"] == 46


def test_research_stop_risk_sizing_and_catastrophic_limit():
    original = {"AAA": {"symbol": "AAA"}}
    config = research_configs(original, "hourly_swing")["AAA"]
    assert original == {"AAA": {"symbol": "AAA"}}
    candidate = {"limit_price": 100, "stop_price": 99, "target_price": 106, "status": "active_signal"}
    transform_swing_candidate(candidate, config, context(), NOW)
    assert candidate["stop_price"] == 97.5
    assert candidate["risk_per_share"] == 2.5
    assert candidate["expected_reward_risk"] == 2.4
    assert backtest.dynamic_qty(config, candidate, 10000) == 20
    transform_swing_candidate(candidate, config, context(atr14=4), NOW)
    assert "swing_stop_exceeds_catastrophic_limit" in candidate["hard_blockers"]


def test_research_stop_requalifies_reward_risk():
    config = research_configs({"AAA": {}}, "hourly_swing")["AAA"]
    candidate = {"limit_price": 100, "stop_price": 99, "target_price": 102, "status": "active_signal"}
    transform_swing_candidate(candidate, config, context(), NOW)
    assert candidate["status"] == "watch"
    assert "swing_reward_risk_too_low" in candidate["hard_blockers"]


def test_lifecycle_metrics_use_market_date_for_reentries():
    trades = [
        {"symbol": "AAA", "entry_time": "2026-09-18T22:00:00Z", "exit_time": "2026-09-18T23:00:00Z", "exit_reason": "stop_floor"},
        {"symbol": "AAA", "entry_time": "2026-09-19T00:00:00Z", "exit_time": "2026-09-19T01:00:00Z", "exit_reason": "end_open_mark"},
    ]
    result = lifecycle_metrics(trades)
    assert result["average_holding_hours"] == 1
    assert result["same_day_reentries_after_stop"] == 1
