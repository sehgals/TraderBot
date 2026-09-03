import datetime

from traderbot.core_strategy_engine.entry_models.signal_families import (
    evaluate_low_volatility_trend,
    evaluate_post_earnings_drift,
    evaluate_signal_families,
)


def features():
    now = datetime.datetime(2026, 9, 2, 14, 30, tzinfo=datetime.timezone.utc)
    bars = []
    for index in range(55):
        close = 100 + index * 0.1
        bars.append({
            "t": now - datetime.timedelta(minutes=5 * (54 - index)),
            "c": close, "h": close + 0.5, "l": close - 0.5,
            "ema9": close - 0.2, "ema21": close - 0.5,
            "ema50": 98 + index * 0.03, "vwap": close - 0.3,
            "atr14": 1, "volume_ratio": 1.2,
        })
    return {
        "symbol": "TEST", "as_of": now, "bar": bars[-1],
        "previous_bar": bars[-2], "recent_three_bars": bars[-3:],
        "prior_20_bars": bars[-21:-1], "bars": bars,
        "market_bars": bars, "sector_bars": bars,
        "atr14": 1, "recent_high_20": max(x["h"] for x in bars[-21:-1]),
        "recent_low_20": min(x["l"] for x in bars[-21:-1]),
        "ema21_slope_5bars": 0.01, "relative_dollar_volume": 1.2,
        "ledger_cap": float("inf"), "market_ok": True, "sector_ok": True,
        "above_exit": True, "no_same_day_loss_reentry": True,
        "entry_filters": {"checks": {}, "metrics": {}}, "event_calendar": None,
    }


def test_all_new_families_are_independent_and_disabled_by_default():
    candidates = evaluate_signal_families(features(), {})
    assert len(candidates) == 5
    assert len({item["model_id"] for item in candidates}) == 5
    assert all(item["status"] == "watch" for item in candidates)
    assert all("risk_budget_percent" in item for item in candidates)


def test_enabled_low_volatility_model_carries_independent_risk_budget():
    result = evaluate_low_volatility_trend(features(), {
        "entry_models": {"low_volatility_trend": {
            "enabled": True, "risk_budget_percent": 0.17,
            "minimum_setup_score": 0,
        }}
    })
    assert result["risk_budget_percent"] == 0.17
    assert result["hard_blockers"] == []


def test_post_earnings_model_hard_blocks_missing_point_in_time_event_data():
    result = evaluate_post_earnings_drift(features(), {
        "entry_models": {"post_earnings_drift": {"enabled": True}}
    })
    assert "post_earnings_data_available" in result["hard_blockers"]
    assert "positive_earnings_surprise" in result["hard_blockers"]
