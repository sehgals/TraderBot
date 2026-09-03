from traderbot.backtester.model_admission import (
    evaluate_model_admission,
    return_correlation,
)


def report(sharpe, pnl, trades=30, pnls=(10, -5, 8)):
    return {
        "account_equity_assumption": 10_000,
        "baseline_metrics": {
            "sharpe_ratio": sharpe,
            "net_pnl_after_estimated_slippage": pnl,
            "trades": trades,
        },
        "trades": [
            {"exit_time": f"2026-01-0{index + 1}T20:00:00Z", "pnl": value}
            for index, value in enumerate(pnls)
        ],
    }


def test_return_correlation_is_measured_on_aligned_oos_days():
    assert return_correlation({"a": 1, "b": 2}, {"a": 2, "b": 4}) == 1
    assert return_correlation({"a": 1, "b": 2}, {"a": 2, "b": 1}) == -1


def test_model_not_admitted_for_exposure_without_incremental_sharpe():
    result = evaluate_model_admission(
        report(1.0, 500),
        report(0.4, 200, pnls=(5, 8, -2)),
        report(0.9, 900),
        maximum_correlation=1.0,
    )
    assert result["admitted"] is False
    assert "no_incremental_risk_adjusted_return" in result["reasons"]
    assert result["return_correlation"] is not None


def test_model_requires_isolated_positive_sample_and_correlation_measurement():
    result = evaluate_model_admission(
        report(1.0, 500), report(0.2, -10, trades=2, pnls=()), report(1.2, 600)
    )
    assert result["admitted"] is False
    assert "insufficient_isolated_oos_trades" in result["reasons"]
    assert "isolated_oos_return_not_positive" in result["reasons"]
    assert "return_correlation_unavailable" in result["reasons"]
