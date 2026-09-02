import datetime

from traderbot.backtester.baseline import (
    attribution_by_dimensions,
    combine_forward_return_reports,
    dataset_manifest,
    finalize_forward_returns,
    new_forward_return_accumulator,
    performance_metrics,
    record_rejected_candidate,
    stable_hash,
)


UTC = datetime.timezone.utc


def test_performance_metrics_include_returns_risk_turnover_slippage_and_exposure():
    curve = [
        {
            "timestamp": f"2026-01-0{day}T20:00:00+00:00",
            "equity": equity,
            "gross_exposure_percent": exposure,
            "cash_percent": 100 - exposure,
        }
        for day, equity, exposure in (
            (1, 10000, 0),
            (2, 10100, 30),
            (3, 10050, 60),
            (4, 10200, 80),
        )
    ]
    portfolio = {
        "starting_equity": 10000,
        "ending_equity": 10200,
        "net_pnl": 200,
        "return_percent": 2,
        "max_drawdown_percent": 0.5,
    }
    trades = [
        {
            "initial_notional": 1000,
            "qty": 10,
            "exit_price": 110,
            "pnl": 100,
            "adds": [],
            "reductions": [],
        },
        {
            "initial_notional": 1000,
            "qty": 10,
            "exit_price": 90,
            "pnl": -50,
            "adds": [],
            "reductions": [],
        },
    ]

    result = performance_metrics(portfolio, trades, curve, estimated_slippage_bps=10)

    assert result["trades"] == 2
    assert result["win_rate_percent"] == 50
    assert result["profit_factor"] == 2
    assert result["expectancy_per_trade"] == 25
    assert result["gross_turnover"] == 4000
    assert result["estimated_slippage_cost"] == 4
    assert result["net_pnl_after_estimated_slippage"] == 196
    assert result["time_at_or_above_25_percent_exposure"] == 75
    assert result["time_at_or_above_50_percent_exposure"] == 50
    assert result["time_at_or_above_75_percent_exposure"] == 25
    assert result["sharpe_ratio"] is not None
    assert result["sortino_ratio"] is not None


def test_rejected_candidate_forward_returns_are_diagnostic_and_use_later_closes():
    start = datetime.datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    bars = [
        {"t": start + datetime.timedelta(minutes=5 * index), "c": close}
        for index, close in enumerate((100, 101, 103, 99, 105))
    ]
    accumulator = new_forward_return_accumulator((1, 3))
    record_rejected_candidate(
        accumulator,
        {
            "model_id": "pullback_reclaim",
            "status": "watch",
            "setup_score": 70,
            "blockers": ["market_ok"],
        },
        "AAA",
        bars,
        0,
    )
    result = finalize_forward_returns(accumulator)
    overall = [row for row in result["rows"] if row["group"] == "overall"]

    assert result["rejected_candidates"] == 1
    assert overall[0]["horizon_bars"] == 1
    assert overall[0]["average_forward_return_percent"] == 1
    assert overall[1]["horizon_bars"] == 3
    assert overall[1]["average_forward_return_percent"] == -1
    assert "never enter order logic" in result["method"]


def test_combined_forward_returns_are_observation_weighted():
    first = {
        "rejected_candidates": 1,
        "horizons_bars": [1],
        "rows": [{
            "group": "overall", "horizon_bars": 1, "observations": 1,
            "average_forward_return_percent": 1, "positive_forward_return_percent": 100,
            "forward_return_stdev_percent": 0,
        }],
    }
    second = {
        "rejected_candidates": 3,
        "horizons_bars": [1],
        "rows": [{
            "group": "overall", "horizon_bars": 1, "observations": 3,
            "average_forward_return_percent": -1, "positive_forward_return_percent": 0,
            "forward_return_stdev_percent": 0,
        }],
    }

    combined = combine_forward_return_reports([first, second])

    assert combined["rejected_candidates"] == 4
    assert combined["rows"][0]["average_forward_return_percent"] == -0.5
    assert combined["rows"][0]["positive_forward_return_percent"] == 25


def test_attribution_crosses_regime_sector_and_model():
    timestamp = datetime.datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    rows = attribution_by_dimensions(
        [{
            "signal_time": timestamp.isoformat(),
            "sector_benchmark": "XLK",
            "entry_model_id": "breakout_continuation",
            "pnl": 25,
        }],
        [],
        lambda _bars, _timestamp: "bull",
    )

    assert rows == [{
        "regime": "bull",
        "sector_benchmark": "XLK",
        "model": "breakout_continuation",
        "trades": 1,
        "win_rate_percent": 100.0,
        "net_pnl": 25.0,
        "expectancy": 25.0,
        "profit_factor": None,
    }]


def test_dataset_manifest_and_config_hash_are_reproducible():
    timestamp = datetime.datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    bars = {"AAA": [{"t": timestamp, "o": 1, "h": 2, "l": 1, "c": 2, "v": 10}]}

    first = dataset_manifest(bars)
    second = dataset_manifest(bars)

    assert first == second
    assert first["AAA"]["bars"] == 1
    assert len(first["AAA"]["sha256"]) == 64
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})
