import datetime
from unittest.mock import patch

from scripts import run_risk_control_backtest as backtest


def bars(symbol, next_bar_low=99.0):
    start = datetime.datetime(2026, 1, 5, 14, 30, tzinfo=datetime.timezone.utc)
    result = []
    for index in range(80):
        low = 99.0
        if index == 60:
            low = 90.0
        elif index == 61:
            low = next_bar_low
        result.append(
            {
                "t": start + datetime.timedelta(minutes=5 * index),
                "o": 100.0,
                "h": 102.0,
                "l": low,
                "c": 100.0,
                "v": 1000.0,
                "ema9": 99.5,
                "ema21": 99.0,
                "ema50": 98.0,
                "atr14": 1.0,
                "vwap": 99.0,
                "volume_ratio": 2.0,
                "symbol": symbol,
            }
        )
    return result


def config(symbol, notional=5000):
    return {
        "symbol": symbol,
        "dynamic_entry_notional": notional,
        "dynamic_market_filter_ignored_notional": notional,
        "initial_stop_loss_percent": 20,
        "trail_trigger_step_percent": 5,
        "trail_stop_below_current_percent": 2.5,
        "ladder_buy_quantity": 0,
        "ladder_drop_steps_percent": [],
        "max_ladder_count": 0,
        "min_cash_balance_percent": 0,
        "reentry_max_per_day": 1,
        "risk_profile": "test",
    }


def signal_on_bar_60(signal_times, priorities=None):
    priorities = priorities or {}

    def fake_plan(
        symbol,
        symbol_bars,
        market_bars,
        exit_trade=None,
        ignore_ledger=False,
        sector_bars=None,
        config=None,
    ):
        latest = symbol_bars[-1]
        if latest["t"] != signal_times[symbol]:
            return {"symbol": symbol, "status": "watch", "last_bar_time": latest["t"].isoformat()}
        return {
            "symbol": symbol,
            "status": "active_signal",
            "mode": "dynamic_breakout_continuation",
            "limit_price": 100.0,
            "last_bar_time": latest["t"].isoformat(),
            "volume_ratio": priorities.get(symbol, 2.0),
            "market_filter_ignored": False,
        }

    return fake_plan


def test_signal_cannot_fill_from_generating_bar_range():
    symbol_bars = bars("AAA", next_bar_low=101.0)
    for bar in symbol_bars[61:]:
        bar.update({"o": 101.0, "l": 101.0, "c": 101.0})
    signal_times = {"AAA": symbol_bars[60]["t"]}

    with patch.object(backtest, "dynamic_entry_plan", signal_on_bar_60(signal_times)):
        result = backtest.simulate_portfolio(
            {"AAA": config("AAA")},
            {"AAA": symbol_bars},
            symbol_bars,
            starting_equity=10_000,
        )

    assert result["portfolio"]["trades"] == 0
    assert result["portfolio"]["pending_orders_at_end"] == 1


def test_first_eligible_fill_is_on_next_bar():
    symbol_bars = bars("AAA", next_bar_low=99.0)
    signal_times = {"AAA": symbol_bars[60]["t"]}

    with patch.object(backtest, "dynamic_entry_plan", signal_on_bar_60(signal_times)):
        result = backtest.simulate_portfolio(
            {"AAA": config("AAA")},
            {"AAA": symbol_bars},
            symbol_bars,
            starting_equity=10_000,
        )

    trade = result["trades_detail"][0]
    assert trade["signal_time"] == symbol_bars[60]["t"].isoformat()
    assert trade["entry_time"] == symbol_bars[61]["t"].isoformat()


def test_simultaneous_orders_share_one_cash_balance():
    aaa = bars("AAA")
    bbb = bars("BBB")
    signal_times = {"AAA": aaa[60]["t"], "BBB": bbb[60]["t"]}

    with patch.object(
        backtest,
        "dynamic_entry_plan",
        signal_on_bar_60(signal_times, priorities={"AAA": 3.0, "BBB": 2.0}),
    ):
        result = backtest.simulate_portfolio(
            {"AAA": config("AAA", 6000), "BBB": config("BBB", 6000)},
            {"AAA": aaa, "BBB": bbb},
            aaa,
            starting_equity=10_000,
        )

    quantities = {trade["symbol"]: trade["base_qty"] for trade in result["trades_detail"]}
    assert quantities == {"AAA": 60, "BBB": 40}
    assert result["portfolio"]["max_gross_exposure"] <= 10_000


def test_portfolio_uses_live_catastrophic_floor_and_hard_reduction():
    symbol_bars = bars("AAA")
    symbol_bars[62].update({"o": 93.0, "h": 94.0, "l": 93.0, "c": 93.0})
    signal_times = {"AAA": symbol_bars[60]["t"]}

    assert backtest.managed_initial_floor_price(config("AAA"), 100.0) == 92.0
    with patch.object(backtest, "dynamic_entry_plan", signal_on_bar_60(signal_times)):
        result = backtest.simulate_portfolio(
            {"AAA": config("AAA")},
            {"AAA": symbol_bars},
            symbol_bars,
            starting_equity=10_000,
        )

    trade = result["trades_detail"][0]
    assert trade["reductions"][0]["reason"] == "hard_reduction"
    assert trade["reductions"][0]["qty"] == 25


def test_active_live_health_policy_requires_matching_hourly_data():
    symbol_bars = bars("AAA")
    active_config = config("AAA")
    active_config["position_health"] = {
        "enabled": True,
        "shadow_mode": False,
        "benchmark_symbol": "QQQ",
    }

    try:
        backtest.simulate_portfolio(
            {"AAA": active_config},
            {"AAA": symbol_bars},
            symbol_bars,
            starting_equity=10_000,
        )
    except ValueError as exc:
        assert "requires 1Hour health bars for AAA and QQQ" in str(exc)
    else:
        raise AssertionError("active live policy was silently omitted")


def test_confirmed_position_health_reduction_is_simulated():
    symbol_bars = bars("AAA")
    signal_times = {"AAA": symbol_bars[60]["t"]}
    active_config = config("AAA")
    active_config["position_health"] = {
        "enabled": True,
        "shadow_mode": False,
        "benchmark_symbol": "QQQ",
        "additions_enabled": False,
        "reduction_confirmation_bars": 2,
        "health_reduction_fraction": 0.5,
    }
    health = {
        "score": 20,
        "state": "At Risk",
        "recommended_action": "reduce",
        "data_fresh": True,
        "remaining_r": 1.0,
        "components": {"trend_checks": {}},
    }

    with (
        patch.object(backtest, "dynamic_entry_plan", signal_on_bar_60(signal_times)),
        patch.object(backtest, "evaluate_position_health", return_value=health),
    ):
        result = backtest.simulate_portfolio(
            {"AAA": active_config},
            {"AAA": symbol_bars},
            symbol_bars,
            starting_equity=10_000,
            health_bars_by_symbol={"AAA": symbol_bars, "QQQ": symbol_bars},
        )

    trade = result["trades_detail"][0]
    assert trade["reductions"][0]["reason"] == "health_reduce"
    assert trade["reductions"][0]["qty"] == 25
