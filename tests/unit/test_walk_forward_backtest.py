import datetime

from scripts.run_walk_forward_backtest import (
    attribution_rows,
    calibration_objective,
    configs_with_score_weights,
    prepare_filter_configs,
    select_weight_profile,
    walk_forward_windows,
)


UTC = datetime.timezone.utc


def test_walk_forward_windows_are_purged_and_non_overlapping():
    windows = walk_forward_windows(
        datetime.datetime(2025, 1, 1, tzinfo=UTC),
        datetime.datetime(2025, 7, 1, tzinfo=UTC),
        train_days=60,
        test_days=30,
        purge_days=2,
    )

    assert windows[0]["test_start"] - windows[0]["train_end"] == datetime.timedelta(days=2)
    assert windows[1]["test_start"] - windows[0]["test_start"] == datetime.timedelta(days=30)


def test_unavailable_point_in_time_data_is_reported_not_silently_passed():
    configs, coverage = prepare_filter_configs({"IBM": {"entry_filters": {}}})

    assert configs["IBM"]["entry_filters"]["liquidity"]["enabled"] is True
    assert configs["IBM"]["entry_filters"]["spread"]["enabled"] is False
    assert coverage["spread"].startswith("not_tested")
    assert coverage["earnings"].startswith("not_tested")


def test_attribution_crosses_regime_and_risk_profile():
    start = datetime.datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    market = []
    for index in range(8):
        close = 100 + index
        market.append(
            {
                "t": start + datetime.timedelta(minutes=5 * index),
                "c": close,
                "ema21": close - 1,
                "ema50": close - 2,
            }
        )
    trades = [
        {
            "signal_time": market[-1]["t"].isoformat(),
            "profile": "large_cap",
            "pnl": 25,
        }
    ]
    assert attribution_rows(trades, market) == [
        {
            "regime": "bull",
            "risk_profile": "large_cap",
            "trades": 1,
            "wins": 1,
            "win_rate_percent": 100.0,
            "net_pnl": 25.0,
            "profit_factor": None,
        }
    ]


def test_weight_calibration_selects_training_objective_and_does_not_mutate_config():
    configs = {"PANW": {"minimum_entry_setup_score": 80}}
    weighted = configs_with_score_weights(configs, {"price_action": 40})
    selected = select_weight_profile([
        {"profile": "balanced", "objective": 1.0, "trades": 2},
        {"profile": "reward_risk", "objective": 2.0, "trades": 1},
    ])

    assert selected["profile"] == "reward_risk"
    assert weighted["PANW"]["entry_score_weights"] == {"price_action": 40}
    assert "entry_score_weights" not in configs["PANW"]


def test_calibration_objective_penalizes_drawdown_and_no_trade_profiles():
    simulation = {
        "portfolio": {"return_percent": 4, "max_drawdown_percent": 1.5},
        "trades_detail": [{"pnl": 1}],
    }
    assert calibration_objective(simulation) == 2.5
    simulation["trades_detail"] = []
    assert calibration_objective(simulation) == -1_000_000
